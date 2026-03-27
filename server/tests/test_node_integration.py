"""Integration tests for multi-node architecture (Phase 1).

Tests node registration, session routing, Ring0 isolation, and event
filtering across hub + remote node configurations.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from server.node_registry import NodeRegistry, RegisteredNode
from server.node_tunnel import NodeTunnel
from server.ws_bridge import WsBridge, Session


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_vibr8_dir(tmp_path):
    """Redirect NodeRegistry persistence to a temp dir."""
    with patch("server.node_registry.NODES_FILE", tmp_path / "nodes.json"), \
         patch("server.node_registry.VIBR8_DIR", tmp_path):
        yield tmp_path


@pytest.fixture
def registry(tmp_vibr8_dir):
    """Fresh NodeRegistry backed by temp dir."""
    return NodeRegistry()


@pytest.fixture
def bridge():
    """Fresh WsBridge with no store or Ring0."""
    return WsBridge()


# ── Node Registry Tests ─────────────────────────────────────────────────────


class TestNodeRegistry:
    """Node registration, heartbeat, and API key management."""

    def test_local_node_always_exists(self, registry):
        """The hub itself is always present as the 'local' node."""
        local = registry.local_node
        assert local.id == "local"
        assert local.status == "online"

    def test_generate_and_validate_api_key(self, registry):
        raw_key, entry = registry.generate_api_key("test-node")
        assert raw_key.startswith("sk-node-")
        assert entry.name == "test-node"
        found = registry.validate_standalone_key(raw_key)
        assert found is not None
        assert found.id == entry.id

    def test_register_node(self, registry):
        raw_key, _ = registry.generate_api_key("cloud-dev")
        node = registry.register(
            name="cloud-dev",
            api_key=raw_key,
            capabilities={"platform": "linux"},
        )
        assert node.name == "cloud-dev"
        assert node.id != "local"
        assert node.capabilities["platform"] == "linux"
        assert registry.get_node(node.id) is node
        assert registry.get_node_by_name("cloud-dev") is node

    def test_reregister_existing_node(self, registry):
        raw_key, _ = registry.generate_api_key("cloud-dev")
        node1 = registry.register("cloud-dev", raw_key)
        node2 = registry.register("cloud-dev", raw_key, {"platform": "darwin"})
        assert node1.id == node2.id
        assert node2.capabilities["platform"] == "darwin"

    def test_reregister_with_wrong_key_fails(self, registry):
        """Re-registering an existing node with a wrong key raises PermissionError."""
        raw_key, _ = registry.generate_api_key("test-node")
        registry.register("test-node", raw_key)
        with pytest.raises(PermissionError):
            registry.register("test-node", "sk-node-wrong-key")

    def test_heartbeat_tracking(self, registry):
        raw_key, _ = registry.generate_api_key("node-a")
        node = registry.register("node-a", raw_key)
        node.status = "online"
        node.last_heartbeat = time.time()
        registry.heartbeat(node.id, session_count=3, ring0_enabled=True)
        assert node.ring0_enabled is True

    def test_heartbeat_timeout_marks_offline(self, registry):
        raw_key, _ = registry.generate_api_key("node-a")
        node = registry.register("node-a", raw_key)
        node.status = "online"
        node.last_heartbeat = time.time() - 100
        newly_offline = registry.check_heartbeats(timeout=90.0)
        assert node.id in newly_offline
        assert node.status == "offline"

    def test_local_node_never_times_out(self, registry):
        local = registry.local_node
        local.last_heartbeat = time.time() - 1000
        newly_offline = registry.check_heartbeats(timeout=90.0)
        assert "local" not in newly_offline
        assert local.status == "online"

    def test_unregister_node(self, registry):
        raw_key, _ = registry.generate_api_key("temp-node")
        node = registry.register("temp-node", raw_key)
        nid = node.id
        assert registry.unregister(nid) is True
        assert registry.get_node(nid) is None

    def test_unregister_active_node_reverts_to_local(self, registry):
        raw_key, _ = registry.generate_api_key("temp-node")
        node = registry.register("temp-node", raw_key)
        registry.active_node_id = node.id
        registry.unregister(node.id)
        assert registry.active_node_id == "local"

    def test_set_active_unknown_node_raises(self, registry):
        with pytest.raises(ValueError):
            registry.active_node_id = "nonexistent-id"

    def test_revoke_api_key(self, registry):
        raw_key, entry = registry.generate_api_key("revokable")
        assert registry.validate_standalone_key(raw_key) is not None
        registry.revoke_api_key(entry.id)
        assert registry.validate_standalone_key(raw_key) is None

    def test_find_by_name_fuzzy(self, registry):
        raw_key, _ = registry.generate_api_key("cloud-dev")
        registry.register("cloud-dev", raw_key)
        matches = registry.find_by_name("cloud")
        assert len(matches) == 1
        assert matches[0].name == "cloud-dev"

    def test_persistence_roundtrip(self, tmp_vibr8_dir):
        """Registry state survives save/reload."""
        reg1 = NodeRegistry()
        raw_key, _ = reg1.generate_api_key("persist-test")
        node = reg1.register("persist-test", raw_key)
        node_id = node.id
        reg1.hub_name = "my-hub"

        reg2 = NodeRegistry()
        loaded = reg2.get_node(node_id)
        assert loaded is not None
        assert loaded.name == "persist-test"
        assert reg2.hub_name == "my-hub"

    def test_set_online_offline(self, registry):
        raw_key, _ = registry.generate_api_key("node-b")
        node = registry.register("node-b", raw_key)
        ws_mock = MagicMock()
        registry.set_online(node.id, ws_mock)
        assert node.status == "online"
        assert node.ws is ws_mock
        registry.set_offline(node.id)
        assert node.status == "offline"
        assert node.ws is None
        assert node.tunnel is None

    def test_update_sessions(self, registry):
        raw_key, _ = registry.generate_api_key("node-c")
        node = registry.register("node-c", raw_key)
        registry.update_sessions(node.id, ["s1", "s2", "s3"])
        assert node.session_ids == ["s1", "s2", "s3"]


# ── Node Tunnel Tests ────────────────────────────────────────────────────────


class TestNodeTunnel:
    """Request/response correlation and message dispatch."""

    @pytest.fixture
    def mock_ws(self):
        ws = AsyncMock()
        ws.closed = False
        ws.send_str = AsyncMock()
        return ws

    @pytest.fixture
    def tunnel(self, mock_ws):
        return NodeTunnel("node-123", "test-node", mock_ws)

    async def test_send_command_and_receive_response(self, tunnel, mock_ws):
        """send_command correlates requestId with response."""
        async def fake_send(data):
            msg = json.loads(data.strip())
            req_id = msg["requestId"]
            await asyncio.sleep(0.01)
            await tunnel.handle_incoming(json.dumps({
                "type": "response",
                "requestId": req_id,
                "data": {"sessions": ["s1", "s2"]},
            }))

        mock_ws.send_str.side_effect = fake_send
        result = await tunnel.send_command({"type": "list_sessions"}, timeout=5.0)
        assert result == {"sessions": ["s1", "s2"]}

    async def test_send_command_timeout(self, tunnel, mock_ws):
        """send_command returns error on timeout."""
        mock_ws.send_str = AsyncMock()
        result = await tunnel.send_command({"type": "slow_command"}, timeout=0.05)
        assert result == {"error": "timeout"}

    async def test_fire_and_forget(self, tunnel, mock_ws):
        await tunnel.send_fire_and_forget({"type": "session_message", "data": "hello"})
        mock_ws.send_str.assert_called_once()

    async def test_fire_and_forget_on_closed_ws(self, tunnel, mock_ws):
        mock_ws.closed = True
        await tunnel.send_fire_and_forget({"type": "session_message"})
        mock_ws.send_str.assert_not_called()

    async def test_node_initiated_message_dispatched(self, tunnel, mock_ws):
        received = []

        async def handler(node_id, msg):
            received.append((node_id, msg))

        tunnel.set_message_handler(handler)
        await tunnel.handle_incoming(json.dumps({
            "type": "heartbeat",
            "sessionCount": 5,
        }))
        assert len(received) == 1
        assert received[0][0] == "node-123"
        assert received[0][1]["type"] == "heartbeat"

    async def test_multi_line_ndjson(self, tunnel, mock_ws):
        """Multiple NDJSON lines in one message are all processed."""
        received = []

        async def handler(node_id, msg):
            received.append(msg)

        tunnel.set_message_handler(handler)
        await tunnel.handle_incoming(
            json.dumps({"type": "heartbeat"}) + "\n" +
            json.dumps({"type": "sessions_update", "sessions": []}) + "\n"
        )
        assert len(received) == 2
        assert received[0]["type"] == "heartbeat"
        assert received[1]["type"] == "sessions_update"

    def test_close_cancels_pending_futures(self, tunnel, mock_ws):
        loop = asyncio.new_event_loop()
        future = loop.create_future()
        tunnel._pending["req-1"] = future
        tunnel.close()
        assert future.done()
        with pytest.raises(ConnectionError):
            future.result()
        loop.close()


# ── WsBridge Session Routing Tests ───────────────────────────────────────────


class TestWsBridgeQualifiedIds:
    """Qualified session ID handling for multi-node."""

    def test_qualify_session_id(self):
        qid = WsBridge.qualify_session_id("node-abc", "ring0")
        assert qid == "node-abc:ring0"

    def test_parse_qualified_id_remote(self):
        node_id, raw_id = WsBridge.parse_qualified_id("node-abc:ring0")
        assert node_id == "node-abc"
        assert raw_id == "ring0"

    def test_parse_qualified_id_local(self):
        node_id, raw_id = WsBridge.parse_qualified_id("ring0")
        assert node_id == ""
        assert raw_id == "ring0"

    def test_is_remote_session(self, bridge):
        assert bridge._is_remote_session("node-abc:session-1") is True
        assert bridge._is_remote_session("local-session-1") is False

    def test_get_session_node_id(self, bridge):
        assert bridge.get_session_node_id("node-abc:session-1") == "node-abc"
        assert bridge.get_session_node_id("local-session-1") == "local"

    def test_raw_session_id(self, bridge):
        assert bridge._raw_session_id("node-abc:session-1") == "session-1"
        assert bridge._raw_session_id("local-session") == "local-session"


class TestWsBridgeRemoteSessions:
    """Remote session proxy management."""

    def test_get_or_create_session_creates_proxy(self, bridge):
        """get_or_create_session creates proxy sessions for remote IDs."""
        session = bridge.get_or_create_session("node-abc:s1")
        assert session.id == "node-abc:s1"
        assert "node-abc:s1" in bridge._sessions

    def test_update_remote_sessions_removes_stale(self, bridge):
        """Stale proxy sessions are cleaned up when node reports new list."""
        # Create proxy sessions first
        bridge.get_or_create_session("node-abc:s1")
        bridge.get_or_create_session("node-abc:s2")
        assert "node-abc:s2" in bridge._sessions

        # Node reports only s1 remaining
        bridge.update_remote_sessions("node-abc", [
            {"sessionId": "node-abc:s1"},
        ])
        assert "node-abc:s1" in bridge._sessions
        assert "node-abc:s2" not in bridge._sessions

    def test_remove_remote_node_sessions(self, bridge):
        """All sessions for a node are removed when it disconnects."""
        bridge.get_or_create_session("node-abc:s1")
        bridge.get_or_create_session("node-abc:s2")
        bridge.get_or_create_session("local-session")

        bridge.remove_remote_node_sessions("node-abc")
        assert "node-abc:s1" not in bridge._sessions
        assert "node-abc:s2" not in bridge._sessions
        assert "local-session" in bridge._sessions

    def test_remove_doesnt_affect_other_nodes(self, bridge):
        """Removing node-abc sessions doesn't touch node-xyz sessions."""
        bridge.get_or_create_session("node-abc:s1")
        bridge.get_or_create_session("node-xyz:s1")

        bridge.remove_remote_node_sessions("node-abc")
        assert "node-abc:s1" not in bridge._sessions
        assert "node-xyz:s1" in bridge._sessions


