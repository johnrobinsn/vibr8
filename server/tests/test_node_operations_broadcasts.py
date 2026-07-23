"""Broadcast-correctness tests for NodeOperations methods.

The class of bug these guard against: an ops method takes a session id
(possibly an 8-char prefix from Ring0's list_sessions output), expands
it internally via ``_expand_session_id``, applies the state change on
the *full* id — but the browser-facing broadcast uses the *unexpanded*
input. Browser stores are keyed on full ids, so the update misses.

Every method that broadcasts to browsers as part of a session-scoped
state change must:

1. Expand the incoming session id before touching state.
2. Broadcast using the expanded id (so browser store lookups match).
3. Return the expanded id in its result payload so route handlers
   don't have to redo the expansion.

Tests here pin those invariants for ``rename_session`` and
``set_permission_mode``; any future method with the same shape should
grow a sibling test in this file.

We attach a ``_broadcast_hook`` to WsBridge — it short-circuits the
real WebSocket send path and just captures ``(session_id, msg)`` — so
we don't need to mock aiohttp sockets.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from vibr8_core.node_operations import NodeOperations
from vibr8_core.ws_bridge import Session, WsBridge


# Full and prefix session ids used across the tests. The prefix must be
# ≥ 6 chars for _expand_session_id to attempt prefix resolution
# (shorter strings are passed through unchanged).
FULL_SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PREFIX = FULL_SID[:8]


@pytest.fixture
def bridge_with_session():
    """A WsBridge with a single registered Session plus a broadcast
    capture hook. Returns (bridge, captured_broadcasts)."""
    bridge = WsBridge()
    bridge._sessions[FULL_SID] = Session(id=FULL_SID)

    captured: list[tuple[str, dict[str, Any]]] = []

    async def _hook(session_id: str, msg: dict[str, Any]) -> None:
        captured.append((session_id, msg))

    bridge._broadcast_hook = _hook
    return bridge, captured


@pytest.fixture
def ops(bridge_with_session):
    """NodeOperations wired to the fixture bridge with a fake launcher/store."""
    bridge, _ = bridge_with_session
    launcher = MagicMock()
    launcher.list_sessions.return_value = []
    launcher.get_session.side_effect = lambda sid: None
    launcher.is_alive.return_value = False
    return NodeOperations(
        launcher=launcher,
        bridge=bridge,
        store=MagicMock(),
        ring0=None,
    )


# ── rename_session ──────────────────────────────────────────────────────────


async def test_rename_session_expands_prefix_and_broadcasts_full_id(
    bridge_with_session, ops,
) -> None:
    """Ring0 passes an 8-char prefix. The broadcast must carry the
    full session id so browser stores (keyed on full ids) pick it up.

    Regression: before the fix, `broadcast_name_update` was called from
    the route with the raw caller input — a prefix — so the browser's
    session-name chip never updated on voice-driven renames.
    """
    _, captured = bridge_with_session

    result = await ops.rename_session(session_id=PREFIX, name="new-name")

    assert result["ok"] is True
    assert result["name"] == "new-name"
    # The method itself returns the expanded id so callers don't have
    # to redo the resolution.
    assert result["sessionId"] == FULL_SID

    # Exactly one broadcast was emitted, and its session id is the
    # full id (not the 8-char prefix the caller passed in).
    assert len(captured) == 1, f"expected 1 broadcast, got {len(captured)}"
    broadcast_sid, msg = captured[0]
    assert broadcast_sid == FULL_SID, (
        f"broadcast used {broadcast_sid!r}, expected full id {FULL_SID!r} — "
        "the browser store won't find this key"
    )
    assert msg["type"] == "session_name_update"
    assert msg["name"] == "new-name"
    assert msg["userRenamed"] is True


async def test_rename_session_with_full_id_still_broadcasts_full_id(
    bridge_with_session, ops,
) -> None:
    """Frontend rename hits this method with the full id. Same guarantee."""
    _, captured = bridge_with_session

    result = await ops.rename_session(session_id=FULL_SID, name="ui-rename")

    assert result["sessionId"] == FULL_SID
    assert len(captured) == 1
    assert captured[0][0] == FULL_SID


async def test_rename_session_empty_name_rejected_no_broadcast(
    bridge_with_session, ops,
) -> None:
    _, captured = bridge_with_session

    result = await ops.rename_session(session_id=PREFIX, name="   ")

    assert "error" in result
    # No state change → no broadcast leak.
    assert captured == []


# ── set_permission_mode ─────────────────────────────────────────────────────


async def test_set_permission_mode_expands_prefix_and_broadcasts_full_id(
    bridge_with_session, ops,
) -> None:
    """Ring0's set_session_mode passes a prefix. The route depends on
    the ops method to expand + broadcast under the full id.

    Regression: before the fix, the route did ``ws_bridge.get_session(prefix)``
    which returns None (dict lookup with prefix key misses), and the
    ``session_update`` broadcast never fired — the composer mode chip
    stayed stale on voice/MCP mode changes.
    """
    _, captured = bridge_with_session

    result = await ops.set_permission_mode(session_id=PREFIX, mode="plan")

    assert result["ok"] is True
    assert result["mode"] == "plan"
    assert result["sessionId"] == FULL_SID

    # Broadcast targeted the full-id session and carries the new mode.
    assert len(captured) == 1
    broadcast_sid, msg = captured[0]
    assert broadcast_sid == FULL_SID
    assert msg["type"] == "session_update"
    assert msg["session"]["permissionMode"] == "plan"


async def test_set_permission_mode_unknown_session_no_broadcast(
    bridge_with_session, ops,
) -> None:
    """Bogus session id: ops returns an error, no broadcast leak."""
    _, captured = bridge_with_session

    result = await ops.set_permission_mode(
        session_id="does-not-exist", mode="plan",
    )

    assert "error" in result
    assert captured == []
