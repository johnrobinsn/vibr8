"""Android Registry — manages ADB-connected Android devices as virtual nodes.

Android nodes are "virtual" — they're registered by the hub itself (no
separate vibr8_node process). The hub manages ADB/scrcpy connections directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from server import adb_utils

logger = logging.getLogger(__name__)

VIBR8_DIR = Path.home() / ".vibr8"
ANDROID_NODES_FILE = VIBR8_DIR / "android-nodes.json"

# Reconnect config
MAX_RECONNECT_ATTEMPTS = 3
RECONNECT_DELAYS = [2.0, 4.0, 8.0]


@dataclass
class AndroidNode:
    """A registered Android device (virtual node)."""

    id: str                          # UUID
    name: str                        # User-given name (e.g., "Pixel 9")
    connection_mode: str             # "usb" | "ip" | "mdns"
    device_id: str                   # ADB serial (e.g., "XXXXXXXX" or "192.168.1.50:5555")
    status: str = "offline"          # "online" | "offline" | "unauthorized"
    ip: Optional[str] = None         # For IP/mDNS mode
    port: Optional[int] = None       # For IP/mDNS mode
    capabilities: dict[str, Any] = field(default_factory=dict)
    last_seen: float = 0             # time.time()
    # Runtime state (not persisted)
    scrcpy_client: Any = None        # ScrcpyClient instance
    _reconnect_task: Optional[asyncio.Task[None]] = None

    @property
    def node_type(self) -> str:
        return "android"

    def to_dict(self) -> dict[str, Any]:
        """Serialize for persistence."""
        d: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "connectionMode": self.connection_mode,
            "deviceId": self.device_id,
            "capabilities": self.capabilities,
        }
        if self.ip:
            d["ip"] = self.ip
        if self.port:
            d["port"] = self.port
        return d

    def to_api_dict(self) -> dict[str, Any]:
        """Serialize for browser API responses."""
        return {
            "id": self.id,
            "name": self.name,
            "nodeType": "android",
            "connectionMode": self.connection_mode,
            "deviceId": self.device_id,
            "status": self.status,
            "ip": self.ip,
            "port": self.port,
            "capabilities": self.capabilities,
            "canRunSessions": False,
            "hasDisplay": True,
            "lastSeen": self.last_seen,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> AndroidNode:
        return AndroidNode(
            id=data["id"],
            name=data["name"],
            connection_mode=data.get("connectionMode", "usb"),
            device_id=data["deviceId"],
            capabilities=data.get("capabilities", {}),
            ip=data.get("ip"),
            port=data.get("port"),
        )


class AndroidRegistry:
    """Central registry for ADB-connected Android devices."""

    def __init__(self) -> None:
        self._nodes: dict[str, AndroidNode] = {}  # node_id → node
        self._poll_task: Optional[asyncio.Task[None]] = None
        self._load()

    # ── Public API ────────────────────────────────────────────────────────

    def register(
        self,
        name: str,
        connection_mode: str,
        device_id: str,
        ip: Optional[str] = None,
        port: Optional[int] = None,
    ) -> AndroidNode:
        """Register a new Android device."""
        # Check for duplicate device_id
        for node in self._nodes.values():
            if node.device_id == device_id:
                raise ValueError(f"Device {device_id} is already registered as '{node.name}'")

        node_id = f"android-{secrets.token_hex(8)}"
        node = AndroidNode(
            id=node_id,
            name=name,
            connection_mode=connection_mode,
            device_id=device_id,
            ip=ip,
            port=port,
        )
        self._nodes[node_id] = node
        self._save()
        logger.info("[android] Registered device %r (id=%s, serial=%s)", name, node_id[:12], device_id)
        return node

    def unregister(self, node_id: str) -> bool:
        """Remove an Android device."""
        node = self._nodes.pop(node_id, None)
        if not node:
            return False
        # Cancel any reconnect task
        if node._reconnect_task and not node._reconnect_task.done():
            node._reconnect_task.cancel()
        self._save()
        logger.info("[android] Unregistered device %r (id=%s)", node.name, node_id[:12])
        return True

    def update(self, node_id: str, **kwargs: Any) -> AndroidNode | None:
        """Update connection settings for an Android device."""
        node = self._nodes.get(node_id)
        if not node:
            return None
        for key in ("name", "ip", "port", "device_id", "connection_mode"):
            if key in kwargs:
                setattr(node, key, kwargs[key])
        self._save()
        return node

    def get_node(self, node_id: str) -> AndroidNode | None:
        return self._nodes.get(node_id)

    def get_all_nodes(self) -> list[AndroidNode]:
        return list(self._nodes.values())

    def get_online_nodes(self) -> list[AndroidNode]:
        return [n for n in self._nodes.values() if n.status == "online"]

    # ── Status management ─────────────────────────────────────────────────

    async def check_device(self, node_id: str) -> bool:
        """Check if a specific device is reachable and update status."""
        node = self._nodes.get(node_id)
        if not node:
            return False

        online = await adb_utils.is_device_online(node.device_id)
        if online:
            node.status = "online"
            node.last_seen = time.time()
            # Refresh device info if capabilities are empty
            if not node.capabilities.get("model"):
                await self._refresh_capabilities(node)
        else:
            node.status = "offline"
        return online

    async def connect_device(self, node: AndroidNode) -> bool:
        """Establish ADB connection for IP/mDNS mode devices."""
        if node.connection_mode == "usb":
            # USB devices are auto-connected, just check status
            return await self.check_device(node.id)

        if not node.ip:
            return False
        port = node.port or 5555
        try:
            await adb_utils.connect(node.ip, port)
            node.device_id = f"{node.ip}:{port}"
            node.status = "online"
            node.last_seen = time.time()
            await self._refresh_capabilities(node)
            self._save()
            return True
        except RuntimeError as e:
            logger.warning("[android] Connect failed for %s: %s", node.name, e)
            node.status = "offline"
            return False

    async def disconnect_device(self, node_id: str) -> bool:
        """Disconnect a wireless ADB device."""
        node = self._nodes.get(node_id)
        if not node:
            return False
        if node.connection_mode != "usb":
            await adb_utils.disconnect(node.device_id)
        node.status = "offline"
        return True

    async def reconnect_device(self, node_id: str) -> bool:
        """Attempt reconnection with retries. Returns True if reconnected."""
        node = self._nodes.get(node_id)
        if not node:
            return False

        for attempt, delay in enumerate(RECONNECT_DELAYS[:MAX_RECONNECT_ATTEMPTS]):
            logger.info("[android] Reconnect attempt %d/%d for %s...",
                        attempt + 1, MAX_RECONNECT_ATTEMPTS, node.name)
            if await self.connect_device(node):
                logger.info("[android] Reconnected to %s", node.name)
                return True
            if attempt < MAX_RECONNECT_ATTEMPTS - 1:
                await asyncio.sleep(delay)

        logger.warning("[android] Failed to reconnect to %s after %d attempts",
                       node.name, MAX_RECONNECT_ATTEMPTS)
        return False

    async def _refresh_capabilities(self, node: AndroidNode) -> None:
        """Fetch device info and update capabilities."""
        try:
            info = await adb_utils.device_info(node.device_id)
            node.capabilities = {
                "canRunSessions": False,
                "hasDisplay": True,
                "nodeType": "android",
                "model": info.get("model", ""),
                "manufacturer": info.get("manufacturer", ""),
                "androidVersion": info.get("androidVersion", ""),
                "screenWidth": int(info.get("screenWidth", 0)),
                "screenHeight": int(info.get("screenHeight", 0)),
            }
        except Exception as e:
            logger.warning("[android] Failed to refresh capabilities for %s: %s", node.name, e)

    # ── Discovery ─────────────────────────────────────────────────────────

    async def discover_devices(self) -> list[dict[str, Any]]:
        """List discoverable ADB devices not already registered.

        Returns USB-attached devices from `adb devices`.
        mDNS discovery is handled separately by mdns_discovery.py.
        """
        registered_serials = {n.device_id for n in self._nodes.values()}
        devices = await adb_utils.list_devices()
        result = []
        for dev in devices:
            if dev.serial in registered_serials:
                continue
            if dev.status != "device":
                continue  # Skip offline/unauthorized
            result.append({
                "serial": dev.serial,
                "model": dev.model,
                "status": dev.status,
                "transportId": dev.transport_id,
            })
        return result

    # ── Polling ───────────────────────────────────────────────────────────

    async def start_polling(self, interval: float = 5.0) -> None:
        """Start background polling for device status changes."""
        if self._poll_task and not self._poll_task.done():
            return
        self._poll_task = asyncio.create_task(self._poll_loop(interval))

    async def stop_polling(self) -> None:
        """Stop background polling."""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    async def _poll_loop(self, interval: float) -> None:
        """Periodically check USB device status."""
        while True:
            try:
                await self._poll_once()
            except Exception:
                logger.exception("[android] Poll error")
            await asyncio.sleep(interval)

    async def _poll_once(self) -> None:
        """Check all registered USB devices for status changes."""
        adb_devices = await adb_utils.list_devices()
        adb_serials = {d.serial: d for d in adb_devices}

        for node in self._nodes.values():
            if node.connection_mode != "usb":
                continue
            dev = adb_serials.get(node.device_id)
            was_online = node.status == "online"
            if dev and dev.status == "device":
                if not was_online:
                    node.status = "online"
                    node.last_seen = time.time()
                    logger.info("[android] USB device %s came online", node.name)
                    if not node.capabilities.get("model"):
                        await self._refresh_capabilities(node)
            else:
                if was_online:
                    node.status = "offline"
                    logger.info("[android] USB device %s went offline", node.name)

    # ── Persistence ───────────────────────────────────────────────────────

    def _load(self) -> None:
        if not ANDROID_NODES_FILE.exists():
            return
        try:
            data = json.loads(ANDROID_NODES_FILE.read_text())
            for node_data in data.get("nodes", []):
                node = AndroidNode.from_dict(node_data)
                self._nodes[node.id] = node
            logger.info("[android] Loaded %d device(s) from %s", len(self._nodes), ANDROID_NODES_FILE)
        except Exception:
            logger.exception("[android] Failed to load android nodes file")

    def _save(self) -> None:
        VIBR8_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "nodes": [n.to_dict() for n in self._nodes.values()],
        }
        ANDROID_NODES_FILE.write_text(json.dumps(data, indent=2))

    # ── Cleanup ───────────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Stop polling and clean up scrcpy clients."""
        await self.stop_polling()
        for node in self._nodes.values():
            if node.scrcpy_client:
                try:
                    await node.scrcpy_client.stop()
                except Exception:
                    pass
                node.scrcpy_client = None