class TestRing0EventIsolation:
    """Hub Ring0 should only receive events for local sessions."""

    async def test_remote_session_events_filtered(self, bridge):
        """_notify_ring0_state_change returns early for remote sessions."""
        ring0 = MagicMock()
        ring0.session_id = "ring0"
        ring0.is_enabled = True
        ring0.events_muted = False
        bridge._ring0_manager = ring0
        bridge.emit_ring0_event = AsyncMock()

        remote_session = bridge.get_or_create_session("node-abc:some-session")
        remote_session.controlled_by = "ring0"

        await bridge._notify_ring0_state_change(remote_session, "idle->running")
        bridge.emit_ring0_event.assert_not_called()

    async def test_local_session_events_emitted(self, bridge):
        """_notify_ring0_state_change fires for local sessions."""
        ring0 = MagicMock()
        ring0.session_id = "ring0"
        ring0.is_enabled = True
        ring0.events_muted = False
        bridge._ring0_manager = ring0
        bridge.emit_ring0_event = AsyncMock()

        local_session = bridge.get_or_create_session("local-session")
        local_session.controlled_by = "ring0"

        with patch("server.session_names.get_name", return_value="Test Session"):
            await bridge._notify_ring0_state_change(local_session, "idle->running")
        bridge.emit_ring0_event.assert_called_once()

    async def test_ring0_own_session_events_suppressed(self, bridge):
        ring0 = MagicMock()
        ring0.session_id = "ring0"
        ring0.is_enabled = True
        bridge._ring0_manager = ring0
        bridge.emit_ring0_event = AsyncMock()

        ring0_session = bridge.get_or_create_session("ring0")
        await bridge._notify_ring0_state_change(ring0_session, "idle->running")
        bridge.emit_ring0_event.assert_not_called()

    async def test_user_pen_events_suppressed(self, bridge):
        ring0 = MagicMock()
        ring0.session_id = "ring0"
        ring0.is_enabled = True
        bridge._ring0_manager = ring0
        bridge.emit_ring0_event = AsyncMock()

        session = bridge.get_or_create_session("user-session")
        session.controlled_by = "user"

        with patch("server.session_names.get_name", return_value="Test"):
            await bridge._notify_ring0_state_change(session, "idle->running")
        bridge.emit_ring0_event.assert_not_called()


