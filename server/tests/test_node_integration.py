"""Integration tests for multi-node architecture (Phase 1).

Tests node registration, session routing, Ring0 isolation, and event
filtering across hub + remote node configurations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.web_app import NotAppKeyWarning
from aiohttp.test_utils import TestClient, TestServer

from server.node_registry import NodeRegistry, RegisteredNode
from server.node_tunnel import NodeTunnel
from vibr8_core.ws_bridge import WsBridge, Session


def _audit_records(caplog, event: str):
    return [
        record for record in caplog.records
        if getattr(record, "audit_event", "") == event
    ]


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
        raw_key, entry = registry.generate_api_key("test-node", username="alice")
        assert raw_key.startswith("sk-node-")
        assert entry.name == "test-node"
        assert entry.username == "alice"
        found = registry.validate_standalone_key(raw_key)
        assert found is not None
        assert found.id == entry.id
        assert found.last_used_at > 0

    def test_register_node(self, registry):
        raw_key, entry = registry.generate_api_key("cloud-dev")
        node = registry.register(
            name="cloud-dev",
            api_key=raw_key,
            capabilities={"platform": "linux"},
        )
        assert node.name == "cloud-dev"
        assert node.id != "local"
        assert node.capabilities["platform"] == "linux"
        assert node.api_key_id == entry.id
        assert entry.node_id == node.id
        assert registry.get_node(node.id) is node
        assert registry.get_node_by_name("cloud-dev") is node

    def test_node_token_binds_to_first_registered_node(self, registry):
        raw_key, entry = registry.generate_api_key("cloud-dev")

        node = registry.register("cloud-dev", raw_key)

        assert entry.node_id == node.id
        assert entry.to_dict()["nodeId"] == node.id
        assert entry.to_api_dict()["nodeId"] == node.id

    def test_bound_node_token_cannot_register_different_node(self, registry):
        raw_key, _ = registry.generate_api_key("node-a")
        registry.register("node-a", raw_key)

        with pytest.raises(PermissionError, match="already bound"):
            registry.register("node-b", raw_key)

    def test_concurrent_registers_bind_token_to_one_node(self, registry):
        raw_key, entry = registry.generate_api_key("shared-token")

        def register(name: str) -> tuple[str, str]:
            try:
                node = registry.register(name, raw_key)
                return ("ok", node.id)
            except PermissionError as exc:
                return ("error", str(exc))

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(register, ["node-a", "node-b"]))

        assert [status for status, _ in results].count("ok") == 1
        assert [status for status, _ in results].count("error") == 1
        assert any("already bound" in value for status, value in results if status == "error")

        registered = [
            node for node in registry.get_all_nodes()
            if node.id != registry.LOCAL_NODE_ID
        ]
        assert len(registered) == 1
        assert entry.node_id == registered[0].id
        assert registry.validate_api_key(registered[0].id, raw_key) is True

    def test_register_new_node_requires_issued_api_key(self, registry):
        with pytest.raises(PermissionError):
            registry.register("cloud-dev", "sk-node-unissued")

    def test_register_new_node_rejects_revoked_api_key(self, registry):
        raw_key, entry = registry.generate_api_key("cloud-dev")
        assert registry.revoke_api_key(entry.id) is True

        with pytest.raises(PermissionError):
            registry.register("cloud-dev", raw_key)

    def test_reregister_existing_node(self, registry):
        raw_key, _ = registry.generate_api_key("cloud-dev")
        node1 = registry.register("cloud-dev", raw_key)
        node2 = registry.register("cloud-dev", raw_key, {"platform": "darwin"})
        assert node1.id == node2.id
        assert node2.capabilities["platform"] == "darwin"

    def test_reregister_existing_node_can_rotate_to_new_token(self, registry):
        raw_key1, entry1 = registry.generate_api_key("cloud-dev")
        raw_key2, entry2 = registry.generate_api_key("cloud-dev-rotated")
        node1 = registry.register("cloud-dev", raw_key1)

        registry.revoke_api_key(entry1.id)
        node2 = registry.register("cloud-dev", raw_key2, {"platform": "linux"})

        assert node1.id == node2.id
        assert node2.api_key_id == entry2.id
        assert entry2.node_id == node2.id
        assert node2.capabilities["platform"] == "linux"
        assert registry.validate_api_key(node2.id, raw_key2) is True
        assert registry.validate_api_key(node2.id, raw_key1) is False

    def test_bound_node_token_cannot_rotate_different_existing_node(self, registry):
        raw_key1, _ = registry.generate_api_key("node-a")
        raw_key2, _ = registry.generate_api_key("node-b")
        registry.register("node-a", raw_key1)
        node2 = registry.register("node-b", raw_key2)

        with pytest.raises(PermissionError, match="already bound"):
            registry.register("node-b", raw_key1)

        assert registry.validate_api_key(node2.id, raw_key2) is True

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

    def test_revoke_api_key(self, registry):
        raw_key, entry = registry.generate_api_key("revokable")
        assert registry.validate_standalone_key(raw_key) is not None
        registry.revoke_api_key(entry.id)
        assert registry.validate_standalone_key(raw_key) is None
        assert registry.list_api_keys() == []
        assert registry._api_keys[entry.id].revoked_at > 0

    def test_revoke_api_key_blocks_registered_node_reconnect(self, registry):
        raw_key, entry = registry.generate_api_key("revokable-node")
        node = registry.register("revokable-node", raw_key)

        assert registry.validate_api_key(node.id, raw_key) is True

        registry.revoke_api_key(entry.id)

        assert registry.validate_api_key(node.id, raw_key) is False

    def test_concurrent_register_and_revoke_never_leave_revoked_token_valid(
        self,
        registry,
    ):
        raw_key, entry = registry.generate_api_key("race-node")

        def register() -> str:
            try:
                registry.register("race-node", raw_key)
                return "registered"
            except PermissionError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as executor:
            register_future = executor.submit(register)
            revoke_future = executor.submit(registry.revoke_api_key, entry.id)

        assert register_future.result() in {"registered", "rejected"}
        assert revoke_future.result() is True
        assert registry.validate_standalone_key(raw_key) is None

        node = registry.get_node_by_name("race-node")
        if node:
            assert registry.validate_api_key(node.id, raw_key) is False

    def test_revoke_api_key_marks_registered_online_nodes_offline(self, registry):
        raw_key, entry = registry.generate_api_key("online-node")
        node = registry.register("online-node", raw_key)
        ws_mock = MagicMock()
        node.tunnel = MagicMock()
        registry.set_online(node.id, ws_mock)

        assert node.status == "online"

        registry.revoke_api_key(entry.id)

        assert node.status == "offline"
        assert node.ws is None
        assert node.tunnel is None

    def test_get_nodes_by_api_key_id(self, registry):
        raw_key, entry = registry.generate_api_key("node-key")
        node = registry.register("node-key", raw_key)

        assert registry.get_nodes_by_api_key_id(entry.id) == [node]
        assert registry.get_nodes_by_api_key_id("missing") == []

    def test_legacy_node_without_api_key_id_keeps_stored_key_behavior(self, registry):
        raw_key, entry = registry.generate_api_key("legacy-node")
        node = registry.register("legacy-node", raw_key)
        node.api_key_id = ""

        registry.revoke_api_key(entry.id)

        assert registry.validate_api_key(node.id, raw_key) is True

    def test_list_api_keys_filters_by_owner(self, registry):
        _, alice_entry = registry.generate_api_key("alice-node", username="alice")
        _, bob_entry = registry.generate_api_key("bob-node", username="bob")

        assert {entry.id for entry in registry.list_api_keys(username="alice")} == {
            alice_entry.id
        }
        assert {entry.id for entry in registry.list_api_keys(username="bob")} == {
            bob_entry.id
        }

    def test_owner_filter_includes_legacy_ownerless_keys(self, registry):
        _, legacy_entry = registry.generate_api_key("legacy-node")
        _, alice_entry = registry.generate_api_key("alice-node", username="alice")

        assert {entry.id for entry in registry.list_api_keys(username="alice")} == {
            alice_entry.id,
            legacy_entry.id,
        }

    def test_revoke_api_key_requires_matching_owner(self, registry):
        raw_key, entry = registry.generate_api_key("owned-node", username="alice")

        assert registry.revoke_api_key(entry.id, username="bob") is False
        assert registry.validate_standalone_key(raw_key) is not None

        assert registry.revoke_api_key(entry.id, username="alice") is True
        assert registry.validate_standalone_key(raw_key) is None

    def test_revoke_api_key_allows_legacy_ownerless_keys(self, registry):
        raw_key, entry = registry.generate_api_key("legacy-node")

        assert registry.revoke_api_key(entry.id, username="alice") is True
        assert registry.validate_standalone_key(raw_key) is None

    def test_api_key_metadata_persists_revocation(self, tmp_vibr8_dir):
        reg1 = NodeRegistry()
        raw_key, entry = reg1.generate_api_key("persist-revoke", username="alice")
        assert reg1.revoke_api_key(entry.id, username="alice") is True

        reg2 = NodeRegistry()
        loaded_entry = reg2._api_keys[entry.id]
        assert loaded_entry.username == "alice"
        assert loaded_entry.revoked_at > 0
        assert reg2.validate_standalone_key(raw_key) is None

    def test_find_by_name_fuzzy(self, registry):
        raw_key, _ = registry.generate_api_key("cloud-dev")
        registry.register("cloud-dev", raw_key)
        matches = registry.find_by_name("cloud")
        assert len(matches) == 1
        assert matches[0].name == "cloud-dev"

    def test_persistence_roundtrip(self, tmp_vibr8_dir):
        """Registry state survives save/reload."""
        reg1 = NodeRegistry()
        raw_key, entry = reg1.generate_api_key("persist-test")
        node = reg1.register("persist-test", raw_key)
        node_id = node.id
        reg1.hub_name = "my-hub"

        reg2 = NodeRegistry()
        loaded = reg2.get_node(node_id)
        assert loaded is not None
        assert loaded.name == "persist-test"
        assert loaded.api_key_id == entry.id
        assert reg2._api_keys[entry.id].node_id == node_id
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

        with patch("vibr8_core.session_names.get_name", return_value="Test Session"):
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
        from vibr8_core.session_store import SessionStore
        from vibr8_core.worktree_tracker import WorktreeTracker

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
        ring0_info.to_dict.return_value = {
            "sessionId": "ring0",
            "name": "Ring0",
            "state": "connected",
            "cwd": "/tmp",
            "backendType": "claude",
        }
        other_info = MagicMock()
        other_info.to_dict.return_value = {
            "sessionId": "other",
            "name": "Other",
            "state": "connected",
            "cwd": "/code",
            "backendType": "claude",
        }
        mock_launcher.list_sessions.return_value = [ring0_info, other_info]
        mock_launcher.get_session.side_effect = lambda sid: ring0_info if sid == "ring0" else None

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/ring0/sessions")
            assert resp.status == 200
            data = await resp.json()

        ring0_entry = next((s for s in data if s["sessionId"] == "ring0"), None)
        assert ring0_entry is not None
        assert ring0_entry["isRing0"] is True


class TestNodeTokenEndpoints:
    """Smoke tests for authenticated node token routes."""

    @pytest.fixture
    def mock_launcher(self):
        launcher = MagicMock()
        launcher.list_sessions.return_value = []
        launcher.get_all_session_ids.return_value = []
        launcher.get_session.return_value = None
        return launcher

    @pytest.fixture
    def app(self, mock_launcher, bridge, registry):
        from server.routes import create_routes
        from vibr8_core.session_store import SessionStore
        from vibr8_core.worktree_tracker import WorktreeTracker

        @web.middleware
        async def auth_user_middleware(request, handler):
            user = request.headers.get("X-Test-User")
            if user:
                request["auth_user"] = user
            return await handler(request)

        store = SessionStore()
        routes = create_routes(
            mock_launcher,
            bridge,
            store,
            worktree_tracker=WorktreeTracker(),
            node_registry=registry,
        )
        app = web.Application(middlewares=[auth_user_middleware])
        app.router.add_routes(routes)
        return app

    async def test_create_node_token_uses_authenticated_user(self, app, registry):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/nodes/tokens",
                json={"name": "alice-node"},
                headers={"X-Test-User": "alice"},
            )
            assert resp.status == 200
            data = await resp.json()

        assert data["apiKey"].startswith("sk-node-")
        assert data["token"] == data["apiKey"]
        assert data["username"] == "alice"
        assert "Revocation prevents new registrations" in data["revocationNote"]
        entry = registry._api_keys[data["id"]]
        assert entry.username == "alice"

    async def test_create_node_token_emits_audit_log(self, app, registry, caplog):
        caplog.set_level(logging.INFO)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/nodes/tokens",
                json={"name": "audit-node"},
                headers={"X-Test-User": "alice"},
            )
            data = await resp.json()

        assert resp.status == 200
        records = _audit_records(caplog, "node_token_created")
        assert records[-1].path == "/api/nodes/tokens"
        assert records[-1].username == "alice"
        assert records[-1].api_key_id == data["id"]
        assert records[-1].token_name == "audit-node"
        assert records[-1].ip

    async def test_legacy_create_node_key_emits_legacy_path_in_audit_log(self, app, registry, caplog):
        caplog.set_level(logging.INFO)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/nodes/generate-key",
                json={"name": "legacy-node"},
                headers={"X-Test-User": "alice"},
            )
            data = await resp.json()

        assert resp.status == 200
        records = _audit_records(caplog, "node_token_created")
        assert records[-1].path == "/api/nodes/generate-key"
        assert records[-1].api_key_id == data["id"]

    async def test_list_node_tokens_filters_to_authenticated_user(self, app, registry):
        _, alice_entry = registry.generate_api_key("alice-node", username="alice")
        registry.generate_api_key("bob-node", username="bob")
        _, legacy_entry = registry.generate_api_key("legacy-node")

        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/nodes/tokens",
                headers={"X-Test-User": "alice"},
            )
            assert resp.status == 200
            data = await resp.json()

        assert {entry["id"] for entry in data} == {alice_entry.id, legacy_entry.id}

    async def test_revoke_node_token_enforces_authenticated_owner(self, app, registry):
        raw_key, entry = registry.generate_api_key("alice-node", username="alice")

        async with TestClient(TestServer(app)) as client:
            bob_resp = await client.delete(
                f"/api/nodes/tokens/{entry.id}",
                headers={"X-Test-User": "bob"},
            )
            assert bob_resp.status == 404
            assert registry.validate_standalone_key(raw_key) is not None

            alice_resp = await client.delete(
                f"/api/nodes/tokens/{entry.id}",
                headers={"X-Test-User": "alice"},
            )

        assert alice_resp.status == 200
        assert registry.validate_standalone_key(raw_key) is None

    async def test_revoke_node_token_emits_audit_logs(self, app, registry, caplog):
        caplog.set_level(logging.WARNING)
        raw_key, entry = registry.generate_api_key("alice-node", username="alice")

        async with TestClient(TestServer(app)) as client:
            bob_resp = await client.delete(
                f"/api/nodes/tokens/{entry.id}",
                headers={"X-Test-User": "bob"},
            )
            alice_resp = await client.delete(
                f"/api/nodes/tokens/{entry.id}",
                headers={"X-Test-User": "alice"},
            )

        assert bob_resp.status == 404
        rejected = _audit_records(caplog, "node_token_revoke_rejected")
        assert rejected[-1].path == f"/api/nodes/tokens/{entry.id}"
        assert rejected[-1].username == "bob"
        assert rejected[-1].api_key_id == entry.id
        assert rejected[-1].reason == "not_found_or_forbidden"
        assert rejected[-1].ip

        assert alice_resp.status == 200
        assert registry.validate_standalone_key(raw_key) is None
        revoked = _audit_records(caplog, "node_token_revoked")
        assert revoked[-1].path == f"/api/nodes/tokens/{entry.id}"
        assert revoked[-1].username == "alice"
        assert revoked[-1].api_key_id == entry.id
        assert revoked[-1].closed_ws_count == 0
        assert revoked[-1].ip

    async def test_legacy_revoke_node_key_emits_legacy_path_in_audit_log(self, app, registry, caplog):
        caplog.set_level(logging.WARNING)
        _, entry = registry.generate_api_key("legacy-node", username="alice")

        async with TestClient(TestServer(app)) as client:
            resp = await client.delete(
                f"/api/nodes/keys/{entry.id}",
                headers={"X-Test-User": "alice"},
            )

        assert resp.status == 200
        records = _audit_records(caplog, "node_token_revoked")
        assert records[-1].path == f"/api/nodes/keys/{entry.id}"
        assert records[-1].api_key_id == entry.id

    async def test_revoke_node_token_closes_bound_online_node_ws(self, app, registry):
        class FakeWs:
            def __init__(self):
                self.closed = False
                self.close_calls = []

            async def close(self, *, code, message):
                self.closed = True
                self.close_calls.append((code, message))

        raw_key, entry = registry.generate_api_key("alice-node", username="alice")
        node = registry.register("alice-node", raw_key)
        ws = FakeWs()
        registry.set_online(node.id, ws)  # type: ignore[arg-type]

        async with TestClient(TestServer(app)) as client:
            resp = await client.delete(
                f"/api/nodes/tokens/{entry.id}",
                headers={"X-Test-User": "alice"},
            )

        assert resp.status == 200
        assert ws.closed is True
        assert ws.close_calls == [(4001, b"Node token revoked")]
        assert node.status == "offline"

    async def test_register_node_rejects_revoked_token_with_audit_log(
        self,
        app,
        registry,
        caplog,
    ):
        caplog.set_level(logging.WARNING)
        raw_key, entry = registry.generate_api_key("revoked-node", username="alice")
        registry.revoke_api_key(entry.id, username="alice")

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/nodes/register",
                json={"name": "revoked-node", "apiKey": raw_key, "capabilities": {}},
            )
            body = await resp.json()

        assert resp.status == 403
        assert body == {"error": "Invalid API key for new node"}
        records = _audit_records(caplog, "node_register_rejected")
        assert records[-1].path == "/api/nodes/register"
        assert records[-1].node_name == "revoked-node"
        assert records[-1].reason == "invalid_token"
        assert records[-1].error_message == "Invalid API key for new node"
        assert records[-1].ip

    async def test_register_node_hides_bound_token_rejection_on_wire(
        self,
        app,
        registry,
        caplog,
    ):
        caplog.set_level(logging.WARNING)
        raw_key, _ = registry.generate_api_key("bound-node", username="alice")
        registry.register("bound-node", raw_key)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/nodes/register",
                json={"name": "other-node", "apiKey": raw_key, "capabilities": {}},
            )
            body = await resp.json()

        assert resp.status == 403
        assert body == {"error": "Invalid API key for new node"}
        records = _audit_records(caplog, "node_register_rejected")
        assert records[-1].path == "/api/nodes/register"
        assert records[-1].node_name == "other-node"
        assert records[-1].reason == "bound_elsewhere"
        assert records[-1].error_message == "API key is already bound to another node"

    async def test_register_node_rate_limit_emits_audit_log(self, app, caplog):
        caplog.set_level(logging.WARNING)

        async with TestClient(TestServer(app)) as client:
            for i in range(10):
                resp = await client.post(
                    "/api/nodes/register",
                    json={
                        "name": f"probe-{i}",
                        "apiKey": "sk-node-unissued",
                        "capabilities": {},
                    },
                )
                assert resp.status == 403

            resp = await client.post(
                "/api/nodes/register",
                json={
                    "name": "probe-limited",
                    "apiKey": "sk-node-unissued",
                    "capabilities": {},
                },
            )
            body = await resp.json()

        assert resp.status == 429
        assert body == {"error": "Too many requests"}
        records = _audit_records(caplog, "node_register_rate_limited")
        assert records[-1].path == "/api/nodes/register"
        assert records[-1].ip

    async def test_register_node_rate_limit_trusts_forwarded_for_when_enabled(
        self,
        app,
        monkeypatch,
    ):
        monkeypatch.setenv("VIBR8_TRUST_PROXY", "1")

        async with TestClient(TestServer(app)) as client:
            for i in range(10):
                resp = await client.post(
                    "/api/nodes/register",
                    json={
                        "name": f"probe-{i}",
                        "apiKey": "sk-node-unissued",
                        "capabilities": {},
                    },
                    headers={"X-Forwarded-For": "203.0.113.10"},
                )
                assert resp.status == 403

            resp = await client.post(
                "/api/nodes/register",
                json={
                    "name": "probe-other-ip",
                    "apiKey": "sk-node-unissued",
                    "capabilities": {},
                },
                headers={"X-Forwarded-For": "203.0.113.11"},
            )

        assert resp.status == 403

    async def test_register_node_success_emits_audit_log(self, app, registry, caplog):
        caplog.set_level(logging.INFO)
        raw_key, entry = registry.generate_api_key("new-node", username="alice")

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/nodes/register",
                json={"name": "new-node", "apiKey": raw_key, "capabilities": {}},
            )
            body = await resp.json()

        assert resp.status == 200
        records = _audit_records(caplog, "node_registered")
        assert records[-1].path == "/api/nodes/register"
        assert records[-1].node_name == "new-node"
        assert records[-1].node_id_prefix == body["nodeId"][:8]
        assert records[-1].api_key_id == entry.id

    async def test_register_node_success_emits_token_bound_audit_log(
        self,
        app,
        registry,
        caplog,
    ):
        caplog.set_level(logging.INFO)
        raw_key, entry = registry.generate_api_key("bound-new-node", username="alice")

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/nodes/register",
                json={
                    "name": "bound-new-node",
                    "apiKey": raw_key,
                    "capabilities": {},
                },
            )
            body = await resp.json()

        assert resp.status == 200
        records = _audit_records(caplog, "node_token_bound")
        assert records[-1].path == "/api/nodes/register"
        assert records[-1].node_name == "bound-new-node"
        assert records[-1].node_id_prefix == body["nodeId"][:8]
        assert records[-1].api_key_id == entry.id
        assert records[-1].ip

    async def test_reregister_same_node_does_not_reemit_token_bound_audit_log(
        self,
        app,
        registry,
        caplog,
    ):
        caplog.set_level(logging.INFO)
        raw_key, _ = registry.generate_api_key("same-token-node", username="alice")
        registry.register("same-token-node", raw_key)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/nodes/register",
                json={
                    "name": "same-token-node",
                    "apiKey": raw_key,
                    "capabilities": {"platform": "linux"},
                },
            )

        assert resp.status == 200
        assert _audit_records(caplog, "node_token_bound") == []

    async def test_reregister_existing_node_with_new_token_emits_bound_audit_log(
        self,
        app,
        registry,
        caplog,
    ):
        caplog.set_level(logging.INFO)
        raw_key1, _ = registry.generate_api_key("rotate-node", username="alice")
        raw_key2, entry2 = registry.generate_api_key("rotate-node-new", username="alice")
        node = registry.register("rotate-node", raw_key1)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/nodes/register",
                json={
                    "name": "rotate-node",
                    "apiKey": raw_key2,
                    "capabilities": {"platform": "linux"},
                },
            )

        assert resp.status == 200
        records = _audit_records(caplog, "node_token_bound")
        assert records[-1].path == "/api/nodes/register"
        assert records[-1].node_name == "rotate-node"
        assert records[-1].node_id_prefix == node.id[:8]
        assert records[-1].api_key_id == entry2.id


class TestNodeWebSocketAuth:
    """HTTP-level coverage for node tunnel authentication."""

    @pytest.fixture
    def app(self, bridge, registry):
        from server.main import BRIDGE_KEY, NODE_WS_RATE_KEY, handle_node_ws

        session_registry = MagicMock()
        session_registry.remove_node_sessions = MagicMock()

        app = web.Application()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", NotAppKeyWarning)
            app["node_registry"] = registry
            app["session_registry"] = session_registry
        app[BRIDGE_KEY] = bridge
        app[NODE_WS_RATE_KEY] = {}
        app.router.add_get("/ws/node/{node_id}", handle_node_ws)
        return app

    async def test_node_ws_rejects_revoked_token_bound_node(self, app, registry):
        raw_key, entry = registry.generate_api_key("revoked-node")
        node = registry.register("revoked-node", raw_key)
        registry.revoke_api_key(entry.id)

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect(f"/ws/node/{node.id}?apiKey={raw_key}")
            msg = await ws.receive(timeout=1.0)

        assert msg.type == web.WSMsgType.CLOSE
        assert ws.close_code == 4001
        assert node.status == "offline"

    async def test_node_ws_rejects_revoked_token_with_audit_log(
        self,
        app,
        registry,
        caplog,
    ):
        caplog.set_level(logging.WARNING)
        raw_key, entry = registry.generate_api_key("revoked-node")
        node = registry.register("revoked-node", raw_key)
        registry.revoke_api_key(entry.id)

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect(f"/ws/node/{node.id}?apiKey={raw_key}")
            msg = await ws.receive(timeout=1.0)

        assert msg.type == web.WSMsgType.CLOSE
        records = _audit_records(caplog, "node_ws_rejected")
        assert records[-1].node_id_prefix == node.id[:8]
        assert records[-1].api_key_id == entry.id
        assert records[-1].reason == "invalid_or_revoked_token"
        assert records[-1].ip
        assert records[-1].attempted_api_key_prefix == raw_key[:16] + "..."

    async def test_node_ws_rejects_unknown_node_with_audit_log(self, app, registry, caplog):
        caplog.set_level(logging.WARNING)

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws/node/missing-node?apiKey=sk-node-nope")
            msg = await ws.receive(timeout=1.0)

        assert msg.type == web.WSMsgType.CLOSE
        records = _audit_records(caplog, "node_ws_rejected")
        assert records[-1].node_id_prefix == "missing-"
        assert records[-1].reason == "unknown_node"
        assert records[-1].ip
        assert records[-1].attempted_api_key_prefix == "sk-node-nope..."

    async def test_node_ws_rate_limit_emits_audit_log(self, app, caplog):
        caplog.set_level(logging.WARNING)

        async with TestClient(TestServer(app)) as client:
            for _ in range(10):
                ws = await client.ws_connect(
                    "/ws/node/missing-node?apiKey=sk-node-nope"
                )
                msg = await ws.receive(timeout=1.0)
                assert msg.type == web.WSMsgType.CLOSE
                assert ws.close_code == 4001

            with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
                await client.ws_connect(
                    "/ws/node/missing-node?apiKey=sk-node-nope"
                )

        assert exc.value.status == 429
        records = _audit_records(caplog, "node_ws_rate_limited")
        assert records[-1].node_id_prefix == "missing-"
        assert records[-1].ip

    async def test_node_ws_rate_limit_trusts_forwarded_for_when_enabled(
        self,
        app,
        monkeypatch,
    ):
        monkeypatch.setenv("VIBR8_TRUST_PROXY", "1")

        async with TestClient(TestServer(app)) as client:
            for _ in range(10):
                ws = await client.ws_connect(
                    "/ws/node/missing-node?apiKey=sk-node-nope",
                    headers={"X-Forwarded-For": "203.0.113.10"},
                )
                msg = await ws.receive(timeout=1.0)
                assert msg.type == web.WSMsgType.CLOSE
                assert ws.close_code == 4001

            ws = await client.ws_connect(
                "/ws/node/missing-node?apiKey=sk-node-nope",
                headers={"X-Forwarded-For": "203.0.113.11"},
            )
            msg = await ws.receive(timeout=1.0)

        assert msg.type == web.WSMsgType.CLOSE
        assert ws.close_code == 4001
