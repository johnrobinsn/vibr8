"""Route-level test for ``POST /api/ring0/switch-ui``.

This handler runs on the node's local server (contract ui/v1: the hub
does not proxy switch-ui). Ring0 MCP calls it with an 8-char session
id prefix pulled from ``list_sessions`` output. The route validates
via ``get_session`` — which expands the prefix internally — and then
must broadcast the *expanded* full id to the browser so the iframe's
store keys (sessionNames, sdkSessions) actually match.

Regression: the earlier implementation broadcast the raw caller input,
so `store.setCurrentSession(prefix)` fed a key nothing matched. The
sidebar-click path worked (full id in URL) but voice-driven switching
looked like a no-op — the ``/api/_title`` publish computed an empty
title and the shell strip went stale.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vibr8_core.node_operations import NodeOperations
from vibr8_core.ws_bridge import Session, WsBridge


FULL_SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PREFIX = FULL_SID[:8]
CLIENT_ID = "test-client-123"


@pytest.fixture
def bridge_with_session():
    bridge = WsBridge()
    bridge._sessions[FULL_SID] = Session(id=FULL_SID)
    # Pretend a browser is connected under CLIENT_ID so the route's
    # send helper resolves it. We stash a fake ws in _ws_by_client;
    # the send path won't actually be exercised because we monkeypatch
    # send_ring0_switch_ui below.
    bridge._ws_by_client[CLIENT_ID] = MagicMock()
    return bridge


@pytest.fixture
def launcher():
    """Fake CliLauncher backing _expand_session_id: knows one session."""
    info = MagicMock()
    info.to_dict.return_value = {"sessionId": FULL_SID, "state": "connected", "cwd": "/tmp"}
    info.sessionId = FULL_SID

    launcher = MagicMock()
    launcher.list_sessions.return_value = [info]
    launcher.get_session.side_effect = lambda sid: info if sid == FULL_SID else None
    launcher.is_alive.return_value = True
    return launcher


@pytest.fixture
def app(bridge_with_session, launcher):
    from server.routes import create_routes
    from vibr8_core.session_store import SessionStore
    from vibr8_core.worktree_tracker import WorktreeTracker

    ops = NodeOperations(
        launcher=launcher,
        bridge=bridge_with_session,
        store=MagicMock(spec=SessionStore),
        ring0=None,
    )
    routes = create_routes(
        launcher, bridge_with_session, MagicMock(spec=SessionStore),
        worktree_tracker=WorktreeTracker(),
        local_node_ops=ops,
    )
    app = web.Application()
    app.router.add_routes(routes)
    return app


async def test_switch_ui_broadcasts_full_id_not_prefix(
    app, bridge_with_session, monkeypatch,
) -> None:
    """Ring0 sends the 8-char prefix; the route must broadcast the full id."""
    captured: dict[str, Any] = {}

    async def _capture(
        target_session_id: str, *, client_id: str,
    ) -> bool:
        captured["session_id"] = target_session_id
        captured["client_id"] = client_id
        return True

    # HubBrowserBridge delegates to ws_bridge; the route calls the
    # bridge's method. Patch the wrapper method that create_routes
    # constructs internally via HubBrowserBridge(ws_bridge).
    monkeypatch.setattr(
        bridge_with_session, "send_ring0_switch_ui", _capture,
    )

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/ring0/switch-ui",
            json={"sessionId": PREFIX, "clientId": CLIENT_ID},
        )
        body = await resp.json()

    assert resp.status == 200, body
    assert body["ok"] is True
    # The route's response uses the expanded id too — callers that
    # echo `sessionId` to the user see the full id.
    assert body["sessionId"] == FULL_SID

    assert captured["session_id"] == FULL_SID, (
        f"broadcast used {captured['session_id']!r}, expected {FULL_SID!r} — "
        "iframe store keys are full ids, so a prefix payload silently misses"
    )
    assert captured["client_id"] == CLIENT_ID


async def test_switch_ui_unknown_session_returns_404(app) -> None:
    """A bogus session id must produce a real 404 for Ring0 rather than
    a silent 200 (the browser would otherwise switch to a nonexistent
    session and Ring0 would tell the user "switched" for nothing)."""
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/ring0/switch-ui",
            json={"sessionId": "nonexistent-id", "clientId": CLIENT_ID},
        )
        assert resp.status == 404


async def test_switch_ui_missing_session_id_returns_400(app) -> None:
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/ring0/switch-ui", json={})
        assert resp.status == 400
