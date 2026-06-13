"""Tests for SwappableNodeClient.

SwappableNodeClient holds local_node_ops; main.py swaps it from the
in-process NodeOperations to RemoteNodeClient (the self-node tunnel)
once the subprocess has registered. Session ids stay raw end to end —
the QualifyingNodeClient layer was deleted with the cross-node session
namespace (docs/node-vended-ui.md, Phase 4).
"""

from __future__ import annotations

from typing import Any

import pytest

from vibr8_core.node_client import SwappableNodeClient


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
