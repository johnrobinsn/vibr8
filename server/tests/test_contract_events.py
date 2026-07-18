"""Tests for contract events/v1 (docs/hub-node-contract-v1.md §B)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from vibr8_core.ws_bridge import WsBridge


def _ring0(session_id: str = "r0-session"):
    ring0 = MagicMock()
    ring0.is_enabled = True
    ring0.session_id = session_id
    return ring0


def _session(session_id: str):
    s = MagicMock()
    s.id = session_id
    return s


async def test_busy_hook_fires_for_ring0_session_transitions():
    bridge = WsBridge()
    bridge._ring0_manager = _ring0("r0")
    calls: list[bool] = []

    async def busy(b: bool) -> None:
        calls.append(b)

    bridge._busy_hook = busy
    bridge._emit_contract_status(_session("r0"), "idle→running")
    bridge._emit_contract_status(_session("r0"), "running→idle")
    await asyncio.sleep(0)
    assert calls == [True, False]


async def test_busy_hook_ignores_non_ring0_sessions():
    bridge = WsBridge()
    bridge._ring0_manager = _ring0("r0")
    calls: list[bool] = []

    async def busy(b: bool) -> None:
        calls.append(b)

    bridge._busy_hook = busy
    bridge._emit_contract_status(_session("other"), "idle→running")
    await asyncio.sleep(0)
    assert calls == []


async def test_attention_hook_fires_on_permission_wait():
    bridge = WsBridge()
    bridge._ring0_manager = _ring0("r0")
    calls: list[tuple[str, dict]] = []

    async def attention(reason: str, **kwargs) -> None:
        # `_attention_hook` grew additive kwargs (severity/context_key/
        # expires_at) so the observer channel can carry a dedup key
        # into the native envelope. Existing single-arg call sites and
        # tests may still ignore them, but this test pins the shape:
        # the wait-for-permission fire supplies severity + contextKey.
        calls.append((reason, kwargs))

    bridge._attention_hook = attention
    bridge._emit_contract_status(_session("any-sess"), "running→waiting_for_permission")
    await asyncio.sleep(0)
    assert len(calls) == 1
    reason, kwargs = calls[0]
    assert "waiting for permission" in reason
    assert kwargs.get("severity") == "warning"
    assert kwargs.get("context_key", "").startswith("perm:")


async def test_no_hooks_is_a_no_op():
    bridge = WsBridge()
    bridge._ring0_manager = _ring0("r0")
    bridge._emit_contract_status(_session("r0"), "idle→running")


def test_flatten_message_text_variants():
    f = WsBridge._flatten_message_text
    assert f("plain") == "plain"
    assert f({"content": "str content"}) == "str content"
    assert f({"content": [{"type": "text", "text": "a"}, {"type": "tool_use"}]}) == "a"
    assert f([{"type": "text", "text": "x"}, {"type": "text", "text": "y"}]) == "x y"
    assert f({"content": 42}) == ""
    assert f(None) == ""


async def test_node_agent_transcript_dispatch():
    from vibr8_node.node_agent import NodeAgent

    agent = NodeAgent("ws://example.invalid", "key", "t")
    agent._ops = MagicMock()
    agent._ops.ring0_input = AsyncMock(return_value={"ok": True})

    result = await agent._dispatch_command(
        "transcript", {"type": "transcript", "text": "hi there", "clientId": "c9"},
    )
    assert result == {"ok": True}
    agent._ops.ring0_input.assert_awaited_once_with(
        text="hi there", source_client_id="c9",
    )


async def test_node_agent_emits_contract_events():
    from vibr8_node.node_agent import NodeAgent

    agent = NodeAgent("ws://example.invalid", "key", "t")
    sent: list[dict] = []

    async def fake_send(payload: dict) -> None:
        sent.append(payload)

    agent._send_to_hub = fake_send
    await agent._emit_speak("hello world")
    await agent._emit_busy(True)
    await agent._emit_attention("needs input")
    assert sent == [
        {"type": "speak", "text": "hello world"},
        {"type": "busy", "busy": True},
        {"type": "attention", "reason": "needs input"},
    ]


async def test_broadcast_voice_preview_targets_ring0_session():
    from vibr8_core.node_operations import NodeOperations

    bridge = MagicMock()
    bridge.send_to_browsers = AsyncMock()
    ring0 = MagicMock()
    ring0.session_id = "r0-sess"
    ops = NodeOperations(
        launcher=MagicMock(), bridge=bridge, store=MagicMock(), ring0=ring0,
    )
    result = await ops.broadcast_voice_preview(transcript="hel")
    assert result == {"ok": True}
    bridge.send_to_browsers.assert_awaited_once_with(
        "r0-sess", {"type": "voice_transcript_preview", "transcript": "hel"},
    )


async def test_broadcast_voice_preview_noop_without_ring0_session():
    from vibr8_core.node_operations import NodeOperations

    bridge = MagicMock()
    bridge.send_to_browsers = AsyncMock()
    ring0 = MagicMock()
    ring0.session_id = ""  # Ring0 enabled but no live session
    ops = NodeOperations(
        launcher=MagicMock(), bridge=bridge, store=MagicMock(), ring0=ring0,
    )
    assert await ops.broadcast_voice_preview(transcript="x") == {"ok": False}
    bridge.send_to_browsers.assert_not_awaited()


async def test_node_agent_dispatches_broadcast_voice_preview():
    """The generic tunnel dispatcher routes the hub's broadcast_voice_preview
    command to the node op (camelCase-free payload → kwargs)."""
    from vibr8_node.node_agent import NodeAgent

    agent = NodeAgent("ws://example.invalid", "key", "t")
    agent._ops = MagicMock()
    agent._ops.broadcast_voice_preview = AsyncMock(return_value={"ok": True})

    result = await agent._dispatch_command(
        "broadcast_voice_preview",
        {"type": "broadcast_voice_preview", "transcript": "live text"},
    )
    assert result == {"ok": True}
    agent._ops.broadcast_voice_preview.assert_awaited_once_with(transcript="live text")
