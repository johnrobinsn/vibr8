"""Verify the hub refuses ``POST /api/nodes/{id}/activate`` when the
resolved client is deeplink-pinned to a different node.

Without this the browser tab silently snaps back (App.tsx re-forces
activeNodeId to the pinned value) but Ring0's ``switch_node`` MCP
tool tells the user "Switched to node X" — a straight lie. The 409
lets Ring0 report honestly.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vibr8_core.hub_browser_bridge import HubBrowserBridge
from vibr8_core.ws_bridge import WsBridge


CLIENT_ID = "client-abc"
PINNED_NODE_ID = "5b724a910a9e27ba49ff78ca53aeee83"
OTHER_NODE_ID = "d40201bc00000000000000000000000a"


class _FakeNode:
    def __init__(self, node_id: str, name: str) -> None:
        self.id = node_id
        self.name = name
        self.status = "online"
        self.tunnel = None


class _FakeRegistry:
    def __init__(self, *nodes: _FakeNode) -> None:
        self._nodes = {n.id: n for n in nodes}

    def get_node(self, node_id: str) -> _FakeNode | None:
        return self._nodes.get(node_id)


@pytest.fixture
def bridge():
    return WsBridge()


@pytest.fixture
def hub_browser_bridge(bridge):
    return HubBrowserBridge(bridge)


@pytest.fixture
def registry():
    return _FakeRegistry(
        _FakeNode(PINNED_NODE_ID, "blah"),
        _FakeNode(OTHER_NODE_ID, "hermes"),
    )


@pytest.fixture
def app(bridge, hub_browser_bridge, registry):
    from server.routes import create_routes
    from vibr8_core.session_store import SessionStore
    from vibr8_core.worktree_tracker import WorktreeTracker

    launcher = MagicMock()
    launcher.list_sessions.return_value = []
    launcher.get_session.side_effect = lambda sid: None

    routes = create_routes(
        launcher, bridge, MagicMock(spec=SessionStore),
        worktree_tracker=WorktreeTracker(),
        node_registry=registry,
        hub_browser_bridge=hub_browser_bridge,
    )
    app = web.Application()
    app.router.add_routes(routes)
    return app


async def test_activate_returns_409_when_client_pinned_elsewhere(
    app, hub_browser_bridge,
) -> None:
    """Client is pinned to `blah`. Ring0 tries to activate `hermes`.
    The route must refuse with a 409 and a human-readable error naming
    the pinned node, so Ring0's tool reports honestly."""
    hub_browser_bridge.set_client_pin(CLIENT_ID, PINNED_NODE_ID)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            f"/api/nodes/{OTHER_NODE_ID}/activate",
            json={"clientId": CLIENT_ID},
        )
        body = await resp.json()

    assert resp.status == 409, body
    assert "pinned" in body["error"].lower()
    assert body["pinnedNodeId"] == PINNED_NODE_ID
    assert body["pinnedNodeName"] == "blah"
    # Active node must NOT have flipped as a side effect.
    assert hub_browser_bridge.get_client_active_node(CLIENT_ID) == ""


async def test_activate_succeeds_when_target_matches_pin(
    app, hub_browser_bridge,
) -> None:
    """Activating the same node the client is pinned to is a no-conflict
    case — the route should succeed. (Otherwise a pinned tab could
    never issue its own confirming active-node write on reload.)"""
    hub_browser_bridge.set_client_pin(CLIENT_ID, PINNED_NODE_ID)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            f"/api/nodes/{PINNED_NODE_ID}/activate",
            json={"clientId": CLIENT_ID},
        )
        body = await resp.json()

    assert resp.status == 200, body
    assert body["ok"] is True
    assert hub_browser_bridge.get_client_active_node(CLIENT_ID) == PINNED_NODE_ID


async def test_activate_succeeds_when_client_unpinned(
    app, hub_browser_bridge,
) -> None:
    """No pin recorded → activation proceeds normally."""
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            f"/api/nodes/{OTHER_NODE_ID}/activate",
            json={"clientId": CLIENT_ID},
        )
        assert resp.status == 200


async def test_active_node_post_can_set_and_clear_pin(
    app, hub_browser_bridge,
) -> None:
    """``POST /api/clients/{id}/active-node`` propagates the pin state
    from the shell to the hub. Clearing (null/absent) removes the pin
    so a tab that navigates away from ``/@<node>`` no longer refuses
    switches."""
    async with TestClient(TestServer(app)) as client:
        # Set the pin.
        resp = await client.post(
            f"/api/clients/{CLIENT_ID}/active-node",
            json={"nodeId": PINNED_NODE_ID, "pinnedNodeId": PINNED_NODE_ID},
        )
        assert resp.status == 200
        assert hub_browser_bridge.get_client_pin(CLIENT_ID) == PINNED_NODE_ID

        # Clear the pin (null).
        resp = await client.post(
            f"/api/clients/{CLIENT_ID}/active-node",
            json={"nodeId": OTHER_NODE_ID, "pinnedNodeId": None},
        )
        assert resp.status == 200
        assert hub_browser_bridge.get_client_pin(CLIENT_ID) == ""


async def test_would_conflict_with_pin_helper() -> None:
    """Direct unit test of the bridge helper — cheap guarantee that
    the semantic ('pin set AND target != pin → returns pinned id') is
    preserved even if a refactor moves the check elsewhere."""
    bridge = HubBrowserBridge(WsBridge())
    bridge.set_client_pin(CLIENT_ID, PINNED_NODE_ID)

    # Same target → no conflict.
    assert bridge.would_conflict_with_pin(CLIENT_ID, PINNED_NODE_ID) == ""
    # Different target → returns the pinned id.
    assert bridge.would_conflict_with_pin(CLIENT_ID, OTHER_NODE_ID) == PINNED_NODE_ID
    # Unpinned client → no conflict regardless of target.
    assert bridge.would_conflict_with_pin("other-client", OTHER_NODE_ID) == ""
