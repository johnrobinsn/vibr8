"""Tests for the Codex adapter and WsBridge adapter integration.

Covers: adapter lifecycle, message translation, state management,
permission flow, error handling, and WsBridge attach_adapter callback.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vibr8_core.codex_adapter import CodexAdapter, CodexAdapterOptions, JsonRpcTransport
from vibr8_core.ws_bridge import WsBridge, Session


# ── Helpers ──────────────────────────────────────────────────────────────────


def make_mock_proc(stdout_lines: list[str] | None = None):
    """Create a mock asyncio.subprocess.Process with stdio pipes."""
    proc = MagicMock()
    proc.pid = 12345
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()

    # stdout: feed lines then EOF
    reader = AsyncMock()
    if stdout_lines:
        data_chunks = [line.encode() for line in stdout_lines]
        data_chunks.append(b"")  # EOF
        reader.read = AsyncMock(side_effect=data_chunks)
    else:
        reader.read = AsyncMock(return_value=b"")
    proc.stdout = reader
    proc.stderr = None

    # wait() never returns unless explicitly resolved
    wait_future = asyncio.get_event_loop().create_future()
    proc.wait = AsyncMock(return_value=wait_future)
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    return proc


def json_line(obj: dict) -> str:
    return json.dumps(obj) + "\n"


# ── JsonRpcTransport Tests ──────────────────────────────────────────────────


class TestJsonRpcTransport:
    """Low-level JSON-RPC transport: read, write, dispatch."""

    @pytest.fixture
    async def transport(self):
        stdin = MagicMock()
        stdin.write = MagicMock()
        stdin.drain = AsyncMock()
        stdout = AsyncMock()
        stdout.read = AsyncMock(return_value=b"")
        t = JsonRpcTransport(stdin, stdout)
        t._reader_task.cancel()
        yield t

    async def test_dispatch_notification(self, transport):
        handler = MagicMock()
        transport.on_notification(handler)
        transport._dispatch({"method": "item/started", "params": {"item": {"type": "agentMessage"}}})
        handler.assert_called_once_with("item/started", {"item": {"type": "agentMessage"}})

    async def test_dispatch_request(self, transport):
        handler = MagicMock()
        transport.on_request(handler)
        transport._dispatch({"method": "item/commandExecution/requestApproval", "id": 5, "params": {"command": "ls"}})
        handler.assert_called_once_with("item/commandExecution/requestApproval", 5, {"command": "ls"})

    async def test_dispatch_response_resolves_future(self, transport):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        transport._pending[1] = future
        transport._dispatch({"id": 1, "result": {"thread": {"id": "t1"}}})
        assert future.done()
        assert future.result() == {"thread": {"id": "t1"}}

    async def test_dispatch_response_error(self, transport):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        transport._pending[2] = future
        transport._dispatch({"id": 2, "error": {"message": "boom"}})
        assert future.done()
        with pytest.raises(RuntimeError, match="boom"):
            future.result()

    async def test_process_buffer_handles_partial_lines(self, transport):
        handler = MagicMock()
        transport.on_notification(handler)

        transport._buffer = '{"method": "a", "params": {}}\n{"metho'
        transport._process_buffer()
        handler.assert_called_once_with("a", {})
        assert transport._buffer == '{"metho'

    async def test_process_buffer_skips_blank_lines(self, transport):
        handler = MagicMock()
        transport.on_notification(handler)

        transport._buffer = '\n\n{"method": "b", "params": {"x": 1}}\n\n'
        transport._process_buffer()
        handler.assert_called_once_with("b", {"x": 1})

    async def test_call_sends_request_and_awaits(self, transport):
        async def fake_drain():
            transport._dispatch({"id": 1, "result": {"ok": True}})

        transport._stdin.drain = fake_drain
        result = await transport.call("initialize", {"clientInfo": {}})
        assert result == {"ok": True}
        transport._stdin.write.assert_called_once()
        written = transport._stdin.write.call_args[0][0].decode()
        msg = json.loads(written)
        assert msg["method"] == "initialize"
        assert msg["id"] == 1

    async def test_notify_sends_without_id(self, transport):
        await transport.notify("initialized", {})
        transport._stdin.write.assert_called_once()
        written = transport._stdin.write.call_args[0][0].decode()
        msg = json.loads(written)
        assert msg["method"] == "initialized"
        assert "id" not in msg

    async def test_connected_property(self, transport):
        assert transport.connected is True
        transport._connected = False
        assert transport.connected is False


# ── CodexAdapter Interface Tests ────────────────────────────────────────────


class TestCodexAdapterInterface:
    """Verify the adapter implements the interface WsBridge expects."""

    @pytest.fixture
    async def adapter(self):
        proc = make_mock_proc()
        a = CodexAdapter(proc, "test-session-1", CodexAdapterOptions(model="gpt-5.5", cwd="/code"))
        a._init_task.cancel()
        a._exit_task.cancel()
        yield a

    async def test_is_connected_method_exists(self, adapter):
        assert hasattr(adapter, "is_connected")
        assert callable(adapter.is_connected)

    async def test_is_connected_returns_bool(self, adapter):
        assert adapter.is_connected() is False

    async def test_connected_property(self, adapter):
        assert adapter.connected is False
        adapter._connected = True
        assert adapter.connected is True
        assert adapter.is_connected() is True

    async def test_on_browser_message_callback(self, adapter):
        cb = MagicMock()
        adapter.on_browser_message(cb)
        assert adapter._browser_message_cb is cb

    async def test_on_session_meta_callback(self, adapter):
        cb = MagicMock()
        adapter.on_session_meta(cb)
        assert adapter._session_meta_cb is cb

    async def test_on_disconnect_callback(self, adapter):
        cb = MagicMock()
        adapter.on_disconnect(cb)
        assert adapter._disconnect_cb is cb

    async def test_on_init_error_callback(self, adapter):
        cb = MagicMock()
        adapter.on_init_error(cb)
        assert adapter._init_error_cb is cb

    async def test_send_browser_message_queues_when_not_initialized(self, adapter):
        msg = {"type": "user_message", "content": "hello"}
        result = adapter.send_browser_message(msg)
        assert result is True
        assert len(adapter._pending_outgoing) == 1

    async def test_send_browser_message_drops_non_queueable_when_disconnected(self, adapter):
        msg = {"type": "set_model", "model": "gpt-4"}
        result = adapter.send_browser_message(msg)
        assert result is False

    async def test_disconnect_terminates_process(self, adapter):
        adapter._connected = True
        await adapter.disconnect()
        assert adapter.connected is False
        adapter._proc.terminate.assert_called_once()

    async def test_thread_id_property(self, adapter):
        assert adapter.thread_id is None
        adapter._thread_id = "t-123"
        assert adapter.thread_id == "t-123"


# ── CodexAdapter Message Translation Tests ──────────────────────────────────


class TestCodexAdapterMessages:
    """Verify Codex JSON-RPC events are correctly translated to browser messages."""

    @pytest.fixture
    async def adapter(self):
        proc = make_mock_proc()
        a = CodexAdapter(proc, "test-session-2", CodexAdapterOptions(model="gpt-5.5", cwd="/code"))
        a._init_task.cancel()
        a._exit_task.cancel()
        a._connected = True
        a._initialized = True
        a._thread_id = "thread-1"
        yield a

    def collect_emitted(self, adapter) -> list[dict]:
        msgs: list[dict] = []
        adapter.on_browser_message(lambda m: msgs.append(m))
        return msgs

    async def test_agent_message_started_emits_stream_events(self, adapter):
        msgs = self.collect_emitted(adapter)
        adapter._handle_notification("item/started", {
            "item": {"type": "agentMessage", "id": "am-1"},
        })
        types = [m.get("type") for m in msgs]
        assert "stream_event" in types
        events = [m["event"]["type"] for m in msgs if m.get("type") == "stream_event"]
        assert "message_start" in events
        assert "content_block_start" in events

    async def test_agent_message_delta_emits_text_delta(self, adapter):
        msgs = self.collect_emitted(adapter)
        adapter._handle_notification("item/agentMessage/delta", {"delta": "Hello "})
        adapter._handle_notification("item/agentMessage/delta", {"delta": "world"})
        assert adapter._streaming_text == "Hello world"
        deltas = [m for m in msgs if m.get("type") == "stream_event"
                  and m.get("event", {}).get("type") == "content_block_delta"]
        assert len(deltas) == 2

    async def test_agent_message_completed_emits_assistant(self, adapter):
        msgs = self.collect_emitted(adapter)
        adapter._streaming_text = "Hello world"
        adapter._streaming_item_id = "am-1"
        adapter._handle_notification("item/completed", {
            "item": {"type": "agentMessage", "id": "am-1", "text": "Hello world"},
        })
        assistant_msgs = [m for m in msgs if m.get("type") == "assistant"]
        assert len(assistant_msgs) == 1
        content = assistant_msgs[0]["message"]["content"]
        assert content[0]["text"] == "Hello world"
        assert adapter._streaming_text == ""
        assert adapter._streaming_item_id is None

    async def test_command_execution_emits_tool_use_and_result(self, adapter):
        msgs = self.collect_emitted(adapter)
        adapter._handle_notification("item/started", {
            "item": {"type": "commandExecution", "id": "cmd-1", "command": "ls -la"},
        })
        adapter._handle_notification("item/completed", {
            "item": {
                "type": "commandExecution", "id": "cmd-1",
                "command": "ls -la", "stdout": "file.txt\n", "stderr": "",
                "exitCode": 0, "status": "completed",
            },
        })
        tool_uses = [m for m in msgs if m.get("type") == "assistant"
                     and any(c.get("type") == "tool_use" for c in m.get("message", {}).get("content", []))]
        tool_results = [m for m in msgs if m.get("type") == "assistant"
                        and any(c.get("type") == "tool_result" for c in m.get("message", {}).get("content", []))]
        assert len(tool_uses) >= 1
        assert len(tool_results) == 1
        assert tool_results[0]["message"]["content"][0]["content"] == "file.txt"

    async def test_file_change_emits_tool_use_and_result(self, adapter):
        msgs = self.collect_emitted(adapter)
        adapter._handle_notification("item/started", {
            "item": {
                "type": "fileChange", "id": "fc-1",
                "changes": [{"path": "/code/main.py", "kind": "edit"}],
            },
        })
        adapter._handle_notification("item/completed", {
            "item": {
                "type": "fileChange", "id": "fc-1",
                "changes": [{"path": "/code/main.py", "kind": "edit"}],
                "status": "completed",
            },
        })
        tool_uses = [m for m in msgs if m.get("type") == "assistant"
                     and any(c.get("type") == "tool_use" for c in m.get("message", {}).get("content", []))]
        assert len(tool_uses) >= 1
        assert tool_uses[0]["message"]["content"][0]["name"] == "Edit"

    async def test_turn_completed_emits_result(self, adapter):
        msgs = self.collect_emitted(adapter)
        adapter._handle_notification("turn/completed", {
            "turn": {"status": "completed"},
        })
        results = [m for m in msgs if m.get("type") == "result"]
        assert len(results) == 1
        assert results[0]["data"]["subtype"] == "success"
        assert results[0]["data"]["is_error"] is False

    async def test_turn_completed_with_error(self, adapter):
        msgs = self.collect_emitted(adapter)
        adapter._handle_notification("turn/completed", {
            "turn": {"status": "failed", "error": {"message": "Something went wrong"}},
        })
        results = [m for m in msgs if m.get("type") == "result"]
        assert len(results) == 1
        assert results[0]["data"]["is_error"] is True
        assert results[0]["data"]["result"] == "Something went wrong"

    async def test_turn_completed_clears_emitted_tool_use_ids(self, adapter):
        adapter._emitted_tool_use_ids.add("cmd-1")
        adapter._handle_notification("turn/completed", {"turn": {"status": "completed"}})
        assert len(adapter._emitted_tool_use_ids) == 0

    async def test_token_usage_updated_emits_session_update(self, adapter):
        msgs = self.collect_emitted(adapter)
        adapter._handle_notification("thread/tokenUsage/updated", {
            "tokenUsage": {
                "total": {"inputTokens": 5000, "outputTokens": 1000},
                "modelContextWindow": 100000,
            },
        })
        updates = [m for m in msgs if m.get("type") == "session_update"]
        assert len(updates) == 1
        assert updates[0]["session"]["context_used_percent"] == 6

    async def test_ensure_tool_use_emitted_backfills_missing_start(self, adapter):
        msgs = self.collect_emitted(adapter)
        adapter._handle_notification("item/completed", {
            "item": {
                "type": "commandExecution", "id": "cmd-backfill",
                "command": "echo hi", "stdout": "hi", "stderr": "",
                "exitCode": 0, "status": "completed",
            },
        })
        tool_uses = [m for m in msgs if m.get("type") == "assistant"
                     and any(c.get("type") == "tool_use" for c in m.get("message", {}).get("content", []))]
        assert len(tool_uses) >= 1

    async def test_reasoning_item_emits_thinking_block(self, adapter):
        msgs = self.collect_emitted(adapter)
        adapter._handle_notification("item/started", {
            "item": {"type": "reasoning", "id": "r-1", "summary": "thinking..."},
        })
        adapter._handle_notification("item/completed", {
            "item": {"type": "reasoning", "id": "r-1", "summary": "I should do X"},
        })
        thinking = [m for m in msgs if m.get("type") == "assistant"
                    and any(c.get("type") == "thinking" for c in m.get("message", {}).get("content", []))]
        assert len(thinking) == 1


# ── CodexAdapter Approval Tests ──────────────────────────────��──────────────


class TestCodexAdapterApprovals:
    """Permission request/response flow."""

    @pytest.fixture
    async def adapter(self):
        proc = make_mock_proc()
        a = CodexAdapter(proc, "test-session-3", CodexAdapterOptions(
            model="gpt-5.5", cwd="/code", approval_mode="suggest",
        ))
        a._init_task.cancel()
        a._exit_task.cancel()
        a._connected = True
        a._initialized = True
        a._thread_id = "thread-1"
        yield a

    async def test_command_approval_emits_permission_request(self, adapter):
        msgs: list[dict] = []
        adapter.on_browser_message(lambda m: msgs.append(m))
        adapter._handle_request("item/commandExecution/requestApproval", 10, {
            "command": ["rm", "-rf", "/"],
            "reason": "Dangerous command",
        })
        perms = [m for m in msgs if m.get("type") == "permission_request"]
        assert len(perms) == 1
        assert perms[0]["request"]["tool_name"] == "Bash"
        assert "rm -rf /" in perms[0]["request"]["input"]["command"]

    async def test_file_change_approval_emits_permission_request(self, adapter):
        msgs: list[dict] = []
        adapter.on_browser_message(lambda m: msgs.append(m))
        adapter._handle_request("item/fileChange/requestApproval", 11, {
            "changes": [{"path": "/code/main.py", "kind": "edit"}],
        })
        perms = [m for m in msgs if m.get("type") == "permission_request"]
        assert len(perms) == 1
        assert perms[0]["request"]["tool_name"] == "Edit"

    async def test_permission_response_allow_sends_accept(self, adapter):
        # Simulate approval request
        adapter._handle_request("item/commandExecution/requestApproval", 20, {
            "command": "ls",
        })
        # Find the request_id
        req_id = list(adapter._pending_approvals.keys())[0]
        # Send allow response
        await adapter._handle_outgoing_permission_response({
            "request_id": req_id,
            "behavior": "allow",
        })
        # Should have sent respond with accept
        written = adapter._transport._stdin.write.call_args[0][0].decode()
        response = json.loads(written)
        assert response["result"]["decision"] == "accept"

    async def test_permission_response_deny_sends_decline(self, adapter):
        adapter._handle_request("item/commandExecution/requestApproval", 21, {
            "command": "dangerous",
        })
        req_id = list(adapter._pending_approvals.keys())[0]
        await adapter._handle_outgoing_permission_response({
            "request_id": req_id,
            "behavior": "deny",
        })
        written = adapter._transport._stdin.write.call_args[0][0].decode()
        response = json.loads(written)
        assert response["result"]["decision"] == "decline"


# ── WsBridge attach_adapter Integration Tests ───────────────────────────────


class TestWsBridgeAdapterIntegration:
    """Test that WsBridge correctly manages adapter-based sessions."""

    @pytest.fixture
    def bridge(self):
        b = WsBridge()
        b._broadcast_to_browsers = AsyncMock()
        b._push_to_native_clients = AsyncMock()
        b._notify_ring0_state_change = AsyncMock()
        return b

    @pytest.fixture
    def mock_adapter(self):
        adapter = MagicMock()
        adapter.is_connected = MagicMock(return_value=True)
        adapter.connected = True
        adapter.send_browser_message = MagicMock(return_value=True)
        adapter.disconnect = AsyncMock()
        # Capture callbacks registered by attach_adapter
        adapter._callbacks = {}

        def capture_on_browser_message(cb):
            adapter._callbacks["on_browser_message"] = cb
        def capture_on_session_meta(cb):
            adapter._callbacks["on_session_meta"] = cb
        def capture_on_disconnect(cb):
            adapter._callbacks["on_disconnect"] = cb

        adapter.on_browser_message = capture_on_browser_message
        adapter.on_session_meta = capture_on_session_meta
        adapter.on_disconnect = capture_on_disconnect
        adapter.on_init_error = MagicMock()
        return adapter

    def test_attach_sets_session_properties(self, bridge, mock_adapter):
        bridge.attach_adapter("s1", mock_adapter, "codex")
        session = bridge._sessions["s1"]
        assert session.adapter is mock_adapter
        assert session.backend_type == "codex"
        assert session.state["backend_type"] == "codex"

    def test_is_cli_connected_returns_true_for_attached_adapter(self, bridge, mock_adapter):
        bridge.attach_adapter("s1", mock_adapter, "codex")
        assert bridge.is_cli_connected("s1") is True

    def test_is_cli_connected_returns_false_when_no_adapter(self, bridge):
        bridge.get_or_create_session("s1", "codex")
        assert bridge.is_cli_connected("s1") is False

    async def test_assistant_message_sets_is_running(self, bridge, mock_adapter):
        bridge.attach_adapter("s1", mock_adapter, "codex")
        session = bridge._sessions["s1"]
        assert session.state.get("is_running") is not True

        cb = mock_adapter._callbacks["on_browser_message"]
        await cb({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}})
        assert session.state["is_running"] is True
        bridge._notify_ring0_state_change.assert_called()

    async def test_result_message_clears_is_running(self, bridge, mock_adapter):
        bridge.attach_adapter("s1", mock_adapter, "codex")
        session = bridge._sessions["s1"]
        session.state["is_running"] = True

        cb = mock_adapter._callbacks["on_browser_message"]
        await cb({"type": "result", "data": {"is_error": False, "total_cost_usd": 0.05, "num_turns": 3}})
        assert session.state["is_running"] is False
        assert session.state["total_cost_usd"] == 0.05
        assert session.state["num_turns"] == 3

    async def test_permission_request_sets_waiting_state(self, bridge, mock_adapter):
        bridge.attach_adapter("s1", mock_adapter, "codex")
        session = bridge._sessions["s1"]
        session.state["is_running"] = True

        cb = mock_adapter._callbacks["on_browser_message"]
        await cb({
            "type": "permission_request",
            "request": {"request_id": "perm-1", "tool_name": "Bash", "description": "run ls"},
        })
        assert session.state["is_waiting_for_permission"] is True
        assert "perm-1" in session.pending_permissions

    async def test_assistant_auto_clears_pending_permissions(self, bridge, mock_adapter):
        bridge.attach_adapter("s1", mock_adapter, "codex")
        session = bridge._sessions["s1"]
        session.pending_permissions["old-perm"] = {"request_id": "old-perm"}
        session.state["is_waiting_for_permission"] = True

        cb = mock_adapter._callbacks["on_browser_message"]
        await cb({"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}})
        assert len(session.pending_permissions) == 0
        assert session.state["is_waiting_for_permission"] is False

    async def test_result_auto_clears_pending_permissions(self, bridge, mock_adapter):
        bridge.attach_adapter("s1", mock_adapter, "codex")
        session = bridge._sessions["s1"]
        session.pending_permissions["perm-x"] = {"request_id": "perm-x"}

        cb = mock_adapter._callbacks["on_browser_message"]
        await cb({"type": "result", "data": {"is_error": False}})
        assert len(session.pending_permissions) == 0

    async def test_messages_appended_to_history(self, bridge, mock_adapter):
        bridge.attach_adapter("s1", mock_adapter, "codex")
        session = bridge._sessions["s1"]
        cb = mock_adapter._callbacks["on_browser_message"]

        await cb({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}})
        await cb({"type": "result", "data": {"is_error": False}})
        assert len(session.message_history) == 2

    async def test_session_init_updates_state(self, bridge, mock_adapter):
        bridge.attach_adapter("s1", mock_adapter, "codex")
        session = bridge._sessions["s1"]
        cb = mock_adapter._callbacks["on_browser_message"]

        await cb({"type": "session_init", "session": {"model": "gpt-5.5", "cwd": "/code/moonbeam"}})
        assert session.state["model"] == "gpt-5.5"
        assert session.state["cwd"] == "/code/moonbeam"
        assert session.state["backend_type"] == "codex"

    async def test_disconnect_clears_adapter_and_notifies(self, bridge, mock_adapter):
        bridge.attach_adapter("s1", mock_adapter, "codex")
        session = bridge._sessions["s1"]
        session.state["is_running"] = True

        cb = mock_adapter._callbacks["on_disconnect"]
        await cb()
        assert session.adapter is None
        assert session.state["is_running"] is False
        bridge._broadcast_to_browsers.assert_any_call(session, {"type": "cli_disconnected"})

    def test_session_meta_updates_state(self, bridge, mock_adapter):
        bridge.attach_adapter("s1", mock_adapter, "codex")
        session = bridge._sessions["s1"]
        cb = mock_adapter._callbacks["on_session_meta"]

        cb({"cliSessionId": "thread-abc", "model": "gpt-5.5", "cwd": "/new/path"})
        assert session.state["model"] == "gpt-5.5"
        assert session.state["cwd"] == "/new/path"

    async def test_queued_messages_flushed_on_attach(self, bridge, mock_adapter):
        session = bridge.get_or_create_session("s1", "codex")
        session.pending_messages = [json.dumps({"type": "user_message", "content": "hello"})]

        bridge.attach_adapter("s1", mock_adapter, "codex")
        mock_adapter.send_browser_message.assert_called_once()

    async def test_status_change_tracks_compacting(self, bridge, mock_adapter):
        bridge.attach_adapter("s1", mock_adapter, "codex")
        session = bridge._sessions["s1"]
        cb = mock_adapter._callbacks["on_browser_message"]

        await cb({"type": "status_change", "status": "compacting"})
        assert session.state["is_compacting"] is True

        await cb({"type": "status_change", "status": None})
        assert session.state["is_compacting"] is False
