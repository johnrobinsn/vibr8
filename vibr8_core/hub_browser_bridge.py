"""HubBrowserBridge — hub-only browser/client tracking and broadcasts.

The hub owns the user's browser connections (browser WebSocket, native WS,
WebRTC peer sockets). All operations that affect the user-facing UI —
broadcasts, client metadata, computer-use agent registration — live here.

**Today**: a thin facade over the in-process `WsBridge`. Every method
delegates. This lets us start migrating callers from `ws_bridge.*` to
`hub_browser_bridge.*` without changing behavior.

**After Phase 4c-4 (the keystone)**: the hub no longer instantiates a
session-state `WsBridge`. This class becomes the real implementation,
holding its own `session_id → browser_sockets` map, `client_id →
metadata` registry, and broadcast helpers. It receives messages
forwarded from the tunnel and fans out to subscribed browsers.

Splitting via a facade now means the keystone PR can swap the backing
without touching call sites.
"""

from __future__ import annotations

from typing import Any


class HubBrowserBridge:
    """Hub-side browser/client tracking + broadcasts."""

    def __init__(self, ws_bridge: Any) -> None:
        # Today: backed by the in-process WsBridge. Phase 4c-4 will replace
        # this with the real implementation that owns its own state.
        self._ws_bridge = ws_bridge
        # Per-client active node. Each browser client (and eventually each
        # tab) picks its own active node; voice routing and node-scoped UI
        # operations read this map. Replaces hub-wide
        # `node_registry.active_node_id`.
        self._client_active_nodes: dict[str, str] = {}

    # Enumerated explicitly for IDE discovery and to document the surface;
    # __getattr__ below handles anything we missed.

    # ── Broadcast helpers ────────────────────────────────────────────────

    async def broadcast_name_update(self, session_id: str, name: str, user_renamed: bool = False) -> None:
        return await self._ws_bridge.broadcast_name_update(session_id, name, user_renamed)

    async def send_ring0_switch_ui(
        self, target_session_id: str, *, client_id: str,
    ) -> bool:
        return await self._ws_bridge.send_ring0_switch_ui(
            target_session_id, client_id=client_id,
        )

    async def send_ring0_switch_node(self, node_id: str, *, client_id: str) -> bool:
        return await self._ws_bridge.send_ring0_switch_node(node_id, client_id=client_id)

    async def broadcast_to_all_browsers(self, msg: dict[str, Any]) -> None:
        return await self._ws_bridge.broadcast_to_all_browsers(msg)

    async def broadcast_guard_state(self, session_id: str, enabled: bool, *, client_id: str | None = None) -> None:
        return await self._ws_bridge.broadcast_guard_state(session_id, enabled, client_id=client_id)

    async def broadcast_audio_off(self, session_id: str) -> None:
        return await self._ws_bridge.broadcast_audio_off(session_id)

    async def broadcast_tts_muted(self, session_id: str, muted: bool, *, client_id: str | None = None) -> None:
        return await self._ws_bridge.broadcast_tts_muted(session_id, muted, client_id=client_id)

    async def broadcast_voice_mode(self, session_id: str, mode: str | None, *, client_id: str | None = None) -> None:
        return await self._ws_bridge.broadcast_voice_mode(session_id, mode, client_id=client_id)

    # ── Client metadata ──────────────────────────────────────────────────

    def set_client_metadata(self, client_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        return self._ws_bridge.set_client_metadata(client_id, updates)

    def register_device_info(self, client_id: str, device_info: dict[str, Any]) -> dict[str, Any]:
        return self._ws_bridge.register_device_info(client_id, device_info)

    def get_all_clients(self) -> dict[str, dict[str, str]]:
        return self._ws_bridge.get_all_clients()

    def get_ring0_prompt_client(self) -> str:
        return self._ws_bridge.get_ring0_prompt_client()

    # ── Per-client active node ───────────────────────────────────────────

    def set_client_active_node(self, client_id: str, node_id: str) -> None:
        if not client_id:
            return
        self._client_active_nodes[client_id] = node_id or ""

    def get_client_active_node(self, client_id: str, default: str = "") -> str:
        if not client_id:
            return default
        return self._client_active_nodes.get(client_id, default)

    def clear_client_active_node(self, client_id: str) -> None:
        self._client_active_nodes.pop(client_id, None)

    # ── Catch-all ────────────────────────────────────────────────────────

    def __getattr__(self, name: str) -> Any:
        # Anything not explicitly enumerated falls through to the underlying
        # bridge. After Phase 4c-4 this should narrow as the real impl takes
        # over the hub-only surface.
        return getattr(self._ws_bridge, name)
