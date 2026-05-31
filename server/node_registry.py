"""Node Registry — tracks remote vibr8 nodes that register with the hub.

Each node is a host running Ring0 + Claude Code sessions. The hub maintains
a registry of all nodes, their status, and active node selection.
"""

from __future__ import annotations

import json
import logging
import platform
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import bcrypt
from aiohttp import web

logger = logging.getLogger(__name__)

VIBR8_DIR = Path.home() / ".vibr8"
NODES_FILE = VIBR8_DIR / "nodes.json"


@dataclass
class ApiKeyEntry:
    """Metadata for an issued API key."""
    id: str                    # short unique ID
    name: str                  # user-given label
    key_hash: str              # bcrypt hash
    key_prefix: str            # first 12 chars for display (e.g. "sk-node-1234...")
    username: str = ""         # authenticated user that created this key
    node_id: str = ""          # node identity this key is bound to after use
    created_at: float = 0     # time.time()
    last_used_at: float = 0   # time.time(), 0 = never used
    revoked_at: float = 0     # time.time(), 0 = active

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "keyHash": self.key_hash,
            "keyPrefix": self.key_prefix,
            "username": self.username,
            "nodeId": self.node_id,
            "createdAt": self.created_at,
            "lastUsedAt": self.last_used_at,
            "revokedAt": self.revoked_at,
        }

    def to_api_dict(self) -> dict[str, Any]:
        """For browser API — no hash."""
        return {
            "id": self.id,
            "name": self.name,
            "keyPrefix": self.key_prefix,
            "username": self.username,
            "nodeId": self.node_id or None,
            "createdAt": self.created_at,
            "lastUsedAt": self.last_used_at,
            "revokedAt": self.revoked_at or None,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ApiKeyEntry:
        return ApiKeyEntry(
            id=data["id"],
            name=data["name"],
            key_hash=data["keyHash"],
            key_prefix=data.get("keyPrefix", "sk-node-****"),
            username=data.get("username", ""),
            node_id=data.get("nodeId", ""),
            created_at=data.get("createdAt", 0),
            last_used_at=data.get("lastUsedAt", 0),
            revoked_at=data.get("revokedAt", 0),
        )


@dataclass
class RegisteredNode:
    id: str                                  # UUID assigned by hub
    name: str                                # User-friendly name (e.g., "cloud-dev")
    api_key_hash: str                        # bcrypt hash of API key
    api_key_id: str = ""                     # issued key/token id used to register
    capabilities: dict[str, Any] = field(default_factory=dict)
    status: str = "offline"                  # "online" | "offline"
    last_heartbeat: float = 0                # time.time()
    session_ids: list[str] = field(default_factory=list)
    ring0_enabled: bool = False
    ws: Optional[web.WebSocketResponse] = None  # Live tunnel WS (not persisted)
    tunnel: Any = None                       # NodeTunnel instance (not persisted)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for persistence (excludes live ws/tunnel)."""
        return {
            "id": self.id,
            "name": self.name,
            "apiKeyHash": self.api_key_hash,
            "apiKeyId": self.api_key_id,
            "capabilities": self.capabilities,
            "ring0Enabled": self.ring0_enabled,
        }

    def to_api_dict(self) -> dict[str, Any]:
        """Serialize for browser API responses."""
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "platform": self.capabilities.get("platform", ""),
            "hostname": self.capabilities.get("hostname", ""),
            "sessionCount": len(self.session_ids),
            "ring0Enabled": self.ring0_enabled,
            "defaultBackend": self.capabilities.get("defaultBackend", "claude"),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> RegisteredNode:
        return RegisteredNode(
            id=data["id"],
            name=data["name"],
            api_key_hash=data["apiKeyHash"],
            api_key_id=data.get("apiKeyId", ""),
            capabilities=data.get("capabilities", {}),
            ring0_enabled=data.get("ring0Enabled", False),
        )


class NodeRegistry:
    """Central registry of all nodes, including the local hub node.

    The in-memory lock is per instance; production should use one registry
    instance per process so node/token mutations serialize around one state.
    """

    LOCAL_NODE_ID = "local"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._nodes: dict[str, RegisteredNode] = {}  # node_id → node
        self._api_keys: dict[str, ApiKeyEntry] = {}  # key_id → entry
        self._hub_name: str = platform.node() or "Local"
        self._load()
        # Ensure the local node always has an entry
        self._ensure_local_node()

    def _ensure_local_node(self) -> None:
        """Create the local node entry if it doesn't exist."""
        if self.LOCAL_NODE_ID not in self._nodes:
            self._nodes[self.LOCAL_NODE_ID] = RegisteredNode(
                id=self.LOCAL_NODE_ID,
                name=self._hub_name,
                api_key_hash="",  # local node doesn't authenticate
                capabilities={"platform": platform.system(), "hostname": platform.node()},
                status="online",
                last_heartbeat=time.time(),
            )

    @property
    def local_node(self) -> RegisteredNode:
        """The always-present local hub node."""
        return self._nodes[self.LOCAL_NODE_ID]

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def hub_name(self) -> str:
        return self._hub_name

    @hub_name.setter
    def hub_name(self, name: str) -> None:
        with self._lock:
            self._hub_name = name.strip() or platform.node() or "Local"
            self.local_node.name = self._hub_name
            self._save()

    def register(
        self,
        name: str,
        api_key: str,
        capabilities: dict[str, Any] | None = None,
    ) -> RegisteredNode:
        """Register a new node or re-register an existing one by name."""
        with self._lock:
            # Check if a node with this name already exists
            existing = self.get_node_by_name(name)
            if existing:
                # Re-registration: validate against stored key first, then standalone key pool
                if not self.validate_api_key(existing.id, api_key):
                    key_entry = self.validate_standalone_key(api_key)
                    if not key_entry:
                        raise PermissionError("Invalid API key for existing node")
                    if key_entry.node_id and key_entry.node_id != existing.id:
                        raise PermissionError("API key is already bound to another node")
                    # Valid standalone key — update stored hash
                    existing.api_key_hash = bcrypt.hashpw(
                        api_key.encode(), bcrypt.gensalt()
                    ).decode()
                    existing.api_key_id = key_entry.id
                    key_entry.node_id = existing.id
                if capabilities:
                    existing.capabilities = capabilities
                self._save()
                return existing

            # New registration — validate against issued keys and update last_used
            key_entry = self.validate_standalone_key(api_key)
            if not key_entry:
                raise PermissionError("Invalid API key for new node")
            if key_entry.node_id:
                raise PermissionError("API key is already bound to another node")

            node_id = secrets.token_hex(16)
            api_key_hash = bcrypt.hashpw(
                api_key.encode(), bcrypt.gensalt()
            ).decode()
            key_entry.node_id = node_id
            node = RegisteredNode(
                id=node_id,
                name=name,
                api_key_hash=api_key_hash,
                api_key_id=key_entry.id,
                capabilities=capabilities or {},
            )
            self._nodes[node_id] = node
            self._save()
            logger.info("[nodes] Registered node %r (id=%s)", name, node_id[:8])
            return node

    def unregister(self, node_id: str) -> bool:
        """Remove a node from the registry."""
        with self._lock:
            node = self._nodes.pop(node_id, None)
            if not node:
                return False
            self._save()
            logger.info("[nodes] Unregistered node %r (id=%s)", node.name, node_id[:8])
            return True

    def get_node(self, node_id: str) -> RegisteredNode | None:
        with self._lock:
            return self._nodes.get(node_id)

    def get_node_by_name(self, name: str) -> RegisteredNode | None:
        """Exact match by name (case-insensitive)."""
        with self._lock:
            name_lower = name.lower()
            for node in self._nodes.values():
                if node.name.lower() == name_lower:
                    return node
            return None

    def find_by_name(self, query: str) -> list[RegisteredNode]:
        """Fuzzy match: case-insensitive partial match on node name."""
        with self._lock:
            query_lower = query.lower()
            matches = []
            for node in self._nodes.values():
                if query_lower in node.name.lower():
                    matches.append(node)
            return matches

    def get_all_nodes(self) -> list[RegisteredNode]:
        with self._lock:
            return list(self._nodes.values())

    def get_nodes_by_api_key_id(self, key_id: str) -> list[RegisteredNode]:
        with self._lock:
            return [node for node in self._nodes.values() if node.api_key_id == key_id]

    def validate_api_key(self, node_id: str, api_key: str) -> bool:
        with self._lock:
            node = self._nodes.get(node_id)
            if not node or not node.api_key_hash:
                return False
            if node.api_key_id:
                entry = self._api_keys.get(node.api_key_id)
                if not entry or entry.revoked_at:
                    return False
            return bcrypt.checkpw(api_key.encode(), node.api_key_hash.encode())

    def validate_api_key_any(self, api_key: str) -> RegisteredNode | None:
        """Validate an API key against all registered nodes. Returns the matching node."""
        with self._lock:
            for node in self._nodes.values():
                if node.api_key_hash and self.validate_api_key(node.id, api_key):
                    return node
            return None

    def generate_api_key(
        self,
        name: str = "",
        username: str | None = None,
    ) -> tuple[str, ApiKeyEntry]:
        """Generate a new API key with metadata. Returns (raw_key, entry)."""
        with self._lock:
            raw_key = f"sk-node-{secrets.token_hex(24)}"
            key_hash = bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt()).decode()
            entry = ApiKeyEntry(
                id=secrets.token_hex(8),
                name=name or "Unnamed key",
                key_hash=key_hash,
                key_prefix=raw_key[:16] + "...",
                username=username or "",
                created_at=time.time(),
            )
            self._api_keys[entry.id] = entry
            self._save()
            logger.info("[nodes] Generated API key %r (id=%s)", entry.name, entry.id)
            return raw_key, entry

    def list_api_keys(self, username: str | None = None) -> list[ApiKeyEntry]:
        """Return all API key entries (no raw keys)."""
        with self._lock:
            keys = [
                key for key in self._api_keys.values()
                if not key.revoked_at
                and (username is None or key.username in ("", username))
            ]
            return sorted(keys, key=lambda k: k.created_at, reverse=True)

    def revoke_api_key(self, key_id: str, username: str | None = None) -> bool:
        """Revoke an API key by ID."""
        with self._lock:
            entry = self._api_keys.get(key_id)
            if not entry or entry.revoked_at:
                return False
            if username is not None and entry.username and entry.username != username:
                return False
            entry.revoked_at = time.time()
            for node in self._nodes.values():
                if node.api_key_id == key_id:
                    self.set_offline(node.id)
            self._save()
            logger.info("[nodes] Revoked API key %r (id=%s)", entry.name, key_id)
            return True

    def validate_standalone_key(self, api_key: str) -> ApiKeyEntry | None:
        """Validate an API key against the issued keys list. Updates last_used."""
        with self._lock:
            for entry in self._api_keys.values():
                if entry.revoked_at:
                    continue
                if bcrypt.checkpw(api_key.encode(), entry.key_hash.encode()):
                    entry.last_used_at = time.time()
                    self._save()
                    return entry
            return None

    # ── Status management ─────────────────────────────────────────────────

    def heartbeat(
        self,
        node_id: str,
        session_count: int | None = None,
        ring0_enabled: bool | None = None,
    ) -> None:
        with self._lock:
            node = self._nodes.get(node_id)
            if not node:
                return
            node.last_heartbeat = time.time()
            if session_count is not None and len(node.session_ids) != session_count:
                pass  # session_ids updated via update_sessions()
            if ring0_enabled is not None:
                node.ring0_enabled = ring0_enabled

    def set_online(self, node_id: str, ws: web.WebSocketResponse) -> None:
        with self._lock:
            node = self._nodes.get(node_id)
            if not node:
                return
            node.status = "online"
            node.ws = ws
            node.last_heartbeat = time.time()
            logger.info("[nodes] Node %r is online", node.name)

    def set_offline(self, node_id: str) -> None:
        with self._lock:
            node = self._nodes.get(node_id)
            if not node:
                return
            was_online = node.status == "online"
            node.status = "offline"
            node.ws = None
            node.tunnel = None
            node.session_ids = []
            if was_online:
                logger.info("[nodes] Node %r is offline", node.name)

    def update_sessions(self, node_id: str, session_ids: list[str]) -> None:
        with self._lock:
            node = self._nodes.get(node_id)
            if node:
                node.session_ids = session_ids

    def check_heartbeats(self, timeout: float = 90.0) -> list[str]:
        """Mark nodes offline if heartbeat is stale. Returns list of newly-offline node IDs."""
        with self._lock:
            now = time.time()
            newly_offline: list[str] = []
            for node in self._nodes.values():
                if node.id == self.LOCAL_NODE_ID:
                    continue  # local node is always online
                if node.status == "online" and (now - node.last_heartbeat) > timeout:
                    self.set_offline(node.id)
                    newly_offline.append(node.id)
            return newly_offline

    # ── Persistence ───────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load persisted state during initialization before concurrent use."""
        if not NODES_FILE.exists():
            return
        try:
            data = json.loads(NODES_FILE.read_text())
            for node_data in data.get("nodes", {}).values():
                node = RegisteredNode.from_dict(node_data)
                self._nodes[node.id] = node
            if data.get("hubName"):
                self._hub_name = data["hubName"]
            for key_data in data.get("apiKeys", {}).values():
                entry = ApiKeyEntry.from_dict(key_data)
                self._api_keys[entry.id] = entry
            logger.info("[nodes] Loaded %d node(s), %d API key(s) from %s (hub=%s)",
                        len(self._nodes), len(self._api_keys), NODES_FILE, self._hub_name)
        except Exception:
            logger.exception("[nodes] Failed to load nodes file")

    def _save(self) -> None:
        with self._lock:
            VIBR8_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "nodes": {nid: n.to_dict() for nid, n in self._nodes.items() if nid != self.LOCAL_NODE_ID},
                "apiKeys": {kid: k.to_dict() for kid, k in self._api_keys.items()},
                "hubName": self._hub_name,
            }
            NODES_FILE.write_text(json.dumps(data, indent=2))
