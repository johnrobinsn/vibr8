"""Unified session registry — single source of truth for all sessions across nodes.

Every session (local or remote) gets a canonical entry here. Routes and MCP
tools resolve session IDs through the registry and get back a SessionRouter
that abstracts local vs. tunneled dispatch.

Local sessions:   qualified_id = raw UUID, node_id = "local"
Remote sessions:  qualified_id = "nodeId:rawUUID", node_id = nodeId
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from server.cli_launcher import CliLauncher, SdkSessionInfo
    from server.node_registry import NodeRegistry
    from server.ws_bridge import WsBridge

logger = logging.getLogger(__name__)

LOCAL_NODE_ID = "local"


@dataclass
class SessionEntry:
    qualified_id: str
    node_id: str
    raw_id: str
    name: str = ""
    state: str = "starting"
    backend_type: str = "claude"
    cwd: str = ""
    is_ring0: bool = False
    model: str = ""
    archived: bool = False


# ── Router protocol ──────────────────────────────────────────────────────────


@runtime_checkable
class SessionRouter(Protocol):
    async def send_message(self, text: str, source_client_id: str = "") -> str | None: ...
    def interrupt(self) -> bool: ...
    def get_message_history(self) -> list[dict[str, Any]]: ...
    def get_pending_permissions(self) -> list[dict[str, Any]]: ...
    def get_pending_permission_count(self) -> int: ...
    async def respond_permission(self, request_id: str, behavior: str, message: str = "") -> bool: ...
    async def kill(self) -> bool: ...


class LocalSessionRouter:
    """Routes operations to the local WsBridge and CliLauncher."""

    def __init__(self, raw_id: str, ws_bridge: WsBridge, launcher: CliLauncher) -> None:
        self._raw_id = raw_id
        self._bridge = ws_bridge
        self._launcher = launcher

    async def send_message(self, text: str, source_client_id: str = "") -> str | None:
        return await self._bridge.submit_user_message(self._raw_id, text, source_client_id=source_client_id)

    def interrupt(self) -> bool:
        return self._bridge.interrupt_session(self._raw_id)

    def get_message_history(self) -> list[dict[str, Any]]:
        return self._bridge.get_message_history(self._raw_id)

    def get_pending_permissions(self) -> list[dict[str, Any]]:
        return self._bridge.get_pending_permissions(self._raw_id)

    def get_pending_permission_count(self) -> int:
        return self._bridge.get_pending_permission_count(self._raw_id)

    async def respond_permission(self, request_id: str, behavior: str, message: str = "") -> bool:
        return await self._bridge.respond_to_permission(self._raw_id, request_id, behavior, message)

    async def kill(self) -> bool:
        return await self._launcher.kill(self._raw_id)


class TunneledSessionRouter:
    """Routes operations through a remote node's WebSocket tunnel.

    Message history and pending permissions are read from the hub-side
    proxy session in WsBridge (populated by handle_remote_session_message).
    Write operations (send, interrupt, kill, permission response) are
    forwarded to the node via the tunnel.
    """

    def __init__(
        self,
        raw_id: str,
        qualified_id: str,
        node_id: str,
        node_registry: NodeRegistry,
        ws_bridge: WsBridge,
    ) -> None:
        self._raw_id = raw_id
        self._qualified_id = qualified_id
        self._node_id = node_id
        self._node_registry = node_registry
        self._bridge = ws_bridge

    def _get_tunnel(self):
        node = self._node_registry.get_node(self._node_id)
        if node and node.tunnel and node.tunnel.connected:
            return node.tunnel
        return None

    async def send_message(self, text: str, source_client_id: str = "") -> str | None:
        tunnel = self._get_tunnel()
        if not tunnel:
            return "Remote node unavailable"
        await tunnel.send_fire_and_forget({
            "type": "submit_message",
            "sessionId": self._raw_id,
            "content": text,
            "sourceClientId": source_client_id,
        })
        return None

    def interrupt(self) -> bool:
        tunnel = self._get_tunnel()
        if not tunnel:
            return False
        asyncio.ensure_future(tunnel.send_fire_and_forget({
            "type": "interrupt",
            "sessionId": self._raw_id,
        }))
        return True

    def get_message_history(self) -> list[dict[str, Any]]:
        return self._bridge.get_message_history(self._qualified_id)

    def get_pending_permissions(self) -> list[dict[str, Any]]:
        return self._bridge.get_pending_permissions(self._qualified_id)

    def get_pending_permission_count(self) -> int:
        return self._bridge.get_pending_permission_count(self._qualified_id)

    async def respond_permission(self, request_id: str, behavior: str, message: str = "") -> bool:
        tunnel = self._get_tunnel()
        if not tunnel:
            return False
        result = await tunnel.send_command({
            "type": "respond_permission",
            "sessionId": self._raw_id,
            "requestId": request_id,
            "behavior": behavior,
            "message": message,
        })
        return result.get("ok", False)

    async def kill(self) -> bool:
        tunnel = self._get_tunnel()
        if not tunnel:
            return False
        result = await tunnel.send_command({
            "type": "kill_session",
            "sessionId": self._raw_id,
        })
        return result.get("ok", False)


# ── Registry ─────────────────────────────────────────────────────────────────


class SessionRegistry:
    """Unified registry for all sessions across all nodes.

    Local sessions are keyed by their raw UUID (no prefix).
    Remote sessions are keyed by "nodeId:rawUUID".
    """

    def __init__(
        self,
        ws_bridge: WsBridge,
        launcher: CliLauncher,
        node_registry: NodeRegistry | None = None,
    ) -> None:
        self._entries: dict[str, SessionEntry] = {}
        self._bridge = ws_bridge
        self._launcher = launcher
        self._node_registry = node_registry

    # ── Qualified ID helpers ─────────────────────────────────────────────

    @staticmethod
    def qualify(node_id: str, raw_id: str) -> str:
        if node_id == LOCAL_NODE_ID:
            return raw_id
        return f"{node_id}:{raw_id}"

    @staticmethod
    def parse(qualified_id: str) -> tuple[str, str]:
        """Returns (node_id, raw_id)."""
        if ":" in qualified_id:
            parts = qualified_id.split(":", 1)
            return parts[0], parts[1]
        return LOCAL_NODE_ID, qualified_id

    # ── CRUD ─────────────────────────────────────────────────────────────

    def register(self, entry: SessionEntry) -> None:
        self._entries[entry.qualified_id] = entry

    def unregister(self, qualified_id: str) -> None:
        self._entries.pop(qualified_id, None)

    def update(self, qualified_id: str, **kwargs: Any) -> None:
        entry = self._entries.get(qualified_id)
        if entry:
            for k, v in kwargs.items():
                if hasattr(entry, k):
                    setattr(entry, k, v)

    def get(self, qualified_id: str) -> SessionEntry | None:
        return self._entries.get(qualified_id)

    # ── Resolution ───────────────────────────────────────────────────────

    def resolve(self, id_or_prefix: str, *, node_id: str | None = None) -> SessionEntry | None:
        """Resolve a full or prefix session ID to a SessionEntry.

        Lookup order:
        1. Exact match on qualified_id
        2. Prefix match on qualified_id
        3. Prefix match on raw_id (catches bare UUIDs for remote sessions)

        If `node_id` is provided, all matches are constrained to entries
        whose `node_id` equals it. Used to scope switch_ui to the caller's
        own node.
        """
        entry = self._entries.get(id_or_prefix)
        if entry and (node_id is None or entry.node_id == node_id):
            return entry

        for qid, entry in self._entries.items():
            if qid.startswith(id_or_prefix) and (node_id is None or entry.node_id == node_id):
                return entry

        for entry in self._entries.values():
            if entry.raw_id.startswith(id_or_prefix) and (node_id is None or entry.node_id == node_id):
                return entry

        return None

    def list_all(self, node_id: str = "") -> list[SessionEntry]:
        if node_id:
            return [e for e in self._entries.values() if e.node_id == node_id]
        return list(self._entries.values())

    # ── Routing ──────────────────────────────────────────────────────────

    def get_router(self, entry: SessionEntry) -> SessionRouter:
        if entry.node_id == LOCAL_NODE_ID:
            return LocalSessionRouter(entry.raw_id, self._bridge, self._launcher)
        if not self._node_registry:
            raise ValueError(f"No node registry for remote session {entry.qualified_id}")
        return TunneledSessionRouter(
            entry.raw_id, entry.qualified_id, entry.node_id,
            self._node_registry, self._bridge,
        )

    # ── Sync from launcher (local sessions) ──────────────────────────────

    def sync_from_launcher(self, ring0_session_id: str = "") -> None:
        """Refresh local entries from the CliLauncher's session list."""
        launcher_ids = set(self._launcher.get_all_session_ids())

        for raw_id in launcher_ids:
            qid = raw_id  # local sessions use raw UUID as key
            info = self._launcher.get_session(raw_id)
            if not info:
                continue
            existing = self._entries.get(qid)
            if existing:
                existing.state = info.state
                existing.archived = info.archived or False
                existing.name = info.name or existing.name
                existing.model = info.model or existing.model
            else:
                self.register(SessionEntry(
                    qualified_id=qid,
                    node_id=LOCAL_NODE_ID,
                    raw_id=raw_id,
                    name=info.name or "",
                    state=info.state,
                    backend_type=info.backendType or "claude",
                    cwd=info.cwd,
                    is_ring0=(raw_id == ring0_session_id),
                    model=info.model or "",
                    archived=info.archived or False,
                ))

        local_qids = [qid for qid, e in self._entries.items() if e.node_id == LOCAL_NODE_ID]
        for qid in local_qids:
            if qid not in launcher_ids:
                self.unregister(qid)

    # ── Sync from tunnel (remote sessions) ───────────────────────────────

    def sync_remote_sessions(self, node_id: str, sessions: list[dict[str, Any]]) -> None:
        """Update entries for a remote node from its sessions_update."""
        current_qids: set[str] = set()

        for s in sessions:
            raw_id = s.get("sessionId", s.get("id", ""))
            if not raw_id:
                continue
            # Strip node prefix if the caller already qualified it
            prefix = f"{node_id}:"
            if raw_id.startswith(prefix):
                raw_id = raw_id[len(prefix):]
            qid = self.qualify(node_id, raw_id)
            current_qids.add(qid)

            existing = self._entries.get(qid)
            if existing:
                existing.state = s.get("state", existing.state)
                existing.name = s.get("name", existing.name)
                existing.backend_type = s.get("backendType", existing.backend_type)
                existing.is_ring0 = s.get("isRing0", existing.is_ring0)
            else:
                self.register(SessionEntry(
                    qualified_id=qid,
                    node_id=node_id,
                    raw_id=raw_id,
                    name=s.get("name", ""),
                    state=s.get("state", "connected"),
                    backend_type=s.get("backendType", "claude"),
                    cwd=s.get("cwd", ""),
                    is_ring0=s.get("isRing0", False),
                ))

        stale = [qid for qid, e in self._entries.items()
                 if e.node_id == node_id and qid not in current_qids]
        for qid in stale:
            self.unregister(qid)

    def remove_node_sessions(self, node_id: str) -> None:
        """Remove all entries for a node (when it goes offline)."""
        to_remove = [qid for qid, e in self._entries.items() if e.node_id == node_id]
        for qid in to_remove:
            self.unregister(qid)