# ── Remote Session Message Handling ──────────────────────────────────────────


class TestRemoteSessionMessages:
    """Messages from remote nodes routed to browser clients."""

    async def test_handle_remote_creates_proxy(self, bridge):
        bridge._broadcast_to_browsers = AsyncMock()

        await bridge.handle_remote_session_message("node-abc:s1", {
            "type": "assistant",
            "message": "Hello from remote",
        })

        assert "node-abc:s1" in bridge._sessions
        proxy = bridge._sessions["node-abc:s1"]
        assert len(proxy.message_history) == 1
        assert proxy.message_history[0]["message"] == "Hello from remote"

    async def test_handle_remote_tracks_permissions(self, bridge):
        bridge._broadcast_to_browsers = AsyncMock()

        await bridge.handle_remote_session_message("node-abc:s1", {
            "type": "permission_request",
            "request": {"request_id": "perm-1", "tool_name": "Write"},
        })

        proxy = bridge._sessions["node-abc:s1"]
        assert "perm-1" in proxy.pending_permissions

        await bridge.handle_remote_session_message("node-abc:s1", {
            "type": "permission_response",
            "request_id": "perm-1",
        })
        assert "perm-1" not in proxy.pending_permissions

    async def test_handle_remote_broadcasts(self, bridge):
        bridge._broadcast_to_browsers = AsyncMock()

        msg = {"type": "assistant", "message": "test"}
        await bridge.handle_remote_session_message("node-abc:s1", msg)

        bridge._broadcast_to_browsers.assert_called_once()
        call_args = bridge._broadcast_to_browsers.call_args
        assert call_args[0][1] == msg

    async def test_handle_remote_updates_session_state(self, bridge):
        bridge._broadcast_to_browsers = AsyncMock()

        await bridge.handle_remote_session_message("node-abc:s1", {
            "type": "session_update",
            "session": {"model": "claude-sonnet-4-6", "cwd": "/remote/code"},
        })

        proxy = bridge._sessions["node-abc:s1"]
        assert proxy.state.get("model") == "claude-sonnet-4-6"
        assert proxy.state.get("cwd") == "/remote/code"


