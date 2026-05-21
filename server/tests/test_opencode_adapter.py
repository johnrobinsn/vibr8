"""Tests for the OpenCode adapter.

Covers: adapter lifecycle, message translation, state management,
permission flow, error handling, and Adapter Guide compliance.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vibr8_core.opencode_adapter import OpenCodeAdapter, OpenCodeAdapterOptions


# ── Helpers ──────────────────────────────────────────────────────────────────


def make_adapter(
    session_id: str = "test-session",
    model: str = "opencode/gpt-5.5",
    cwd: str = "/code",
    connected: bool = False,
    initialized: bool = False,
    opencode_session_id: str | None = None,
) -> OpenCodeAdapter:
    """Create an adapter with init task cancelled (for unit testing)."""
    with patch("vibr8_core.opencode_adapter.asyncio.create_task") as mock_ct:
        mock_ct.return_value = MagicMock()
        a = OpenCodeAdapter(session_id, OpenCodeAdapterOptions(
            model=model,
            cwd=cwd,
            server_url="http://127.0.0.1:9999",
            password="test-password",
            approval_mode="bypassPermissions",
        ))
    a._init_task.cancel = MagicMock()
    a._connected = connected
    a._initialized = initialized
    a._opencode_session_id = opencode_session_id
    return a


def make_ready_adapter(**kwargs: Any) -> OpenCodeAdapter:
    """Create an adapter in ready state (connected + initialized)."""
    return make_adapter(
        connected=True,
        initialized=True,
        opencode_session_id="ses_test123",
        **kwargs,
    )


def collect_emitted(adapter: OpenCodeAdapter) -> list[dict]:
    msgs: list[dict] = []
    adapter.on_browser_message(lambda m: msgs.append(m))
    return msgs


# ── Interface Compliance Tests ───────────────────────────────────────────────


class TestOpenCodeAdapterInterface:
    """Verify the adapter implements the interface WsBridge expects."""

    @pytest.fixture
    def adapter(self):
        return make_adapter()

    def test_is_connected_method_exists(self, adapter):
        assert hasattr(adapter, "is_connected")
        assert callable(adapter.is_connected)

    def test_is_connected_returns_bool(self, adapter):
        assert adapter.is_connected() is False

    def test_connected_property(self, adapter):
        assert adapter.connected is False
        adapter._connected = True
        assert adapter.connected is True
        assert adapter.is_connected() is True

    def test_connected_property_matches_method(self, adapter):
        adapter._connected = True
        assert adapter.connected == adapter.is_connected()
        adapter._connected = False
        assert adapter.connected == adapter.is_connected()

    def test_on_browser_message_callback(self, adapter):
        cb = MagicMock()
        adapter.on_browser_message(cb)
        assert adapter._browser_message_cb is cb

    def test_on_session_meta_callback(self, adapter):
        cb = MagicMock()
        adapter.on_session_meta(cb)
        assert adapter._session_meta_cb is cb

    def test_on_disconnect_callback(self, adapter):
        cb = MagicMock()
        adapter.on_disconnect(cb)
        assert adapter._disconnect_cb is cb

    def test_on_init_error_callback(self, adapter):
        cb = MagicMock()
        adapter.on_init_error(cb)
        assert adapter._init_error_cb is cb


# ── Message Queuing Tests ────────────────────────────────────────────────────


class TestOpenCodeAdapterQueuing:
    """Message queuing before initialization completes."""

    @pytest.fixture
    def adapter(self):
        return make_adapter()

    def test_user_message_queued_when_not_initialized(self, adapter):
        msg = {"type": "user_message", "content": "hello"}
        result = adapter.send_browser_message(msg)
        assert result is True
        assert len(adapter._pending_outgoing) == 1

    def test_permission_response_queued_when_not_initialized(self, adapter):
        msg = {"type": "permission_response", "request_id": "r1", "behavior": "allow"}
        result = adapter.send_browser_message(msg)
        assert result is True
        assert len(adapter._pending_outgoing) == 1

    def test_set_model_dropped_when_not_connected(self, adapter):
        msg = {"type": "set_model", "model": "gpt-4"}
        result = adapter.send_browser_message(msg)
        assert result is False
        assert len(adapter._pending_outgoing) == 0

    def test_set_permission_mode_dropped_when_not_connected(self, adapter):
        msg = {"type": "set_permission_mode", "mode": "plan"}
        result = adapter.send_browser_message(msg)
        assert result is False

    def test_interrupt_dropped_when_not_connected(self, adapter):
        msg = {"type": "interrupt"}
        result = adapter.send_browser_message(msg)
        assert result is False

    def test_multiple_messages_queued(self, adapter):
        adapter.send_browser_message({"type": "user_message", "content": "a"})
        adapter.send_browser_message({"type": "user_message", "content": "b"})
        assert len(adapter._pending_outgoing) == 2


# ── Message Dispatch Tests ───────────────────────────────────────────────────


class TestOpenCodeAdapterDispatch:
    """Verify message dispatch when adapter is initialized."""

    @pytest.fixture
    def adapter(self):
        return make_ready_adapter()

    async def test_user_message_accepted(self, adapter):
        result = adapter.send_browser_message({"type": "user_message", "content": "test"})
        assert result is True

    async def test_permission_response_accepted(self, adapter):
        adapter._pending_approvals["r1"] = "oc-perm-1"
        result = adapter.send_browser_message({
            "type": "permission_response",
            "request_id": "r1",
            "behavior": "allow",
        })
        assert result is True

    async def test_interrupt_accepted(self, adapter):
        result = adapter.send_browser_message({"type": "interrupt"})
        assert result is True

    def test_set_model_rejected(self, adapter):
        result = adapter.send_browser_message({"type": "set_model", "model": "gpt-5"})
        assert result is False

    def test_set_permission_mode_rejected(self, adapter):
        result = adapter.send_browser_message({"type": "set_permission_mode", "mode": "plan"})
        assert result is False

    def test_unknown_message_type_rejected(self, adapter):
        result = adapter.send_browser_message({"type": "nonsense"})
        assert result is False


# ── SSE Event Translation Tests ──────────────────────────────────────────────


class TestOpenCodeAdapterMessages:
    """Verify SSE events are correctly translated to browser messages."""

    @pytest.fixture
    def adapter(self):
        a = make_ready_adapter()
        return a

    def test_part_delta_text_emits_stream_event(self, adapter):
        msgs = collect_emitted(adapter)
        adapter._dispatch_sse_event({
            "type": "message.part.delta",
            "properties": {"sessionID": "ses_test123", "partID": "p1", "delta": "Hello "},
        })
        assert len(msgs) == 1
        assert msgs[0]["type"] == "stream_event"
        assert msgs[0]["event"]["type"] == "content_block_delta"
        assert msgs[0]["event"]["delta"]["text"] == "Hello "
        assert adapter._streaming_text == "Hello "

    def test_part_delta_accumulates_text(self, adapter):
        msgs = collect_emitted(adapter)
        adapter._dispatch_sse_event({
            "type": "message.part.delta",
            "properties": {"sessionID": "ses_test123", "partID": "p1", "delta": "Hello "},
        })
        adapter._dispatch_sse_event({
            "type": "message.part.delta",
            "properties": {"sessionID": "ses_test123", "partID": "p1", "delta": "world"},
        })
        assert adapter._streaming_text == "Hello world"
        assert len(msgs) == 2

    def test_part_delta_reasoning_emits_thinking_delta(self, adapter):
        adapter._part_types["p1"] = "reasoning"
        msgs = collect_emitted(adapter)
        adapter._dispatch_sse_event({
            "type": "message.part.delta",
            "properties": {"sessionID": "ses_test123", "partID": "p1", "delta": "Let me think..."},
        })
        assert msgs[0]["event"]["delta"]["type"] == "thinking_delta"
        assert msgs[0]["event"]["delta"]["thinking"] == "Let me think..."

    def test_part_updated_tool_pending_emits_tool_use(self, adapter):
        msgs = collect_emitted(adapter)
        adapter._dispatch_sse_event({
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_test123",
                "part": {
                    "type": "tool",
                    "id": "t1",
                    "callID": "call-1",
                    "tool": "Bash",
                    "state": {"status": "pending", "input": {"command": "ls"}},
                },
            },
        })
        tool_uses = [m for m in msgs if m.get("type") == "assistant"
                     and any(c.get("type") == "tool_use" for c in m.get("message", {}).get("content", []))]
        assert len(tool_uses) >= 1
        assert tool_uses[0]["message"]["content"][0]["name"] == "Bash"
        assert tool_uses[0]["message"]["content"][0]["input"] == {"command": "ls"}

    def test_part_updated_tool_completed_emits_result(self, adapter):
        msgs = collect_emitted(adapter)
        adapter._emitted_tool_use_ids.add("call-1")
        adapter._dispatch_sse_event({
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_test123",
                "part": {
                    "type": "tool",
                    "id": "t1",
                    "callID": "call-1",
                    "tool": "Bash",
                    "state": {"status": "completed", "input": {}, "output": "file.txt"},
                },
            },
        })
        tool_results = [m for m in msgs if m.get("type") == "assistant"
                        and any(c.get("type") == "tool_result" for c in m.get("message", {}).get("content", []))]
        assert len(tool_results) == 1
        assert tool_results[0]["message"]["content"][0]["content"] == "file.txt"

    def test_part_updated_tool_error_emits_error_result(self, adapter):
        msgs = collect_emitted(adapter)
        adapter._emitted_tool_use_ids.add("call-2")
        adapter._dispatch_sse_event({
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_test123",
                "part": {
                    "type": "tool",
                    "id": "t2",
                    "callID": "call-2",
                    "tool": "Bash",
                    "state": {"status": "error", "input": {}, "error": "command not found"},
                },
            },
        })
        tool_results = [m for m in msgs if m.get("type") == "assistant"
                        and any(c.get("type") == "tool_result" for c in m.get("message", {}).get("content", []))]
        assert len(tool_results) == 1
        assert tool_results[0]["message"]["content"][0]["is_error"] is True

    def test_part_updated_reasoning_emits_thinking_start(self, adapter):
        msgs = collect_emitted(adapter)
        adapter._dispatch_sse_event({
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_test123",
                "part": {"type": "reasoning", "id": "r1", "text": "Let me consider..."},
            },
        })
        thinking_starts = [m for m in msgs if m.get("type") == "stream_event"
                           and m.get("event", {}).get("type") == "content_block_start"
                           and m.get("event", {}).get("content_block", {}).get("type") == "thinking"]
        assert len(thinking_starts) == 1

    def test_part_updated_compaction_emits_status_change(self, adapter):
        msgs = collect_emitted(adapter)
        adapter._dispatch_sse_event({
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_test123",
                "part": {"type": "compaction", "id": "c1"},
            },
        })
        assert msgs[0]["type"] == "status_change"
        assert msgs[0]["status"] == "compacting"

    def test_message_updated_emits_assistant_and_result(self, adapter):
        msgs = collect_emitted(adapter)
        adapter._streaming_text = "Hello world"
        adapter._dispatch_sse_event({
            "type": "message.updated",
            "properties": {
                "sessionID": "ses_test123",
                "info": {
                    "role": "assistant",
                    "cost": 0.05,
                    "finish": "end_turn",
                    "tokens": {"input": 100, "output": 50, "cache": {"write": 0, "read": 0}},
                },
            },
        })
        assistant_msgs = [m for m in msgs if m.get("type") == "assistant"]
        result_msgs = [m for m in msgs if m.get("type") == "result"]
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0]["message"]["content"][0]["text"] == "Hello world"
        assert len(result_msgs) == 1
        assert result_msgs[0]["data"]["is_error"] is False
        assert result_msgs[0]["data"]["total_cost_usd"] == 0.05

    def test_message_updated_resets_streaming_state(self, adapter):
        adapter._streaming_text = "Hello"
        adapter._part_types = {"p1": "text"}
        adapter._emitted_tool_use_ids = {"call-1"}
        adapter._dispatch_sse_event({
            "type": "message.updated",
            "properties": {
                "sessionID": "ses_test123",
                "info": {"role": "assistant", "finish": "end_turn", "tokens": {}},
            },
        })
        assert adapter._streaming_text == ""
        assert len(adapter._part_types) == 0
        assert len(adapter._emitted_tool_use_ids) == 0

    def test_message_updated_no_text_still_emits_result(self, adapter):
        msgs = collect_emitted(adapter)
        adapter._streaming_text = ""
        adapter._dispatch_sse_event({
            "type": "message.updated",
            "properties": {
                "sessionID": "ses_test123",
                "info": {"role": "assistant", "finish": "end_turn", "tokens": {}},
            },
        })
        assistant_msgs = [m for m in msgs if m.get("type") == "assistant"]
        result_msgs = [m for m in msgs if m.get("type") == "result"]
        assert len(assistant_msgs) == 0
        assert len(result_msgs) == 1

    def test_message_updated_error_sets_is_error(self, adapter):
        msgs = collect_emitted(adapter)
        adapter._dispatch_sse_event({
            "type": "message.updated",
            "properties": {
                "sessionID": "ses_test123",
                "info": {
                    "role": "assistant",
                    "error": {"name": "SomeError", "data": {"message": "boom"}},
                    "tokens": {},
                },
            },
        })
        result_msgs = [m for m in msgs if m.get("type") == "result"]
        assert result_msgs[0]["data"]["is_error"] is True
        assert result_msgs[0]["data"]["subtype"] == "error_during_execution"

    def test_message_updated_abort_error_not_treated_as_error(self, adapter):
        msgs = collect_emitted(adapter)
        adapter._dispatch_sse_event({
            "type": "message.updated",
            "properties": {
                "sessionID": "ses_test123",
                "info": {
                    "role": "assistant",
                    "error": {"name": "MessageAbortedError"},
                    "tokens": {},
                },
            },
        })
        result_msgs = [m for m in msgs if m.get("type") == "result"]
        assert result_msgs[0]["data"]["is_error"] is False


# ── Permission Flow Tests ────────────────────────────────────────────────────


class TestOpenCodeAdapterPermissions:
    """Permission request/response flow."""

    @pytest.fixture
    def adapter(self):
        return make_ready_adapter()

    def test_permission_asked_emits_permission_request(self, adapter):
        msgs = collect_emitted(adapter)
        adapter._dispatch_sse_event({
            "type": "permission.asked",
            "properties": {
                "sessionID": "ses_test123",
                "id": "oc-perm-1",
                "permission": "shell",
                "metadata": {"command": "rm -rf /tmp/test"},
                "tool": {"callID": "call-1"},
            },
        })
        perm_msgs = [m for m in msgs if m.get("type") == "permission_request"]
        assert len(perm_msgs) == 1
        req = perm_msgs[0]["request"]
        assert req["tool_name"] == "shell"
        assert req["input"]["command"] == "rm -rf /tmp/test"
        assert "request_id" in req

    def test_permission_asked_stores_mapping(self, adapter):
        adapter._dispatch_sse_event({
            "type": "permission.asked",
            "properties": {
                "sessionID": "ses_test123",
                "id": "oc-perm-1",
                "permission": "shell",
                "metadata": {},
            },
        })
        assert len(adapter._pending_approvals) == 1
        request_id = list(adapter._pending_approvals.keys())[0]
        assert adapter._pending_approvals[request_id] == "oc-perm-1"

    def test_permission_asked_with_patterns_fallback(self, adapter):
        msgs = collect_emitted(adapter)
        adapter._dispatch_sse_event({
            "type": "permission.asked",
            "properties": {
                "sessionID": "ses_test123",
                "id": "oc-perm-2",
                "permission": "file_write",
                "metadata": None,
                "patterns": ["/tmp/test.txt"],
            },
        })
        req = msgs[0]["request"]
        assert "/tmp/test.txt" in req["description"]


# ── Tool Deduplication Tests ─────────────────────────────────────────────────


class TestOpenCodeAdapterToolDedup:
    """Tool use deduplication (same pattern as CodexAdapter)."""

    @pytest.fixture
    def adapter(self):
        return make_ready_adapter()

    def test_tool_use_tracked_on_start(self, adapter):
        adapter._dispatch_sse_event({
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_test123",
                "part": {
                    "type": "tool",
                    "id": "t1",
                    "callID": "call-1",
                    "tool": "Bash",
                    "state": {"status": "pending", "input": {}},
                },
            },
        })
        assert "call-1" in adapter._emitted_tool_use_ids

    def test_duplicate_tool_use_not_emitted(self, adapter):
        msgs = collect_emitted(adapter)
        adapter._emitted_tool_use_ids.add("call-1")
        adapter._dispatch_sse_event({
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_test123",
                "part": {
                    "type": "tool",
                    "id": "t1",
                    "callID": "call-1",
                    "tool": "Bash",
                    "state": {"status": "completed", "input": {}, "output": "ok"},
                },
            },
        })
        tool_uses = [m for m in msgs if m.get("type") == "assistant"
                     and any(c.get("type") == "tool_use" for c in m.get("message", {}).get("content", []))]
        assert len(tool_uses) == 0

    def test_backfill_tool_use_if_start_missed(self, adapter):
        msgs = collect_emitted(adapter)
        adapter._dispatch_sse_event({
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_test123",
                "part": {
                    "type": "tool",
                    "id": "t1",
                    "callID": "call-new",
                    "tool": "Read",
                    "state": {"status": "completed", "input": {"path": "/file"}, "output": "contents"},
                },
            },
        })
        tool_uses = [m for m in msgs if m.get("type") == "assistant"
                     and any(c.get("type") == "tool_use" for c in m.get("message", {}).get("content", []))]
        assert len(tool_uses) == 1
        assert tool_uses[0]["message"]["content"][0]["name"] == "Read"

    def test_emitted_tool_ids_cleared_on_turn_completion(self, adapter):
        adapter._emitted_tool_use_ids = {"call-1", "call-2"}
        adapter._dispatch_sse_event({
            "type": "message.updated",
            "properties": {
                "sessionID": "ses_test123",
                "info": {"role": "assistant", "finish": "end_turn", "tokens": {}},
            },
        })
        assert len(adapter._emitted_tool_use_ids) == 0


# ── Session Status Tests ─────────────────────────────────────────────────────


class TestOpenCodeAdapterSessionStatus:
    """Session status events (busy, error)."""

    @pytest.fixture
    def adapter(self):
        return make_ready_adapter()

    def test_session_status_busy_emits_message_start(self, adapter):
        msgs = collect_emitted(adapter)
        adapter._dispatch_sse_event({
            "type": "session.status",
            "properties": {
                "sessionID": "ses_test123",
                "status": {"type": "busy"},
            },
        })
        stream_events = [m for m in msgs if m.get("type") == "stream_event"]
        event_types = [m["event"]["type"] for m in stream_events]
        assert "message_start" in event_types
        assert "content_block_start" in event_types

    def test_session_error_emits_error(self, adapter):
        msgs = collect_emitted(adapter)
        adapter._dispatch_sse_event({
            "type": "session.error",
            "properties": {
                "sessionID": "ses_test123",
                "error": {"data": {"message": "rate limit exceeded"}},
            },
        })
        error_msgs = [m for m in msgs if m.get("type") == "error"]
        assert len(error_msgs) == 1
        assert "rate limit" in error_msgs[0]["message"]


# ── Session Filtering Tests ──────────────────────────────────────────────────


class TestOpenCodeAdapterSessionFilter:
    """Events for other sessions are ignored."""

    @pytest.fixture
    def adapter(self):
        return make_ready_adapter()

    def test_events_from_other_session_ignored(self, adapter):
        msgs = collect_emitted(adapter)
        adapter._dispatch_sse_event({
            "type": "message.part.delta",
            "properties": {"sessionID": "other-session", "partID": "p1", "delta": "nope"},
        })
        assert len(msgs) == 0
        assert adapter._streaming_text == ""

    def test_events_without_session_id_processed(self, adapter):
        msgs = collect_emitted(adapter)
        adapter._dispatch_sse_event({
            "type": "session.error",
            "properties": {"error": "something broke"},
        })
        assert len(msgs) == 1


# ── Disconnect Tests ─────────────────────────────────────────────────────────


class TestOpenCodeAdapterDisconnect:
    """Disconnect and cleanup behavior."""

    @pytest.fixture
    def adapter(self):
        a = make_ready_adapter()
        a._http = MagicMock()
        a._http.closed = False
        a._http.close = AsyncMock()
        return a

    async def test_disconnect_sets_connected_false(self, adapter):
        await adapter.disconnect()
        assert adapter.connected is False

    async def test_disconnect_closes_http(self, adapter):
        await adapter.disconnect()
        adapter._http.close.assert_called_once()

    async def test_disconnect_cancels_sse_task(self, adapter):
        sse_task = MagicMock()
        sse_task.done.return_value = False
        adapter._sse_task = sse_task
        await adapter.disconnect()
        sse_task.cancel.assert_called_once()

    async def test_disconnect_calls_disconnect_cb(self, adapter):
        cb = MagicMock(return_value=None)
        adapter.on_disconnect(cb)
        await adapter.disconnect()
        cb.assert_called_once()

    async def test_disconnect_calls_async_disconnect_cb(self, adapter):
        cb = AsyncMock()
        adapter.on_disconnect(cb)
        await adapter.disconnect()
        cb.assert_called_once()

    async def test_disconnect_cancels_init_task(self, adapter):
        init_task = MagicMock()
        init_task.done.return_value = False
        adapter._init_task = init_task
        await adapter.disconnect()
        init_task.cancel.assert_called_once()


# ── Emit Helper Tests ────────────────────────────────────────────────────────


class TestOpenCodeAdapterEmit:
    """_emit handles sync and async callbacks correctly."""

    @pytest.fixture
    def adapter(self):
        return make_ready_adapter()

    def test_emit_calls_sync_callback(self, adapter):
        cb = MagicMock()
        adapter.on_browser_message(cb)
        adapter._emit({"type": "test"})
        cb.assert_called_once_with({"type": "test"})

    def test_emit_no_callback_no_error(self, adapter):
        adapter._emit({"type": "test"})

    def test_emit_handles_async_callback(self, adapter):
        cb = AsyncMock()
        adapter.on_browser_message(cb)
        with patch("vibr8_core.opencode_adapter.asyncio.ensure_future") as mock_ef:
            adapter._emit({"type": "test"})
            mock_ef.assert_called_once()


# ── Server Instance Disposed Test ────────────────────────────────────────────


class TestOpenCodeAdapterServerDisposed:

    @pytest.fixture
    def adapter(self):
        a = make_ready_adapter()
        a._http = MagicMock()
        a._http.closed = False
        a._http.close = AsyncMock()
        return a

    def test_server_disposed_triggers_disconnect(self, adapter):
        with patch("vibr8_core.opencode_adapter.asyncio.create_task") as mock_ct:
            adapter._dispatch_sse_event({
                "type": "server.instance.disposed",
                "properties": {},
            })
            mock_ct.assert_called_once()
