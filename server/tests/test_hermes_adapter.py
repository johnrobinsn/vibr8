"""Tests for the Hermes ACP adapter.

Covers: adapter lifecycle, init (session/new vs session/load), message
translation, streaming-block bracketing, tool-call progress, available
commands / current mode plumbing, permission flow, model post-init
set_model, and graceful set_permission_mode.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from vibr8_core.hermes_adapter import (
    HermesAdapter,
    HermesAdapterOptions,
    JsonRpcTransport,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def make_mock_proc():
    """Mock asyncio.subprocess.Process with stdio pipes and a never-resolving wait()."""
    proc = MagicMock()
    proc.pid = 23456
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()

    reader = AsyncMock()
    reader.read = AsyncMock(return_value=b"")
    proc.stdout = reader
    proc.stderr = None

    proc.wait = AsyncMock(return_value=0)
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    return proc


def collect(adapter: HermesAdapter) -> list[dict]:
    msgs: list[dict] = []
    adapter.on_browser_message(lambda m: msgs.append(m))
    return msgs


def stream_events(msgs: list[dict]) -> list[dict]:
    return [m["event"] for m in msgs if m.get("type") == "stream_event"]


# ── JsonRpcTransport Tests ──────────────────────────────────────────────────


class TestJsonRpcTransport:
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
        transport._dispatch({"method": "session/update", "params": {"update": {}}})
        handler.assert_called_once_with("session/update", {"update": {}})

    async def test_dispatch_request(self, transport):
        handler = MagicMock()
        transport.on_request(handler)
        transport._dispatch({
            "method": "session/request_permission", "id": 7, "params": {"x": 1},
        })
        handler.assert_called_once_with("session/request_permission", 7, {"x": 1})

    async def test_dispatch_response_resolves_future(self, transport):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        transport._pending[1] = future
        transport._dispatch({"id": 1, "result": {"sessionId": "h-1"}})
        assert future.done()
        assert future.result() == {"sessionId": "h-1"}

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
        transport._buffer = '{"method":"a","params":{}}\n{"meth'
        transport._process_buffer()
        handler.assert_called_once_with("a", {})
        assert transport._buffer == '{"meth'

    async def test_call_sends_jsonrpc_request(self, transport):
        async def fake_drain():
            transport._dispatch({"id": 1, "result": {"ok": True}})

        transport._stdin.drain = fake_drain
        result = await transport.call("initialize", {})
        assert result == {"ok": True}
        written = transport._stdin.write.call_args[0][0].decode()
        msg = json.loads(written)
        assert msg["method"] == "initialize"
        assert msg["jsonrpc"] == "2.0"
        assert msg["id"] == 1

    async def test_notify_sends_without_id(self, transport):
        await transport.notify("notifications/initialized")
        written = transport._stdin.write.call_args[0][0].decode()
        msg = json.loads(written)
        assert "id" not in msg
        assert msg["method"] == "notifications/initialized"

    async def test_respond_sends_response(self, transport):
        await transport.respond(42, {"outcome": {"outcome": "selected", "optionId": "allow"}})
        written = transport._stdin.write.call_args[0][0].decode()
        msg = json.loads(written)
        assert msg["id"] == 42
        assert msg["result"]["outcome"]["optionId"] == "allow"


# ── HermesAdapter Interface Tests ───────────────────────────────────────────


class TestHermesAdapterInterface:
    """Verify the adapter implements the interface WsBridge expects."""

    @pytest.fixture
    async def adapter(self):
        proc = make_mock_proc()
        a = HermesAdapter(proc, "test-session-1", HermesAdapterOptions(
            model="claude-opus-4-20250514", cwd="/code",
        ))
        a._init_task.cancel()
        a._exit_task.cancel()
        yield a

    async def test_is_connected_method_exists(self, adapter):
        assert hasattr(adapter, "is_connected") and callable(adapter.is_connected)

    async def test_connected_property(self, adapter):
        assert adapter.connected is False
        adapter._connected = True
        assert adapter.connected is True
        assert adapter.is_connected() is True

    async def test_callbacks_register(self, adapter):
        bm, sm, dc, ie = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        adapter.on_browser_message(bm)
        adapter.on_session_meta(sm)
        adapter.on_disconnect(dc)
        adapter.on_init_error(ie)
        assert adapter._browser_message_cb is bm
        assert adapter._session_meta_cb is sm
        assert adapter._disconnect_cb is dc
        assert adapter._init_error_cb is ie

    async def test_user_message_queued_when_uninitialized(self, adapter):
        result = adapter.send_browser_message({"type": "user_message", "content": "hi"})
        assert result is True
        assert len(adapter._pending_outgoing) == 1

    async def test_permission_response_queued_when_uninitialized(self, adapter):
        result = adapter.send_browser_message({
            "type": "permission_response", "request_id": "r", "behavior": "deny",
        })
        assert result is True
        assert len(adapter._pending_outgoing) == 1

    async def test_non_queueable_dropped_when_disconnected(self, adapter):
        adapter._connected = False
        result = adapter.send_browser_message({"type": "interrupt"})
        assert result is False

    async def test_hermes_session_id_property(self, adapter):
        assert adapter.hermes_session_id is None
        adapter._hermes_session_id = "h-7"
        assert adapter.hermes_session_id == "h-7"

    async def test_disconnect_terminates_process(self, adapter):
        adapter._connected = True
        await adapter.disconnect()
        assert adapter.connected is False
        adapter._proc.terminate.assert_called_once()


# ── Initialization Tests ────────────────────────────────────────────────────


class TestHermesAdapterInit:
    """Initialization paths: fresh session/new vs resume via session/load,
    plus the post-init session/set_model that applies an explicit model."""

    async def _make_with_recorded_calls(self, options: HermesAdapterOptions, responses: dict[str, Any]):
        """Build an adapter whose transport.call() returns canned responses."""
        proc = make_mock_proc()
        a = HermesAdapter(proc, "sess", options)
        a._init_task.cancel()
        a._exit_task.cancel()
        calls: list[tuple[str, dict]] = []

        async def fake_call(method: str, params: dict | None = None):
            calls.append((method, params or {}))
            return responses.get(method, {})

        a._transport.call = fake_call  # type: ignore[assignment]
        a._transport.notify = AsyncMock()
        return a, calls

    async def test_fresh_session_calls_session_new(self):
        a, calls = await self._make_with_recorded_calls(
            HermesAdapterOptions(cwd="/code", mcp_servers=[{"name": "v"}]),
            {"session/new": {"sessionId": "h-new"}},
        )
        msgs = collect(a)
        await a._initialize()
        methods = [c[0] for c in calls]
        assert "initialize" in methods
        assert "session/new" in methods
        # cwd + mcpServers passed through
        new_params = next(p for m, p in calls if m == "session/new")
        assert new_params["cwd"] == "/code"
        assert new_params["mcpServers"] == [{"name": "v"}]
        assert a._hermes_session_id == "h-new"
        # session_init emitted
        assert any(m.get("type") == "session_init" for m in msgs)

    async def test_resume_calls_session_load(self):
        a, calls = await self._make_with_recorded_calls(
            HermesAdapterOptions(cwd="/code", session_id_to_resume="h-old"),
            {"session/load": {"models": {"current": "gpt-5.5"}}},
        )
        await a._initialize()
        methods = [c[0] for c in calls]
        assert "session/load" in methods
        assert "session/new" not in methods
        load_params = next(p for m, p in calls if m == "session/load")
        assert load_params["sessionId"] == "h-old"
        assert a._hermes_session_id == "h-old"

    async def test_post_init_calls_set_model_when_explicit_model_given(self):
        a, calls = await self._make_with_recorded_calls(
            HermesAdapterOptions(cwd="/code", model="claude-opus-4-20250514"),
            {
                "session/new": {"sessionId": "h-1", "models": {"current": "gpt-5.5"}},
                "session/set_model": {},
            },
        )
        await a._initialize()
        methods = [c[0] for c in calls]
        assert "session/set_model" in methods
        sm_params = next(p for m, p in calls if m == "session/set_model")
        assert sm_params["model"] == "claude-opus-4-20250514"
        assert sm_params["sessionId"] == "h-1"

    async def test_no_set_model_when_model_matches_current(self):
        a, calls = await self._make_with_recorded_calls(
            HermesAdapterOptions(cwd="/code", model="gpt-5.5"),
            {"session/new": {"sessionId": "h-1", "models": {"current": "gpt-5.5"}}},
        )
        await a._initialize()
        methods = [c[0] for c in calls]
        assert "session/set_model" not in methods

    async def test_no_set_model_when_no_model_specified(self):
        a, calls = await self._make_with_recorded_calls(
            HermesAdapterOptions(cwd="/code"),
            {"session/new": {"sessionId": "h-1", "models": {"current": "gpt-5.5"}}},
        )
        await a._initialize()
        methods = [c[0] for c in calls]
        assert "session/set_model" not in methods

    async def test_init_emits_session_init_with_state(self):
        a, _ = await self._make_with_recorded_calls(
            HermesAdapterOptions(cwd="/code", model="gpt-5.5", approval_mode="plan"),
            {"session/new": {"sessionId": "h-1", "models": {"current": "gpt-5.5"}}},
        )
        msgs = collect(a)
        await a._initialize()
        inits = [m for m in msgs if m.get("type") == "session_init"]
        assert len(inits) == 1
        state = inits[0]["session"]
        assert state["backend_type"] == "hermes"
        assert state["model"] == "gpt-5.5"
        assert state["cwd"] == "/code"
        assert state["permissionMode"] == "plan"

    async def test_init_failure_invokes_error_callback(self):
        proc = make_mock_proc()
        a = HermesAdapter(proc, "sess", HermesAdapterOptions(cwd="/code"))
        a._init_task.cancel()
        a._exit_task.cancel()

        async def boom(*_a, **_k):
            raise RuntimeError("init exploded")
        a._transport.call = boom  # type: ignore[assignment]

        err: list[str] = []
        a.on_init_error(lambda e: err.append(e))
        await a._initialize()
        assert err and "init exploded" in err[0]
        assert a.connected is False


# ── Streaming + Bracketing Tests ────────────────────────────────────────────


class TestHermesAdapterStreaming:
    """content_block_start / content_block_stop bracketing, deltas,
    tool-call progress, finish-turn cleanup."""

    @pytest.fixture
    async def adapter(self):
        proc = make_mock_proc()
        a = HermesAdapter(proc, "sess", HermesAdapterOptions(cwd="/code"))
        a._init_task.cancel()
        a._exit_task.cancel()
        a._connected = True
        a._initialized = True
        a._hermes_session_id = "h-1"
        a._is_running = True
        yield a

    async def test_text_chunk_opens_and_streams(self, adapter):
        msgs = collect(adapter)
        adapter._handle_agent_message_chunk({"content": {"type": "text", "text": "hi "}})
        adapter._handle_agent_message_chunk({"content": {"type": "text", "text": "world"}})
        events = stream_events(msgs)
        # one block_start, two deltas
        starts = [e for e in events if e["type"] == "content_block_start"]
        deltas = [e for e in events if e["type"] == "content_block_delta"]
        assert len(starts) == 1
        assert len(deltas) == 2
        assert starts[0]["content_block"]["type"] == "text"
        # streaming accumulator builds the full text
        assert adapter._streaming_text == "hi world"

    async def test_text_then_thinking_closes_and_reopens(self, adapter):
        msgs = collect(adapter)
        adapter._handle_agent_message_chunk({"content": {"type": "text", "text": "txt"}})
        adapter._handle_thought_chunk({"content": {"type": "text", "text": "thinking..."}})
        events = stream_events(msgs)
        kinds = [e["type"] for e in events]
        # start(text), delta(text), stop(text), start(thinking), delta(thinking)
        assert kinds == [
            "content_block_start", "content_block_delta",
            "content_block_stop",
            "content_block_start", "content_block_delta",
        ]
        # thinking block has a different content_block.type
        assert events[3]["content_block"]["type"] == "thinking"

    async def test_tool_call_closes_open_text_block(self, adapter):
        msgs = collect(adapter)
        adapter._handle_agent_message_chunk({"content": {"type": "text", "text": "before"}})
        adapter._handle_tool_call_start({
            "toolCallId": "t1", "title": "Bash", "rawInput": {"command": "ls"},
        })
        events = stream_events(msgs)
        # text start, text delta, text stop (when tool starts), then no new block
        kinds = [e["type"] for e in events]
        assert "content_block_stop" in kinds
        # The text block must close before the tool_use assistant message is emitted
        stop_idx = next(i for i, m in enumerate(msgs)
                        if m.get("type") == "stream_event"
                        and m["event"]["type"] == "content_block_stop")
        tool_idx = next(i for i, m in enumerate(msgs)
                        if m.get("type") == "assistant"
                        and any(c.get("type") == "tool_use" for c in m["message"]["content"]))
        assert stop_idx < tool_idx

    async def test_finish_turn_closes_open_block(self, adapter):
        msgs = collect(adapter)
        adapter._handle_agent_message_chunk({"content": {"type": "text", "text": "tail"}})
        adapter._turn_start_time = 1.0
        adapter._finish_turn()
        events = stream_events(msgs)
        kinds = [e["type"] for e in events]
        assert "content_block_stop" in kinds
        # message_stop must come AFTER the block_stop
        stop_idx = kinds.index("content_block_stop")
        msg_stop_idx = kinds.index("message_stop")
        assert stop_idx < msg_stop_idx

    async def test_finish_turn_clears_tool_state(self, adapter):
        adapter._emitted_tool_use_ids.add("t1")
        adapter._tool_call_last_status["t1"] = "in_progress"
        adapter._turn_start_time = 1.0
        adapter._finish_turn()
        assert adapter._emitted_tool_use_ids == set()
        assert adapter._tool_call_last_status == {}

    async def test_block_indices_match_pairwise(self, adapter):
        msgs = collect(adapter)
        adapter._handle_agent_message_chunk({"content": {"type": "text", "text": "a"}})
        adapter._handle_thought_chunk({"content": {"type": "text", "text": "b"}})
        adapter._handle_agent_message_chunk({"content": {"type": "text", "text": "c"}})
        adapter._turn_start_time = 1.0
        adapter._finish_turn()
        events = stream_events(msgs)
        starts = [e for e in events if e["type"] == "content_block_start"]
        stops = [e for e in events if e["type"] == "content_block_stop"]
        assert [e["index"] for e in starts] == [e["index"] for e in stops]


# ── Tool Call Handling ──────────────────────────────────────────────────────


class TestHermesToolCalls:
    @pytest.fixture
    async def adapter(self):
        proc = make_mock_proc()
        a = HermesAdapter(proc, "sess", HermesAdapterOptions(cwd="/code"))
        a._init_task.cancel()
        a._exit_task.cancel()
        a._connected = True
        a._initialized = True
        a._hermes_session_id = "h-1"
        a._is_running = True
        yield a

    async def test_tool_call_start_emits_tool_use(self, adapter):
        msgs = collect(adapter)
        adapter._handle_tool_call_start({
            "toolCallId": "t1", "title": "Bash", "rawInput": {"command": "ls"},
        })
        uses = [m for m in msgs
                if m.get("type") == "assistant"
                and any(c.get("type") == "tool_use" for c in m["message"]["content"])]
        assert len(uses) == 1
        block = uses[0]["message"]["content"][0]
        assert block["name"] == "Bash"
        assert block["id"] == "t1"
        assert block["input"] == {"command": "ls"}

    async def test_duplicate_tool_call_start_deduped(self, adapter):
        msgs = collect(adapter)
        adapter._handle_tool_call_start({
            "toolCallId": "t1", "title": "Bash", "rawInput": {"command": "ls"},
        })
        adapter._handle_tool_call_start({
            "toolCallId": "t1", "title": "Bash", "rawInput": {"command": "ls"},
        })
        uses = [m for m in msgs
                if m.get("type") == "assistant"
                and any(c.get("type") == "tool_use" for c in m["message"]["content"])]
        assert len(uses) == 1

    async def test_tool_call_update_backfills_missing_start(self, adapter):
        msgs = collect(adapter)
        # Skip start; jump straight to a completed update
        adapter._handle_tool_call_update({
            "toolCallId": "t9", "title": "Edit", "status": "completed",
            "rawInput": {"file": "x"}, "rawOutput": "ok",
        })
        uses = [m for m in msgs
                if m.get("type") == "assistant"
                and any(c.get("type") == "tool_use" for c in m["message"]["content"])]
        results = [m for m in msgs
                   if m.get("type") == "assistant"
                   and any(c.get("type") == "tool_result" for c in m["message"]["content"])]
        assert len(uses) == 1
        assert len(results) == 1

    async def test_tool_call_progress_emits_and_dedups(self, adapter):
        msgs = collect(adapter)
        adapter._handle_tool_call_start({"toolCallId": "t1", "title": "Bash"})
        adapter._handle_tool_call_update({"toolCallId": "t1", "status": "in_progress"})
        adapter._handle_tool_call_update({"toolCallId": "t1", "status": "in_progress"})
        adapter._handle_tool_call_update({"toolCallId": "t1", "status": "pending"})
        progress = [m for m in msgs
                    if m.get("type") == "stream_event"
                    and m["event"]["type"] == "tool_use_progress"]
        assert len(progress) == 2  # in_progress + pending; dup in_progress collapsed
        assert progress[0]["event"]["status"] == "in_progress"
        assert progress[1]["event"]["status"] == "pending"

    async def test_completed_update_emits_tool_result(self, adapter):
        msgs = collect(adapter)
        adapter._handle_tool_call_start({"toolCallId": "t1", "title": "Bash"})
        adapter._handle_tool_call_update({
            "toolCallId": "t1", "status": "completed", "rawOutput": "file.txt\n",
        })
        results = [m for m in msgs
                   if m.get("type") == "assistant"
                   and any(c.get("type") == "tool_result" for c in m["message"]["content"])]
        assert len(results) == 1
        block = results[0]["message"]["content"][0]
        assert block["tool_use_id"] == "t1"
        assert block["content"] == "file.txt\n"
        assert block["is_error"] is False

    async def test_errored_update_marks_is_error(self, adapter):
        msgs = collect(adapter)
        adapter._handle_tool_call_start({"toolCallId": "t1", "title": "Bash"})
        adapter._handle_tool_call_update({
            "toolCallId": "t1", "status": "errored", "rawOutput": "exit 1",
        })
        results = [m for m in msgs
                   if m.get("type") == "assistant"
                   and any(c.get("type") == "tool_result" for c in m["message"]["content"])]
        assert len(results) == 1
        assert results[0]["message"]["content"][0]["is_error"] is True

    async def test_completion_clears_progress_tracking(self, adapter):
        adapter._handle_tool_call_start({"toolCallId": "t1", "title": "Bash"})
        adapter._handle_tool_call_update({"toolCallId": "t1", "status": "in_progress"})
        assert "t1" in adapter._tool_call_last_status
        adapter._handle_tool_call_update({
            "toolCallId": "t1", "status": "completed", "rawOutput": "done",
        })
        assert "t1" not in adapter._tool_call_last_status


# ── Session-update notifications ────────────────────────────────────────────


class TestHermesSessionUpdates:
    @pytest.fixture
    async def adapter(self):
        proc = make_mock_proc()
        a = HermesAdapter(proc, "sess", HermesAdapterOptions(cwd="/code"))
        a._init_task.cancel()
        a._exit_task.cancel()
        a._connected = True
        a._initialized = True
        a._hermes_session_id = "h-1"
        yield a

    async def test_available_commands_update_emits_slash_commands(self, adapter):
        msgs = collect(adapter)
        adapter._handle_session_update({"update": {
            "sessionUpdate": "available_commands_update",
            "availableCommands": [{"name": "/init"}, {"name": "/clear"}, "plain"],
        }})
        updates = [m for m in msgs if m.get("type") == "session_update"]
        assert len(updates) == 1
        assert updates[0]["session"]["slash_commands"] == ["/init", "/clear", "plain"]

    async def test_current_mode_update_emits_permission_mode(self, adapter):
        msgs = collect(adapter)
        adapter._handle_session_update({"update": {
            "sessionUpdate": "current_mode_update",
            "currentMode": {"name": "plan"},
        }})
        updates = [m for m in msgs if m.get("type") == "session_update"]
        assert len(updates) == 1
        assert updates[0]["session"]["permissionMode"] == "plan"

    async def test_current_mode_update_accepts_plain_string(self, adapter):
        msgs = collect(adapter)
        adapter._handle_session_update({"update": {
            "sessionUpdate": "current_mode_update", "mode": "bypassPermissions",
        }})
        updates = [m for m in msgs if m.get("type") == "session_update"]
        assert updates[0]["session"]["permissionMode"] == "bypassPermissions"

    async def test_usage_update_emits_context_used_percent(self, adapter):
        msgs = collect(adapter)
        adapter._handle_session_update({"update": {
            "sessionUpdate": "usage_update",
            "usage": {"totalCostUsd": 0.12},
            "size": 1000, "used": 250,
        }})
        updates = [m for m in msgs if m.get("type") == "session_update"]
        assert len(updates) == 1
        assert updates[0]["session"]["total_cost_usd"] == 0.12
        assert updates[0]["session"]["context_used_percent"] == 25.0

    async def test_unknown_update_type_is_ignored(self, adapter):
        msgs = collect(adapter)
        adapter._handle_session_update({"update": {"sessionUpdate": "totally_new"}})
        assert msgs == []


# ── Permission flow ─────────────────────────────────────────────────────────


class TestHermesPermissions:
    @pytest.fixture
    async def adapter(self):
        proc = make_mock_proc()
        a = HermesAdapter(proc, "sess", HermesAdapterOptions(cwd="/code"))
        a._init_task.cancel()
        a._exit_task.cancel()
        a._connected = True
        a._initialized = True
        a._hermes_session_id = "h-1"
        a._transport.respond = AsyncMock()
        yield a

    async def test_permission_request_emits_permission_request(self, adapter):
        msgs = collect(adapter)
        adapter._handle_permission_request(99, {
            "toolCall": {"title": "Bash", "rawInput": {"command": "rm -rf /"}},
            "options": [{"optionId": "allow", "kind": "allow"}],
        })
        perms = [m for m in msgs if m.get("type") == "permission_request"]
        assert len(perms) == 1
        perm = perms[0]
        assert perm["tool_name"] == "Bash"
        assert perm["input"] == {"command": "rm -rf /"}
        # _acp_options preserved for the response handler
        assert perm["_acp_options"] == [{"optionId": "allow", "kind": "allow"}]
        # The rpc id was stored under the request_id
        assert len(adapter._pending_approvals) == 1
        assert next(iter(adapter._pending_approvals.values())) == 99

    async def test_allow_response_picks_allow_option(self, adapter):
        msgs = collect(adapter)
        adapter._handle_permission_request(99, {
            "toolCall": {"title": "Bash", "rawInput": {"command": "ls"}},
            "options": [
                {"optionId": "deny", "kind": "reject"},
                {"optionId": "allow-once", "kind": "allow"},
                {"optionId": "allow-always", "kind": "allow_always"},
            ],
        })
        req_id = next(iter(adapter._pending_approvals.keys()))
        perm = next(m for m in msgs if m.get("type") == "permission_request")
        await adapter._handle_outgoing_permission_response({
            "request_id": req_id, "behavior": "allow",
            "_acp_options": perm["_acp_options"],
        })
        adapter._transport.respond.assert_awaited_once()
        args = adapter._transport.respond.await_args
        assert args.args[0] == 99
        assert args.args[1]["outcome"]["outcome"] == "selected"
        # Picks the first "allow"-kind option
        assert args.args[1]["outcome"]["optionId"] == "allow-once"

    async def test_deny_response_sends_cancelled(self, adapter):
        adapter._handle_permission_request(50, {
            "toolCall": {"title": "Bash", "rawInput": {"command": "ls"}},
            "options": [],
        })
        req_id = next(iter(adapter._pending_approvals.keys()))
        await adapter._handle_outgoing_permission_response({
            "request_id": req_id, "behavior": "deny",
        })
        args = adapter._transport.respond.await_args
        assert args.args[0] == 50
        assert args.args[1]["outcome"] == {"outcome": "cancelled"}

    async def test_unknown_request_id_is_noop(self, adapter):
        await adapter._handle_outgoing_permission_response({
            "request_id": "nope", "behavior": "allow",
        })
        adapter._transport.respond.assert_not_called()


# ── Outgoing dispatch ───────────────────────────────────────────────────────


class TestHermesOutgoingDispatch:
    @pytest.fixture
    async def adapter(self):
        proc = make_mock_proc()
        a = HermesAdapter(proc, "sess", HermesAdapterOptions(cwd="/code"))
        a._init_task.cancel()
        a._exit_task.cancel()
        a._connected = True
        a._initialized = True
        a._hermes_session_id = "h-1"
        yield a

    async def test_set_permission_mode_is_acknowledged(self, adapter):
        msgs = collect(adapter)
        result = adapter._dispatch_outgoing({"type": "set_permission_mode", "mode": "plan"})
        # New behavior: ack True so the UI doesn't get stuck.
        assert result is True
        # And broadcast a session_update so the badge updates.
        updates = [m for m in msgs if m.get("type") == "session_update"]
        assert len(updates) == 1
        assert updates[0]["session"]["permissionMode"] == "plan"

    async def test_set_permission_mode_without_mode_skips_update(self, adapter):
        msgs = collect(adapter)
        result = adapter._dispatch_outgoing({"type": "set_permission_mode"})
        assert result is True
        assert msgs == []

    async def test_unknown_outgoing_returns_false(self, adapter):
        result = adapter._dispatch_outgoing({"type": "something_new"})
        assert result is False

    async def test_interrupt_sends_session_cancel(self, adapter):
        adapter._transport.notify = AsyncMock()
        await adapter._handle_outgoing_interrupt()
        adapter._transport.notify.assert_awaited_once()
        args = adapter._transport.notify.await_args
        assert args.args[0] == "session/cancel"
        assert args.args[1] == {"sessionId": "h-1"}

    async def test_user_message_resets_streaming_state(self, adapter):
        # Pre-populate state from a previous turn
        adapter._streaming_text = "old"
        adapter._emitted_tool_use_ids.add("old-t")
        adapter._tool_call_last_status["old-t"] = "in_progress"
        adapter._open_block_type = "text"
        adapter._open_block_index = 5

        # Stub the prompt call so we don't actually await stdin
        adapter._transport.call = AsyncMock(return_value={"stopReason": "end_turn"})

        await adapter._handle_outgoing_user_message({"type": "user_message", "content": "hi"})

        # Turn state was reset before the prompt
        assert adapter._emitted_tool_use_ids == set()
        assert adapter._tool_call_last_status == {}
        # The prompt was sent with the right payload
        adapter._transport.call.assert_awaited_once()
        args = adapter._transport.call.await_args
        assert args.args[0] == "session/prompt"
        prompt = args.args[1]["prompt"]
        assert prompt[-1] == {"type": "text", "text": "hi"}