# ── API Endpoint Tests ───────────────────────────────────────────────────────


class TestSessionListEndpoints:
    """Test /api/sessions and /api/ring0/sessions include isRing0 flag."""

    @pytest.fixture
    def mock_launcher(self):
        launcher = MagicMock()
        launcher.list_sessions.return_value = []
        launcher.get_all_session_ids.return_value = []
        launcher.get_session.return_value = None
        return launcher

    @pytest.fixture
    def mock_ring0(self):
        ring0 = MagicMock()
        ring0.session_id = "ring0"
        ring0.is_enabled = True
        ring0.events_muted = False
        return ring0

    @pytest.fixture
    def app(self, mock_launcher, mock_ring0, bridge):
        from server.routes import create_routes
        from server.session_store import SessionStore
        from server.worktree_tracker import WorktreeTracker

        store = SessionStore()
        routes = create_routes(
            mock_launcher, bridge, store,
            worktree_tracker=WorktreeTracker(),
            ring0_manager=mock_ring0,
        )
        app = web.Application()
        app.router.add_routes(routes)
        return app

    async def test_local_sessions_include_isRing0(self, app, mock_launcher):
        ring0_info = MagicMock()
        ring0_info.to_dict.return_value = {
            "sessionId": "ring0",
            "state": "connected",
            "cwd": "/tmp",
            "name": "Ring0",
            "createdAt": 1000,
        }
        other_info = MagicMock()
        other_info.to_dict.return_value = {
            "sessionId": "session-abc",
            "state": "connected",
            "cwd": "/code",
            "name": "My Session",
            "createdAt": 2000,
        }
        mock_launcher.list_sessions.return_value = [ring0_info, other_info]

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/sessions")
            assert resp.status == 200
            data = await resp.json()

        # Ring0 should be first (pinned) and have isRing0
        assert data[0]["sessionId"] == "ring0"
        assert data[0]["isRing0"] is True
        # Other session should not
        assert data[1].get("isRing0") is not True

    async def test_ring0_sessions_endpoint_includes_flag(self, app, mock_launcher):
        mock_launcher.get_all_session_ids.return_value = ["ring0", "other"]

        ring0_info = MagicMock()
        ring0_info.name = "Ring0"
        ring0_info.state = "connected"
        ring0_info.cwd = "/tmp"
        ring0_info.backendType = "claude"
        ring0_info.archived = False
        mock_launcher.get_session.side_effect = lambda sid: ring0_info if sid == "ring0" else None

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/ring0/sessions")
            assert resp.status == 200
            data = await resp.json()

        ring0_entry = next((s for s in data if s["sessionId"] == "ring0"), None)
        assert ring0_entry is not None
        assert ring0_entry["isRing0"] is True
