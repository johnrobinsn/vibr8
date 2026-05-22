"""Tests for the SwappableNodeClient + QualifyingNodeClient wrappers.

These two wrappers are the core of the Option A keystone path:
  - SwappableNodeClient: holds local_node_ops; main.py swaps it from
    the in-process NodeOperations to RemoteNodeClient (the self-node
    tunnel) once the subprocess has registered.
  - QualifyingNodeClient: rewrites sessionId at the hub boundary so
    the hub treats self-node sessions like any other remote node
    (qualified IDs go through existing WS forwarding).
"""

from __future__ import annotations

from typing import Any

import pytest

from vibr8_core.node_client import (
    QualifyingNodeClient,
    SwappableNodeClient,
)


class _FakeInner:
    """Stand-in NodeClient that records calls and returns canned shapes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._sessions = [{"sessionId": "abc"}, {"sessionId": "def"}]

    async def list_sessions(self) -> dict:
        self.calls.append(("list_sessions", {}))
        return {"sessions": [dict(s) for s in self._sessions]}

    async def get_session(self, session_id: str = "") -> dict:
        self.calls.append(("get_session", {"session_id": session_id}))
        return {"sessionId": session_id, "state": "connected"}

    async def kill_session(self, session_id: str = "") -> dict:
        self.calls.append(("kill_session", {"session_id": session_id}))
        return {"ok": True}

    async def rename_session(self, session_id: str = "", name: str = "") -> dict:
        self.calls.append(("rename_session", {"session_id": session_id, "name": name}))
        return {"ok": True, "name": name}

    async def submit_message(
        self, session_id: str = "", content: str = "", source_client_id: str = "",
    ) -> dict:
        self.calls.append(("submit_message", {
            "session_id": session_id, "content": content,
            "source_client_id": source_client_id,
        }))
        return {"ok": True}


# ── SwappableNodeClient ─────────────────────────────────────────────────────


async def test_swappable_delegates_to_initial_target() -> None:
    inner = _FakeInner()
    w = SwappableNodeClient(inner)
    r = await w.list_sessions()
    assert r == {"sessions": [{"sessionId": "abc"}, {"sessionId": "def"}]}


async def test_swappable_retargets_after_swap() -> None:
    first = _FakeInner()
    second = _FakeInner()
    second._sessions = [{"sessionId": "xyz"}]
    w = SwappableNodeClient(first)
    assert (await w.list_sessions())["sessions"][0]["sessionId"] == "abc"
    w.swap(second)
    assert (await w.list_sessions())["sessions"][0]["sessionId"] == "xyz"
    # First inner not called again after swap
    assert len(first.calls) == 1
    assert len(second.calls) == 1


async def test_swappable_target_property_returns_current() -> None:
    a, b = _FakeInner(), _FakeInner()
    w = SwappableNodeClient(a)
    assert w.target is a
    w.swap(b)
    assert w.target is b


# ── QualifyingNodeClient ────────────────────────────────────────────────────


async def test_qualifying_strips_prefix_on_session_id_kwarg() -> None:
    inner = _FakeInner()
    q = QualifyingNodeClient(inner, "node42")
    await q.kill_session(session_id="node42:abc")
    # Inner should see the raw ID without the prefix.
    assert inner.calls[-1] == ("kill_session", {"session_id": "abc"})


async def test_qualifying_passes_unprefixed_session_id_through() -> None:
    inner = _FakeInner()
    q = QualifyingNodeClient(inner, "node42")
    await q.kill_session(session_id="abc")
    assert inner.calls[-1] == ("kill_session", {"session_id": "abc"})


async def test_qualifying_does_not_strip_other_nodes_prefix() -> None:
    inner = _FakeInner()
    q = QualifyingNodeClient(inner, "node42")
    # A different node's prefix should pass through untouched
    await q.kill_session(session_id="other:abc")
    assert inner.calls[-1] == ("kill_session", {"session_id": "other:abc"})


async def test_qualifying_rewrites_session_id_in_response_dict() -> None:
    inner = _FakeInner()
    q = QualifyingNodeClient(inner, "node42")
    r = await q.get_session(session_id="abc")
    # Response sessionId qualified on the way back out
    assert r["sessionId"] == "node42:abc"


async def test_qualifying_rewrites_sessions_list_response() -> None:
    inner = _FakeInner()
    q = QualifyingNodeClient(inner, "node42")
    r = await q.list_sessions()
    ids = [s["sessionId"] for s in r["sessions"]]
    assert ids == ["node42:abc", "node42:def"]


async def test_qualifying_does_not_double_qualify_already_prefixed_response() -> None:
    inner = _FakeInner()
    # Inner returns an already-qualified id (shouldn't happen in practice,
    # but verify QualifyingNodeClient is idempotent so a re-emitted
    # response doesn't grow extra `:` segments).
    inner._sessions = [{"sessionId": "node42:abc"}]
    q = QualifyingNodeClient(inner, "node42")
    r = await q.list_sessions()
    assert r["sessions"][0]["sessionId"] == "node42:abc"


# ── Composed: Swappable wrapping Qualifying ─────────────────────────────────


async def test_swappable_can_target_qualifying_wrapper() -> None:
    """Mirrors the main.py wiring: swap to QualifyingNodeClient(remote)."""
    inner = _FakeInner()
    q = QualifyingNodeClient(inner, "selfnode")
    w = SwappableNodeClient(None)  # type: ignore[arg-type]
    w.swap(q)
    r = await w.list_sessions()
    assert r["sessions"][0]["sessionId"] == "selfnode:abc"
    # Round-trip a write call
    await w.kill_session(session_id="selfnode:def")
    assert inner.calls[-1] == ("kill_session", {"session_id": "def"})
