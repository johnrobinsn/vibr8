"""Core WebSocket message router — bridges CLI ↔ browser connections."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
import uuid
from pathlib import Path
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Awaitable

import aiohttp
from aiohttp import web

if TYPE_CHECKING:
    from server.session_store import SessionStore, PersistedSession
    from server.codex_adapter import CodexAdapter
    from server.webrtc import WebRTCManager

logger = logging.getLogger(__name__)

# ── Session state ────────────────────────────────────────────────────────────

BackendType = str  # "claude" | "codex"


def _make_default_state(session_id: str, backend_type: str = "claude") -> dict[str, Any]:
    return {
        "session_id": session_id,
        "backend_type": backend_type,
        "model": "",
        "cwd": "",
        "tools": [],
        "permissionMode": "default",
        "claude_code_version": "",
        "mcp_servers": [],
        "agents": [],
        "slash_commands": [],
        "skills": [],
        "total_cost_usd": 0,
        "num_turns": 0,
        "context_used_percent": 0,
        "is_compacting": False,
        "git_branch": "",
        "is_worktree": False,
        "repo_root": "",
        "git_ahead": 0,
        "git_behind": 0,
        "total_lines_added": 0,
        "total_lines_removed": 0,
        "is_waiting_for_permission": False,
    }


@dataclass
class Session:
    id: str
    backend_type: str = "claude"
    cli_socket: web.WebSocketResponse | None = None
    codex_adapter: Any = None  # CodexAdapter
    browser_sockets: dict[web.WebSocketResponse, str] = field(default_factory=dict)  # ws → clientId
    state: dict[str, Any] = field(default_factory=dict)
    pending_permissions: dict[str, dict[str, Any]] = field(default_factory=dict)
    message_history: list[dict[str, Any]] = field(default_factory=list)
    pending_messages: list[str] = field(default_factory=list)
    _user_msg_counter: int = 0  # Counter for stable user message IDs
    _last_assistant_msg_id: str | None = None  # Last assistant msg_id, used as dedup key for results
    _dedup_msg_ids: set[str] = field(default_factory=set)
    _dedup_result_keys: set[str] = field(default_factory=set)
    _awaiting_replay: bool = False
    _replay_archived: bool = False
    # "Take the Pen" — per-session ownership control
    controlled_by: str = "ring0"  # "ring0" | "user"
    pen_taken_at: float = 0  # time.time() when user took pen
    _pen_timeout: Any = None  # asyncio.TimerHandle for auto-release
    last_prompted_at: float = 0  # ms since epoch, updated on every user prompt


# ── Bridge ───────────────────────────────────────────────────────────────────

class WsBridge:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._store: SessionStore | None = None
        self._webrtc_manager: WebRTCManager | None = None
        self._ring0_manager: Any = None  # Ring0Manager (avoid circular import)
        self._node_registry: Any = None  # NodeRegistry (set via setter)
        # _session_node_map removed: node identity is embedded in qualified session IDs
        # Format: "{node_id}:{raw_session_id}" for remote, raw id for local
        # Hook for vibr8-node: intercepts _broadcast_to_browsers for remote forwarding
        self._broadcast_hook: Callable[..., Awaitable[None]] | None = None
        self._computer_use_agents: dict[str, Any] = {}  # session_id → ComputerUseAgent
        self._active_tts: dict[str, Any] = {}  # session_id → TTS_OpenAI instance
        self._thinking_timers: dict[str, asyncio.TimerHandle] = {}
        self._on_cli_session_id: Callable[[str, str], None] | None = None
        self._on_cli_relaunch_needed: Callable[[str], None] | None = None
        self._on_first_turn_completed: Callable[[str, str], None] | None = None
        self._auto_naming_attempted: set[str] = set()
        # Client identity tracking
        self._client_sessions: dict[str, str] = {}  # clientId → sessionId
        self._ws_by_client: dict[str, web.WebSocketResponse] = {}  # clientId → ws
        self._client_roles: dict[str, str] = {}  # clientId → role ("primary" | "secondscreen")
        self._rpc_pending: dict[str, asyncio.Future] = {}  # rpc_id → Future
        self._mirror_sockets: set[web.WebSocketResponse] = set()  # passive mirror connections
        # Native WebSocket connections (Android foreground service, bypasses WebView)
        self._native_ws_by_client: dict[str, web.WebSocketResponse] = {}
        self._native_rpc_pending: dict[str, asyncio.Future] = {}  # id → Future for native responses
        self._native_subscriptions: dict[str, set[str]] = {}  # clientId → set of sessionIds
        self._native_subscribe_all: set[str] = set()  # clientIds subscribed to all sessions
        # Client metadata (persisted to ~/.vibr8/clients.json)
        self._client_metadata: dict[str, dict[str, Any]] = {}
        self._CLIENTS_PATH = Path.home() / ".vibr8" / "clients.json"
        self._load_client_metadata()

    # ── Native WebSocket (Android foreground service) ───────────────────

    def register_native_ws(self, client_id: str, ws: web.WebSocketResponse) -> None:
        self._native_ws_by_client[client_id] = ws
        logger.debug("[ws-bridge] Native WS registered for client %s", client_id[:8])

    def unregister_native_ws(self, client_id: str) -> None:
        self._native_ws_by_client.pop(client_id, None)
        self._native_subscriptions.pop(client_id, None)
        self._native_subscribe_all.discard(client_id)
        logger.debug("[ws-bridge] Native WS unregistered for client %s", client_id[:8])

    async def handle_native_message(self, client_id: str, data: dict) -> None:
        """Handle an incoming message from a native WebSocket.

        Supports three categories:
        - RPC responses (has ``id`` matching a pending request)
        - Subscriptions (``type: subscribe/unsubscribe``)
        - Permission responses (``type: permission_response``)
        """
        # RPC response
        msg_id = data.get("id")
        if msg_id and msg_id in self._native_rpc_pending:
            future = self._native_rpc_pending.pop(msg_id)
            if not future.done():
                future.set_result(data)
            return

        msg_type = data.get("type")

        if msg_type == "subscribe":
            session_ids = data.get("sessionIds")
            if data.get("all"):
                self._native_subscribe_all.add(client_id)
                self._native_subscriptions.pop(client_id, None)
            elif session_ids is not None:
                self._native_subscribe_all.discard(client_id)
                self._native_subscriptions[client_id] = set(session_ids)
            logger.info("[ws-bridge] Native subscribe: client=%s all=%s sessions=%s",
                        client_id[:8], client_id in self._native_subscribe_all,
                        len(self._native_subscriptions.get(client_id, set())))
            # Push initial state snapshot so the client doesn't start blind
            await self._send_initial_state(client_id)

        elif msg_type == "unsubscribe":
            self._native_subscribe_all.discard(client_id)
            self._native_subscriptions.pop(client_id, None)
            logger.info("[ws-bridge] Native unsubscribe: client=%s", client_id[:8])

        elif msg_type == "permission_response":
            session_id = data.get("sessionId", "")
            session = self._sessions.get(session_id) if session_id else None
            if session:
                await self._handle_permission_response(session, {
                    "request_id": data.get("request_id", ""),
                    "behavior": data.get("behavior", "deny"),
                    "message": data.get("message"),
                })
            else:
                logger.warning("[ws-bridge] Native permission_response for unknown session %s", session_id[:8] if session_id else "?")

    # ── Client metadata ─────────────────────────────────────────────────

    def _load_client_metadata(self) -> None:
        if self._CLIENTS_PATH.exists():
            try:
                self._client_metadata = json.loads(self._CLIENTS_PATH.read_text())
                logger.info("[ws-bridge] Loaded %d client metadata records", len(self._client_metadata))
            except Exception as e:
                logger.error("[ws-bridge] Failed to load client metadata: %s", e)
                self._client_metadata = {}
        else:
            self._client_metadata = {}

    def _save_client_metadata(self) -> None:
        try:
            self._CLIENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._CLIENTS_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._client_metadata, indent=2))
            tmp.rename(self._CLIENTS_PATH)
        except Exception as e:
            logger.error("[ws-bridge] Failed to save client metadata: %s", e)

    def get_client_metadata(self, client_id: str) -> dict[str, Any] | None:
        return self._client_metadata.get(client_id)

    def get_all_client_metadata(self) -> dict[str, dict[str, Any]]:
        return dict(self._client_metadata)

    def set_client_metadata(self, client_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        entry = self._client_metadata.get(client_id, {"createdAt": time.time()})
        for key in ("name", "description", "role", "deviceInfo", "fingerprint"):
            if key in updates:
                entry[key] = updates[key]
        entry["lastSeen"] = time.time()
        self._client_metadata[client_id] = entry
        self._save_client_metadata()
        return entry

    def register_device_info(self, client_id: str, device_info: dict[str, Any]) -> dict[str, Any]:
        """Register device info for a client, compute fingerprint, and do fingerprint matching."""
        fingerprint = self._compute_fingerprint(device_info)

        existing = self._client_metadata.get(client_id)
        if existing:
            existing["deviceInfo"] = device_info
            existing["fingerprint"] = fingerprint
            existing["lastSeen"] = time.time()
            self._client_metadata[client_id] = existing
            self._save_client_metadata()
            return existing

        # New client — check for fingerprint match to inherit name/description/role
        inherited: dict[str, Any] = {}
        for cid, meta in self._client_metadata.items():
            if cid != client_id and meta.get("fingerprint") == fingerprint:
                for key in ("name", "description", "role"):
                    if meta.get(key):
                        inherited[key] = meta[key]
                logger.info("[ws-bridge] Fingerprint match: %s inherits metadata from %s", client_id[:8], cid[:8])
                break

        entry: dict[str, Any] = {
            "deviceInfo": device_info,
            "fingerprint": fingerprint,
            "lastSeen": time.time(),
            "createdAt": time.time(),
            **inherited,
        }
        self._client_metadata[client_id] = entry
        self._save_client_metadata()
        return entry

    @staticmethod
    def _compute_fingerprint(device_info: dict[str, Any]) -> str:
        import hashlib
        parts = [
            device_info.get("userAgent", ""),
            str(device_info.get("screenWidth", "")),
            str(device_info.get("screenHeight", "")),
            str(device_info.get("devicePixelRatio", "")),
            device_info.get("platform", ""),
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

    def resolve_client(self, identifier: str) -> list[dict[str, Any]]:
        """Resolve a client by name, UUID, or prefix. Returns list of matches with full info."""
        all_clients = self._build_client_list()

        # Exact UUID match
        for c in all_clients:
            if c["clientId"] == identifier:
                return [c]

        # Exact name match (case-insensitive)
        by_name = [c for c in all_clients if c.get("name", "").lower() == identifier.lower()]
        if by_name:
            return by_name

        # UUID prefix match
        by_prefix = [c for c in all_clients if c["clientId"].startswith(identifier)]
        if by_prefix:
            return by_prefix

        return []

    def _build_client_list(self) -> list[dict[str, Any]]:
        """Build merged list of all known clients with online status."""
        result = []
        seen = set()

        # Online clients
        for cid, sid in self._client_sessions.items():
            entry = dict(self._client_metadata.get(cid, {}))
            entry["clientId"] = cid
            entry["online"] = True
            entry["sessionId"] = sid
            entry["wsRole"] = self._client_roles.get(cid, "primary")
            result.append(entry)
            seen.add(cid)

        # Offline clients from metadata
        for cid, meta in self._client_metadata.items():
            if cid not in seen:
                entry = dict(meta)
                entry["clientId"] = cid
                entry["online"] = False
                result.append(entry)

        return result

    # ── WebRTC manager ─────────────────────────────────────────────────

    def set_webrtc_manager(self, manager: WebRTCManager) -> None:
        self._webrtc_manager = manager

    def set_ring0_manager(self, manager: Any) -> None:
        self._ring0_manager = manager

    def set_ring0_event_router(self, router: Any) -> None:
        self._ring0_event_router = router

    def set_node_registry(self, registry: Any) -> None:
        self._node_registry = registry

    # ── Remote node session management ────────────────────────────────────
    # Node identity is embedded in the session ID: "{node_id}:{raw_id}" for remote.

    @staticmethod
    def qualify_session_id(node_id: str, raw_id: str) -> str:
        """Prefix a raw session ID with the node identity."""
        return f"{node_id}:{raw_id}"

    @staticmethod
    def parse_qualified_id(qid: str) -> tuple[str, str]:
        """(node_id, raw_id) — for local sessions, node_id is ''."""
        return tuple(qid.split(":", 1)) if ":" in qid else ("", qid)  # type: ignore[return-value]

    def _is_remote_session(self, session_id: str) -> bool:
        """Check if a session belongs to a remote node."""
        return ":" in session_id

    def get_session_node_id(self, session_id: str) -> str:
        """Return the node_id for a session ('local' for hub sessions)."""
        node_id, _ = self.parse_qualified_id(session_id)
        return node_id or "local"

    def _raw_session_id(self, qid: str) -> str:
        """Strip node prefix to get the raw ID the node uses internally."""
        return qid.split(":", 1)[-1]

    def update_remote_sessions(
        self, node_id: str, sessions: list[dict[str, Any]]
    ) -> None:
        """Clean up stale proxy sessions when a node reports its session list.
        Session IDs in the list are already qualified by the caller (main.py)."""
        prefix = f"{node_id}:"
        current_ids = {
            s.get("sessionId", s.get("id", ""))
            for s in sessions
        }
        # Remove proxy sessions that no longer exist on the node
        stale = [sid for sid in self._sessions if sid.startswith(prefix) and sid not in current_ids]
        for sid in stale:
            self._sessions.pop(sid, None)

    async def handle_remote_session_message(
        self, session_id: str, message: dict[str, Any]
    ) -> None:
        """Forward a message from a remote node's CLI to connected browsers.

        Also triggers TTS for assistant messages from remote Ring0 sessions,
        since audio is always processed centrally on the hub.
        """
        session = self._sessions.get(session_id)
        if not session:
            # Create a lightweight proxy session so messages are buffered
            session = self.get_or_create_session(session_id)

        # Buffer messages so reconnecting browsers get history
        msg_type = message.get("type", "")
        if msg_type in ("cli_connected", "cli_disconnected"):
            logger.info(f"[ws-bridge] Remote {msg_type} for session {session_id[:8]} → {len(session.browser_sockets)} browser(s)")
        if msg_type in ("assistant", "result", "user_message"):
            session.message_history.append(message)

        # Track permission requests so browsers connecting later get them
        if msg_type == "permission_request":
            req = message.get("request", {})
            req_id = req.get("request_id", "")
            if req_id:
                session.pending_permissions[req_id] = req
        elif msg_type == "permission_response":
            req_id = message.get("request_id", "")
            session.pending_permissions.pop(req_id, None)

        # Track session state updates from remote node
        if msg_type == "session_update":
            remote_state = message.get("session", {})
            if remote_state:
                session.state.update(remote_state)

        # TTS for remote Ring0 assistant responses
        if msg_type == "assistant" and self._webrtc_manager:
            text = message.get("message")
            if text:
                track = self._webrtc_manager.get_any_outgoing_track()
                if track:
                    audio_client_id, audio_track = track
                    if not self._webrtc_manager.is_tts_muted(audio_client_id):
                        asyncio.ensure_future(self._speak_text(session_id, text, audio_track))

        await self._broadcast_to_browsers(session, message)

    def remove_remote_node_sessions(self, node_id: str) -> None:
        """Clean up all proxy sessions when a node goes offline."""
        prefix = f"{node_id}:"
        to_remove = [sid for sid in self._sessions if sid.startswith(prefix)]
        for sid in to_remove:
            self._sessions.pop(sid, None)

    def cancel_tts(self, session_id: str) -> None:
        """Cancel any in-progress TTS for *session_id* (called on barge-in)."""
        tts = self._active_tts.pop(session_id, None)
        if tts:
            tts.cancel()
            logger.info("[ws-bridge] TTS cancelled for session %s (barge-in)", session_id)

    def _start_thinking_delayed(self, session_id: str, delay: float = 1.5) -> None:
        """Schedule the thinking tone after *delay* seconds.

        Cancelled automatically if a response arrives before the timer fires.
        """
        self._cancel_thinking_timer(session_id)
        loop = asyncio.get_event_loop()
        handle = loop.call_later(delay, self._activate_thinking, session_id)
        self._thinking_timers[session_id] = handle

    def _start_thinking_now(self, session_id: str) -> None:
        """Start the thinking tone immediately (e.g. tool use started)."""
        self._cancel_thinking_timer(session_id)
        self._activate_thinking(session_id)

    def _activate_thinking(self, session_id: str) -> None:
        """Actually turn on the thinking tone."""
        self._thinking_timers.pop(session_id, None)
        if self._webrtc_manager:
            self._webrtc_manager.set_thinking_any(True)

    def _stop_thinking(self, session_id: str) -> None:
        """Stop the thinking tone and cancel any pending timer."""
        self._cancel_thinking_timer(session_id)
        if self._webrtc_manager:
            self._webrtc_manager.set_thinking_any(False)

    def _cancel_thinking_timer(self, session_id: str) -> None:
        handle = self._thinking_timers.pop(session_id, None)
        if handle:
            handle.cancel()

    # ── Callback registration ────────────────────────────────────────────

    def on_cli_session_id_received(self, cb: Callable[[str, str], None]) -> None:
        self._on_cli_session_id = cb

    def on_cli_relaunch_needed_callback(self, cb: Callable[[str], None]) -> None:
        self._on_cli_relaunch_needed = cb

    def on_first_turn_completed_callback(self, cb: Callable[[str, str], None]) -> None:
        self._on_first_turn_completed = cb

    # ── Store ────────────────────────────────────────────────────────────

    def set_store(self, store: SessionStore) -> None:
        self._store = store

    def restore_from_disk(self) -> int:
        if not self._store:
            return 0
        from server import session_names
        persisted = self._store.load_all()
        count = 0
        for p in persisted:
            sid = p.id
            if sid in self._sessions:
                continue
            state = p.state if p.state else _make_default_state(sid)
            session = Session(
                id=sid,
                backend_type=state.get("backend_type", "claude"),
                state=state,
                pending_permissions=dict(p.pendingPermissions) if p.pendingPermissions else {},
                message_history=list(p.messageHistory) if p.messageHistory else [],
                pending_messages=list(p.pendingMessages) if p.pendingMessages else [],
            )
            session.state["backend_type"] = session.backend_type
            # Restore MRU timestamp
            if p.lastPromptedAt:
                session.last_prompted_at = p.lastPromptedAt
            # Restore pen state from persisted session
            if state.get("controlledBy") == "user":
                session.controlled_by = "user"
            # Initialize dedup sets from active history
            session._dedup_msg_ids = {
                e.get("msg_id") for e in session.message_history if e.get("msg_id")
            }
            session._dedup_result_keys = {
                e.get("result_dedup_key") for e in session.message_history if e.get("result_dedup_key")
            }
            self._sessions[sid] = session
            if p.name:
                session_names.set_name(sid, p.name, unique=False)
            if session.state.get("num_turns", 0) > 0:
                self._auto_naming_attempted.add(sid)
            # Trim bloated sessions restored from disk
            if len(session.message_history) > 500:
                self._archive_and_trim(session, keep_count=500)
            count += 1
        if count > 0:
            logger.info(f"[ws-bridge] Restored {count} session(s) from disk")
        return count

    def get_last_prompted_at(self, session_id: str) -> float:
        """Return last_prompted_at (ms) for a session, or 0 if unknown."""
        session = self._sessions.get(session_id)
        return session.last_prompted_at if session else 0

    def _persist_session(self, session: Session) -> None:
        if not self._store:
            return
        from server.session_store import PersistedSession
        from server import session_names
        self._store.save(PersistedSession(
            id=session.id,
            state=session.state,
            messageHistory=session.message_history,
            pendingMessages=session.pending_messages,
            pendingPermissions=list(session.pending_permissions.items()),
            name=session_names.get_name(session.id),
            lastPromptedAt=session.last_prompted_at or None,
        ))

    def _archive_and_trim_before(self, session: Session, index: int) -> None:
        """Archive messages before *index* and trim the active history."""
        if index <= 0 or not self._store:
            return
        to_archive = session.message_history[:index]
        session.message_history = session.message_history[index:]
        self._store.archive_messages(session.id, to_archive)
        self._persist_session(session)
        logger.info("[ws-bridge] Archived %d messages (replay boundary) for session %s, keeping %d",
                    len(to_archive), session.id[:8], len(session.message_history))

    def _archive_and_trim(self, session: Session, keep_count: int) -> None:
        """Archive oldest messages, keeping the last *keep_count*."""
        if len(session.message_history) <= keep_count or not self._store:
            return
        to_archive = session.message_history[:-keep_count]
        session.message_history = session.message_history[-keep_count:]
        self._store.archive_messages(session.id, to_archive)
        self._persist_session(session)
        logger.info("[ws-bridge] Archived %d messages (count cap) for session %s, keeping %d",
                    len(to_archive), session.id[:8], len(session.message_history))

    def flush_to_disk(self) -> None:
        """Immediately persist all sessions that have pending debounced saves."""
        if not self._store:
            return
        pending = self._store.has_pending_saves()
        if not pending:
            return
        from server.session_store import PersistedSession
        from server import session_names
        for session_id in pending:
            session = self._sessions.get(session_id)
            if session:
                self._store.save_sync(PersistedSession(
                    id=session.id,
                    state=session.state,
                    messageHistory=session.message_history,
                    pendingMessages=session.pending_messages,
                    pendingPermissions=list(session.pending_permissions.items()),
                    name=session_names.get_name(session.id),
                ))
        self._store.cancel_pending()
        logger.info("[ws-bridge] Flushed %d session(s) to disk", len(pending))

    # ── Session management ───────────────────────────────────────────────

    def get_or_create_session(self, session_id: str, backend_type: str = "claude") -> Session:
        session = self._sessions.get(session_id)
        if not session:
            session = Session(
                id=session_id,
                backend_type=backend_type,
                state=_make_default_state(session_id, backend_type),
            )
            self._sessions[session_id] = session
        session.backend_type = backend_type
        session.state["backend_type"] = backend_type
        return session

    def get_session(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    async def broadcast_guard_state(self, session_id: str, enabled: bool, *, client_id: str | None = None) -> None:
        """Broadcast guard mode state change to browser and native clients."""
        msg = {"type": "guard_state", "enabled": enabled}
        if client_id:
            await self._send_to_client(client_id, msg)
            await self._push_to_all_native_clients("guard_state", {"enabled": enabled})
        else:
            session = self._sessions.get(session_id)
            if session:
                await self._broadcast_to_browsers(session, msg)
                await self._push_to_native_clients(session_id, "guard_state", {"enabled": enabled})

    async def broadcast_audio_off(self, session_id: str) -> None:
        """Tell browser to disconnect WebRTC audio."""
        session = self._sessions.get(session_id)
        if session:
            await self._broadcast_to_browsers(
                session, {"type": "audio_off"}
            )

    async def broadcast_tts_muted(self, session_id: str, muted: bool, *, client_id: str | None = None) -> None:
        """Broadcast TTS mute state change to browser and native clients."""
        msg = {"type": "tts_muted", "muted": muted}
        if client_id:
            await self._send_to_client(client_id, msg)
            await self._push_to_all_native_clients("tts_muted", {"muted": muted})
        else:
            session = self._sessions.get(session_id)
            if session:
                await self._broadcast_to_browsers(session, msg)
                await self._push_to_native_clients(session_id, "tts_muted", {"muted": muted})

    async def broadcast_voice_mode(self, session_id: str, mode: str | None, *, client_id: str | None = None) -> None:
        """Broadcast voice mode change to browser and native clients."""
        msg = {"type": "voice_mode", "mode": mode}
        if client_id:
            await self._send_to_client(client_id, msg)
            await self._push_to_all_native_clients("voice_mode", {"mode": mode})
        else:
            session = self._sessions.get(session_id)
            if session:
                await self._broadcast_to_browsers(session, msg)
                await self._push_to_native_clients(session_id, "voice_mode", {"mode": mode})

    async def broadcast_node_switch(self, node_id: str, node_name: str | None = None) -> None:
        """Broadcast node_switch to all connected browsers (e.g. from voice command)."""
        msg: dict[str, Any] = {"type": "node_switch", "nodeId": node_id}
        if node_name:
            msg["nodeName"] = node_name
        data = json.dumps(msg)
        for session in self._sessions.values():
            for ws in list(session.browser_sockets):
                try:
                    await ws.send_str(data)
                except Exception:
                    pass

    def get_all_sessions(self) -> list[dict[str, Any]]:
        return [s.state for s in self._sessions.values()]

    def is_cli_connected(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if not session:
            return False
        if session.backend_type == "codex":
            return session.codex_adapter is not None and session.codex_adapter.is_connected()
        if session.backend_type == "computer-use":
            return session_id in self._computer_use_agents
        return session.cli_socket is not None

    def remove_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._auto_naming_attempted.discard(session_id)
        if self._store:
            self._store.remove(session_id)

    async def close_session(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if not session:
            return
        if session.cli_socket:
            await session.cli_socket.close()
            session.cli_socket = None
        if session.codex_adapter:
            await session.codex_adapter.disconnect()
            session.codex_adapter = None
        agent = self._computer_use_agents.pop(session_id, None)
        if agent:
            await agent.stop()
        for ws, client_id in list(session.browser_sockets.items()):
            await ws.close()
            if client_id:
                self._client_sessions.pop(client_id, None)
                self._ws_by_client.pop(client_id, None)
        session.browser_sockets.clear()
        self._sessions.pop(session_id, None)
        self._auto_naming_attempted.discard(session_id)
        if self._store:
            self._store.remove(session_id)

    # ── Codex adapter attachment ─────────────────────────────────────────

    def attach_codex_adapter(self, session_id: str, adapter: Any) -> None:
        session = self.get_or_create_session(session_id, "codex")
        session.backend_type = "codex"
        session.state["backend_type"] = "codex"
        session.codex_adapter = adapter

        async def on_browser_msg(msg: dict[str, Any]) -> None:
            if msg.get("type") == "session_init":
                session.state = {**session.state, **msg.get("session", {}), "backend_type": "codex"}
                self._persist_session(session)
            elif msg.get("type") == "session_update":
                session.state = {**session.state, **msg.get("session", {}), "backend_type": "codex"}
                self._persist_session(session)
            elif msg.get("type") == "status_change":
                session.state["is_compacting"] = msg.get("status") == "compacting"
                self._persist_session(session)

            if msg.get("type") in ("assistant", "result"):
                session.message_history.append(msg)
                # If CLI continues with new messages while permissions are pending,
                # those permissions were auto-approved — clear them to avoid stale state.
                if session.pending_permissions:
                    for req_id in list(session.pending_permissions):
                        await self._broadcast_to_browsers(session, {"type": "permission_cancelled", "request_id": req_id})
                        await self._push_to_native_clients(session.id, "permission_cancelled", {"request_id": req_id})
                    session.pending_permissions.clear()
                self._persist_session(session)

            if msg.get("type") == "permission_request":
                req = msg.get("request", {})
                session.pending_permissions[req.get("request_id", "")] = req
                self._persist_session(session)

            await self._broadcast_to_browsers(session, msg)

            # Auto-naming
            if (
                msg.get("type") == "result"
                and not msg.get("data", {}).get("is_error")
                and self._on_first_turn_completed
                and session.id not in self._auto_naming_attempted
            ):
                self._auto_naming_attempted.add(session.id)
                first = next((m for m in session.message_history if m.get("type") == "user_message"), None)
                if first:
                    self._on_first_turn_completed(session.id, first.get("content", ""))

        adapter.on_browser_message(on_browser_msg)

        def on_meta(meta: dict[str, Any]) -> None:
            if meta.get("cliSessionId") and self._on_cli_session_id:
                self._on_cli_session_id(session.id, meta["cliSessionId"])
            if meta.get("model"):
                session.state["model"] = meta["model"]
            if meta.get("cwd"):
                session.state["cwd"] = meta["cwd"]
            session.state["backend_type"] = "codex"
            self._persist_session(session)

        adapter.on_session_meta(on_meta)

        async def on_disconnect() -> None:
            for req_id in list(session.pending_permissions):
                await self._broadcast_to_browsers(session, {"type": "permission_cancelled", "request_id": req_id})
                await self._push_to_native_clients(session.id, "permission_cancelled", {"request_id": req_id})
            session.pending_permissions.clear()
            # Clear running/permission state and notify Ring0
            was_waiting = session.state.get("is_waiting_for_permission")
            was_running = session.state.get("is_running")
            session.state["is_running"] = False
            session.state["is_waiting_for_permission"] = False
            if was_waiting:
                asyncio.ensure_future(self._notify_ring0_state_change(session, "waiting_for_permission→idle"))
            elif was_running:
                asyncio.ensure_future(self._notify_ring0_state_change(session, "running→idle"))
            session.codex_adapter = None
            self._persist_session(session)
            logger.info(f"[ws-bridge] Codex adapter disconnected for session {session_id}")
            await self._broadcast_to_browsers(session, {"type": "cli_disconnected"})
            await self._push_to_native_clients(session.id, "cli_disconnected")

        adapter.on_disconnect(on_disconnect)

        # Flush queued messages
        if session.pending_messages:
            logger.info(f"[ws-bridge] Flushing {len(session.pending_messages)} queued message(s) to Codex adapter for session {session_id}")
            queued = session.pending_messages[:]
            session.pending_messages.clear()
            for raw in queued:
                try:
                    msg = json.loads(raw)
                    adapter.send_browser_message(msg)
                except Exception:
                    logger.warning(f"[ws-bridge] Failed to parse queued message for Codex")

        # Notify browsers and native clients
        import asyncio
        asyncio.ensure_future(self._broadcast_to_browsers(session, {"type": "cli_connected"}))
        asyncio.ensure_future(self._push_to_native_clients(session.id, "cli_connected"))
        logger.info(f"[ws-bridge] Codex adapter attached for session {session_id}")

    # ── Computer-use agent attachment ──────────────────────────────────────────

    def register_computer_use_agent(self, session_id: str, agent: Any) -> None:
        """Register a ComputerUseAgent for a session."""
        from server.ui_tars_agent import UITarsAgent
        assert isinstance(agent, UITarsAgent)

        session = self.get_or_create_session(session_id, "computer-use")
        session.backend_type = "computer-use"
        session.state["backend_type"] = "computer-use"
        self._computer_use_agents[session_id] = agent

        async def on_agent_message(msg: dict[str, Any]) -> None:
            msg_type = msg.get("type")
            if msg_type in ("assistant", "result", "observation"):
                session.message_history.append(msg)
                self._persist_session(session)
            elif msg_type == "status_change":
                status = msg.get("status", "idle")
                session.state["is_running"] = status in ("running", "watching", "confirming")
                self._persist_session(session)
            # confirm, observation, etc. — all broadcast to browsers
            await self._broadcast_to_browsers(session, msg)

        agent.on_message(on_agent_message)

        # Emit session_init so browsers get the right backend type
        init_msg: dict[str, Any] = {
            "type": "session_init",
            "session": {
                "session_id": session_id,
                "backend_type": "computer-use",
                "model": agent._model,
                "cwd": "",
                "tools": [],
                "permissionMode": "bypassPermissions",
            },
        }
        session.state.update(init_msg["session"])
        self._persist_session(session)
        asyncio.ensure_future(self._broadcast_to_browsers(session, init_msg))
        asyncio.ensure_future(self._broadcast_to_browsers(session, {"type": "cli_connected"}))
        asyncio.ensure_future(self._push_to_native_clients(session.id, "cli_connected"))
        logger.info("[ws-bridge] Computer-use agent registered for session %s", session_id)

    def unregister_computer_use_agent(self, session_id: str) -> None:
        """Remove a ComputerUseAgent."""
        self._computer_use_agents.pop(session_id, None)

    # ── CLI WebSocket handlers ───────────────────────────────────────────

    def handle_cli_open(self, ws: web.WebSocketResponse, session_id: str) -> None:
        session = self.get_or_create_session(session_id)
        session.cli_socket = ws
        session._awaiting_replay = True
        session._replay_archived = False
        logger.info(f"[ws-bridge] CLI connected for session {session_id} (awaiting replay)")
        import asyncio
        asyncio.ensure_future(self._broadcast_to_browsers(session, {"type": "cli_connected"}))
        asyncio.ensure_future(self._push_to_native_clients(session.id, "cli_connected"))
        asyncio.ensure_future(self._push_to_all_native_clients("sessions_changed"))

        # Flush queued messages
        if session.pending_messages:
            logger.info(f"[ws-bridge] Flushing {len(session.pending_messages)} queued message(s) for session {session_id}")
            for ndjson in session.pending_messages:
                self._send_to_cli(session, ndjson)
            session.pending_messages.clear()

    async def handle_cli_message(self, ws: web.WebSocketResponse, raw: str) -> None:
        session_id = None
        for sid, s in self._sessions.items():
            if s.cli_socket is ws:
                session_id = sid
                break
        if not session_id:
            return
        session = self._sessions.get(session_id)
        if not session:
            return

        # NDJSON: split on newlines, parse each line
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(f"[ws-bridge] Failed to parse CLI message: {line[:200]}")
                continue
            await self._route_cli_message(session, msg)

    async def handle_cli_close(self, ws: web.WebSocketResponse) -> None:
        session_id = None
        for sid, s in self._sessions.items():
            if s.cli_socket is ws:
                session_id = sid
                break
        if not session_id:
            return
        session = self._sessions.get(session_id)
        if not session:
            return

        session.cli_socket = None
        pending_perms = list(session.pending_permissions.keys())
        is_running = session.state.get("is_running")
        is_waiting = session.state.get("is_waiting_for_permission")
        logger.info(f"[ws-bridge] CLI disconnected for session {session_id} is_running={is_running} is_waiting={is_waiting} pending_perms={len(pending_perms)}")
        await self._broadcast_to_browsers(session, {"type": "cli_disconnected"})
        await self._push_to_native_clients(session.id, "cli_disconnected")

        for req_id in list(session.pending_permissions):
            await self._broadcast_to_browsers(session, {"type": "permission_cancelled", "request_id": req_id})
            await self._push_to_native_clients(session.id, "permission_cancelled", {"request_id": req_id})
        session.pending_permissions.clear()
        # Clear running/permission state and notify Ring0
        was_waiting = session.state.get("is_waiting_for_permission")
        was_running = session.state.get("is_running")
        session.state["is_running"] = False
        session.state["is_waiting_for_permission"] = False
        if was_waiting:
            asyncio.ensure_future(self._notify_ring0_state_change(session, "waiting_for_permission→idle"))
        elif was_running:
            asyncio.ensure_future(self._notify_ring0_state_change(session, "running→idle"))

        asyncio.ensure_future(self._push_to_all_native_clients("sessions_changed"))

    # ── Browser WebSocket handlers ───────────────────────────────────────

    async def handle_browser_open(self, ws: web.WebSocketResponse, session_id: str, client_id: str = "", role: str = "primary", mirror: bool = False) -> None:
        session = self.get_or_create_session(session_id)
        session.browser_sockets[ws] = client_id

        if mirror:
            # Mirror connections are passive — they receive broadcasts but don't
            # register as the canonical WS for this client (control channel does that).
            self._mirror_sockets.add(ws)
            logger.info(f"[ws-bridge] Mirror WS connected for session {session_id} client={client_id or '(none)'} ({len(session.browser_sockets)} browsers)")
        else:
            was_already_connected = client_id and self._client_roles.get(client_id) == "secondscreen"
            if client_id:
                self._client_sessions[client_id] = session_id
                self._ws_by_client[client_id] = ws
                self._client_roles[client_id] = role
                # Update lastSeen in metadata
                if client_id in self._client_metadata:
                    self._client_metadata[client_id]["lastSeen"] = time.time()
                    self._save_client_metadata()
            logger.info(f"[ws-bridge] Browser connected for session {session_id} client={client_id or '(none)'} role={role} ({len(session.browser_sockets)} browsers)")

            # Notify Ring0 when a paired second screen connects for the first time (not reconnects)
            if client_id and role == "secondscreen" and not was_already_connected:
                from server.ring0_events import Ring0Event
                await self.emit_ring0_event(Ring0Event(
                    fields={"type": "second_screen_connected", "clientId": client_id[:8]},
                ))

        # Send current session state
        await self._send_to_browser(ws, {"type": "session_init", "session": session.state})

        # For remote sessions with no buffered history, fetch from the node
        if not session.message_history and self._is_remote_session(session_id):
            node_id, raw_id = self.parse_qualified_id(session_id)
            node = self._node_registry.get_node(node_id) if self._node_registry and node_id else None
            if node and node.tunnel and node.tunnel.connected:
                try:
                    resp = await node.tunnel.send_command({
                        "type": "get_session_output",
                        "sessionId": raw_id,
                    })
                    remote_messages = resp.get("messages", [])
                    if remote_messages:
                        session.message_history = remote_messages
                        logger.info("[ws-bridge] Fetched %d messages from remote node for session %s", len(remote_messages), session_id[:8])
                except Exception:
                    logger.warning("[ws-bridge] Failed to fetch remote history for session %s", session_id[:8])

        if session.message_history:
            history = session.message_history
            msg: dict[str, Any] = {"type": "message_history", "messages": history}
            if self._store and self._store.has_archive(session.id):
                meta = self._store.get_archive_meta(session.id)
                msg["archivedMessageCount"] = meta.get("totalArchivedMessages", 0)
            await self._send_to_browser(ws, msg)

        # Send pending permissions
        for perm in session.pending_permissions.values():
            await self._send_to_browser(ws, {"type": "permission_request", "request": perm})

        # Check backend connectivity
        if self._is_remote_session(session_id):
            # Remote sessions: check if the node tunnel is connected
            node_id, _ = self.parse_qualified_id(session_id)
            node = self._node_registry.get_node(node_id) if self._node_registry and node_id else None
            backend_connected = bool(node and node.tunnel and node.tunnel.connected)
            logger.info(f"[ws-bridge] Backend check for remote session {session_id[:8]}: node={node_id[:8] if node_id else '?'} "
                        f"node_found={node is not None} tunnel={node.tunnel is not None if node else False} "
                        f"tunnel_connected={node.tunnel.connected if node and node.tunnel else False} → {backend_connected}")
        else:
            if session.backend_type == "codex":
                backend_connected = session.codex_adapter is not None and session.codex_adapter.is_connected()
            elif session.backend_type == "computer-use":
                backend_connected = session_id in self._computer_use_agents
            else:
                backend_connected = session.cli_socket is not None
            logger.info(f"[ws-bridge] Backend check for local session {session_id[:8]}: "
                        f"backend_type={session.backend_type} → {backend_connected}")
        if not backend_connected:
            logger.warning(f"[ws-bridge] Sending cli_disconnected to browser for session {session_id[:8]} (backend not connected)")
            await self._send_to_browser(ws, {"type": "cli_disconnected"})
            if not self._is_remote_session(session_id) and self._on_cli_relaunch_needed:
                logger.info(f"[ws-bridge] Browser connected but backend is dead for session {session_id}, requesting relaunch")
                self._on_cli_relaunch_needed(session_id)

    async def handle_browser_message(self, ws: web.WebSocketResponse, raw: str) -> None:
        session_id = None
        for sid, s in self._sessions.items():
            if ws in s.browser_sockets:
                session_id = sid
                break
        if not session_id:
            return
        session = self._sessions.get(session_id)
        if not session:
            return

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"[ws-bridge] Failed to parse browser message: {raw[:200]}")
            return

        # Handle RPC responses from clients
        if msg.get("type") == "rpc_response":
            rpc_id = msg.get("id", "")
            logger.info("[ws-bridge] RPC response received: rpc_id=%s pending=%s keys=%s", rpc_id[:8] if rpc_id else "?", rpc_id in self._rpc_pending, list(msg.keys()))
            future = self._rpc_pending.pop(rpc_id, None)
            if future and not future.done():
                # Always resolve (never reject) so callers get error details
                # like errorCode instead of a generic RuntimeError.
                result: dict = {}
                if "error" in msg:
                    result["error"] = msg["error"]
                    if "errorCode" in msg:
                        result["errorCode"] = msg["errorCode"]
                if "result" in msg:
                    result.update(msg["result"])
                future.set_result(result)
            return

        await self._route_browser_message(session, msg, ws)

    async def handle_browser_close(self, ws: web.WebSocketResponse) -> None:
        is_mirror = ws in self._mirror_sockets
        if is_mirror:
            self._mirror_sockets.discard(ws)

        for sid, s in self._sessions.items():
            if ws in s.browser_sockets:
                client_id = s.browser_sockets.pop(ws, "")

                if is_mirror:
                    # Mirror connections don't own the client identity — just log and exit
                    logger.info(f"[ws-bridge] Mirror WS disconnected for session {sid} client={client_id or '(none)'} ({len(s.browser_sockets)} browsers)")
                else:
                    was_second_screen = False
                    if client_id:
                        was_second_screen = self._client_roles.get(client_id) == "secondscreen"
                        self._client_sessions.pop(client_id, None)
                        self._ws_by_client.pop(client_id, None)
                        self._client_roles.pop(client_id, None)
                    logger.info(f"[ws-bridge] Browser disconnected for session {sid} client={client_id or '(none)'} ({len(s.browser_sockets)} browsers)")

                    # Notify Ring0 when a paired second screen disconnects
                    if was_second_screen and client_id:
                        from server.ring0_events import Ring0Event
                        await self.emit_ring0_event(Ring0Event(
                            fields={"type": "second_screen_disconnected", "clientId": client_id[:8]},
                        ))
                break

    # ── CLI message routing ──────────────────────────────────────────────

    async def _route_cli_message(self, session: Session, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type")
        if msg_type not in ("stream_event", "keep_alive"):
            extra = ""
            if msg_type == "control_request":
                req = msg.get("request", {})
                extra = f" subtype={req.get('subtype')} tool={req.get('tool_name')} req_id={msg.get('request_id', '')[:8]}"
            elif msg_type == "rate_limit_event":
                extra = f" data={json.dumps(msg)[:200]}"
            logger.info("[ws-bridge] CLI msg type=%s session=%s%s", msg_type, session.id[:8], extra)
        if msg_type == "system":
            await self._handle_system_message(session, msg)
        elif msg_type == "assistant":
            await self._handle_assistant_message(session, msg)
        elif msg_type == "result":
            await self._handle_result_message(session, msg)
        elif msg_type == "stream_event":
            await self._handle_stream_event(session, msg)
        elif msg_type == "control_request":
            await self._handle_control_request(session, msg)
        elif msg_type == "tool_progress":
            await self._handle_tool_progress(session, msg)
        elif msg_type == "tool_use_summary":
            await self._handle_tool_use_summary(session, msg)
        elif msg_type == "auth_status":
            await self._handle_auth_status(session, msg)
        elif msg_type == "keep_alive":
            pass  # silently consume

    async def _handle_system_message(self, session: Session, msg: dict[str, Any]) -> None:
        subtype = msg.get("subtype")
        if subtype == "init":
            session.state["is_running"] = False
            session.state["is_waiting_for_permission"] = False

            if msg.get("session_id") and self._on_cli_session_id:
                self._on_cli_session_id(session.id, msg["session_id"])

            session.state["model"] = msg.get("model", "")
            session.state["cwd"] = msg.get("cwd", "")
            session.state["tools"] = msg.get("tools", [])
            session.state["permissionMode"] = msg.get("permissionMode", "default")
            session.state["claude_code_version"] = msg.get("claude_code_version", "")
            session.state["mcp_servers"] = msg.get("mcp_servers", [])
            session.state["agents"] = msg.get("agents", [])
            session.state["slash_commands"] = msg.get("slash_commands", [])
            session.state["skills"] = msg.get("skills", [])

            # Resolve git info
            cwd = session.state.get("cwd")
            if cwd:
                try:
                    session.state["git_branch"] = subprocess.run(
                        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                        cwd=cwd, capture_output=True, text=True, timeout=3,
                    ).stdout.strip()

                    try:
                        git_dir = subprocess.run(
                            ["git", "rev-parse", "--git-dir"],
                            cwd=cwd, capture_output=True, text=True, timeout=3,
                        ).stdout.strip()
                        session.state["is_worktree"] = "/worktrees/" in git_dir
                    except Exception:
                        pass

                    try:
                        session.state["repo_root"] = subprocess.run(
                            ["git", "rev-parse", "--show-toplevel"],
                            cwd=cwd, capture_output=True, text=True, timeout=3,
                        ).stdout.strip()
                    except Exception:
                        pass

                    try:
                        counts = subprocess.run(
                            ["git", "rev-list", "--left-right", "--count", "@{upstream}...HEAD"],
                            cwd=cwd, capture_output=True, text=True, timeout=3,
                        ).stdout.strip()
                        parts = counts.split()
                        if len(parts) == 2:
                            session.state["git_behind"] = int(parts[0])
                            session.state["git_ahead"] = int(parts[1])
                    except Exception:
                        session.state["git_ahead"] = 0
                        session.state["git_behind"] = 0
                except Exception:
                    pass

            await self._broadcast_to_browsers(session, {"type": "session_init", "session": session.state})
            self._persist_session(session)

        elif subtype == "status":
            session.state["is_compacting"] = msg.get("status") == "compacting"
            if msg.get("permissionMode"):
                session.state["permissionMode"] = msg["permissionMode"]
            await self._broadcast_to_browsers(session, {
                "type": "status_change",
                "status": msg.get("status"),
            })

    async def _handle_assistant_message(self, session: Session, msg: dict[str, Any]) -> None:
        # Dedup by Anthropic message ID — skip replayed messages we already have
        msg_content = msg.get("message")
        msg_id = msg_content.get("id") if isinstance(msg_content, dict) else None
        is_update = False
        if msg_id:
            if msg_id in session._dedup_msg_ids:
                session._last_assistant_msg_id = msg_id  # Track for result dedup even on skip
                if session._awaiting_replay:
                    # Still replaying — archive on first hit, drop all replayed messages.
                    # _awaiting_replay stays True until a NEW msg_id arrives.
                    if not session._replay_archived:
                        session._replay_archived = True
                        idx = next((i for i, e in enumerate(session.message_history)
                                    if e.get("msg_id") == msg_id), None)
                        if idx is not None and idx > 0:
                            self._archive_and_trim_before(session, idx)
                    return
                # Not replay — this is a streaming update (same msg_id with
                # new content blocks, e.g. thinking → text).  Let it through
                # so the browser gets the updated content.
                is_update = True
            else:
                session._dedup_msg_ids.add(msg_id)
            session._last_assistant_msg_id = msg_id

        # If we get a NEW message while awaiting replay, no replay happened (fresh session)
        if session._awaiting_replay:
            session._awaiting_replay = False

        # Detect idle → running transition
        if not session.state.get("is_running"):
            session.state["is_running"] = True
            import asyncio
            asyncio.ensure_future(self._notify_ring0_state_change(session, "idle→running"))
            asyncio.ensure_future(self._push_to_native_clients(session.id, "status_change", {
                "agentStatus": "running",
                "isRunning": True, "isWaitingForPermission": False,
            }))

        text = msg.get("message")

        # TTS: speak assistant response if audio is active and TTS not muted.
        # When Ring0 is enabled, only Ring0's responses trigger TTS.
        if text and self._webrtc_manager:
            ring0 = self._ring0_manager
            is_ring0_session = ring0 and ring0.is_enabled and session.id == ring0.session_id
            tts_allowed = not ring0 or not ring0.is_enabled or is_ring0_session
            # Look up outgoing track — audio is keyed by client_id, not session.
            audio_client_id = ""
            track = None
            pair = self._webrtc_manager.get_any_outgoing_track()
            if pair:
                audio_client_id, track = pair
            tts_muted = self._webrtc_manager.is_tts_muted(audio_client_id) if audio_client_id else False
            text_preview = repr(text)[:200] if not isinstance(text, str) else f"{len(text)} chars"
            logger.info(
                "[ws-bridge] TTS check: session=%s, audio_client=%s, text_type=%s, preview=%s, track=%s, tts_muted=%s, tts_allowed=%s",
                session.id, audio_client_id, type(text).__name__, text_preview, track is not None, tts_muted, tts_allowed,
            )
            if track and not tts_muted and tts_allowed:
                import asyncio
                asyncio.ensure_future(self._speak_text(session.id, text, track))

        browser_msg: dict[str, Any] = {
            "type": "assistant",
            "message": text,
            "parent_tool_use_id": msg.get("parent_tool_use_id"),
            "timestamp": int(time.time() * 1000),
        }
        if msg_id:
            browser_msg["msg_id"] = msg_id
        # Log tool uses in assistant messages for debugging
        if isinstance(text, dict):
            content_blocks = text.get("content", [])
            if isinstance(content_blocks, list):
                tool_uses = [b.get("name") for b in content_blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
                if tool_uses:
                    logger.info("[ws-bridge] Assistant has tool_use blocks: %s session=%s", tool_uses, session.id[:8])
        if is_update and msg_id:
            # Replace existing history entry with updated content
            for i in range(len(session.message_history) - 1, -1, -1):
                if session.message_history[i].get("msg_id") == msg_id:
                    session.message_history[i] = browser_msg
                    break
            else:
                session.message_history.append(browser_msg)
        else:
            session.message_history.append(browser_msg)
        logger.info("[ws-bridge] Broadcasting assistant to %d browsers for session %s", len(session.browser_sockets), session.id[:8])
        await self._broadcast_to_browsers(session, browser_msg)
        self._persist_session(session)

    async def _speak_text(self, session_id: str, text, track) -> None:
        """Synthesize *text* via OpenAI TTS and push Opus frames to *track*."""
        try:
            # message can be a string, a list of content blocks, or a dict with content
            if isinstance(text, dict):
                # e.g. {"role": "assistant", "content": "..."} or {"content": [...]}
                content = text.get("content", "")
                if isinstance(content, list):
                    text = " ".join(
                        block.get("text", "") for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    )
                elif isinstance(content, str):
                    text = content
                else:
                    text = ""
            elif isinstance(text, list):
                text = " ".join(
                    block.get("text", "") for block in text
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            if not isinstance(text, str) or not text.strip():
                logger.info("[ws-bridge] TTS skipped: empty or non-string text for session %s (type=%s)", session_id, type(text).__name__)
                return

            # Strip markdown formatting for cleaner TTS output.
            import re
            text = re.sub(r'\*{1,3}', '', text)           # bold/italic: ** * ***
            text = re.sub(r'#{1,6}\s*', '', text)         # headings: ##
            text = re.sub(r'`{1,3}[^`]*`{1,3}', '', text) # inline/block code
            text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)  # links: [text](url) → text
            text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)  # list markers
            text = text.strip()
            if not text:
                logger.info("[ws-bridge] TTS skipped: empty after markdown strip for session %s", session_id)
                return

            # Pronunciation fixes: replace brand names with phonetic equivalents.
            text = re.sub(r'(?i)\bvibr8\b', 'vibrate', text)

            frame_count = 0
            def on_frame(frame):
                nonlocal frame_count
                frame_count += 1
                track.push_opus_frame(frame)

            # Stop thinking tone — TTS is taking over.
            self._cancel_thinking_timer(session_id)
            if self._webrtc_manager:
                self._webrtc_manager.set_thinking_any(False)
            logger.info("[ws-bridge] TTS starting for session %s: %d chars", session_id, len(text))
            from server.tts import TTS_OpenAI
            tts = TTS_OpenAI(opus_frame_handler=on_frame)
            # Cancel any prior TTS still running for this session.
            prev = self._active_tts.pop(session_id, None)
            if prev:
                prev.cancel()
            self._active_tts[session_id] = tts
            try:
                await tts.say(text)
            finally:
                if self._active_tts.get(session_id) is tts:
                    self._active_tts.pop(session_id, None)
            logger.info("[ws-bridge] TTS done for session %s: %d opus frames pushed", session_id, frame_count)
        except Exception:
            logger.exception("[ws-bridge] TTS failed for session %s", session_id)
        finally:
            self._stop_thinking(session_id)

    async def _handle_result_message(self, session: Session, msg: dict[str, Any]) -> None:
        # Dedup by preceding assistant's msg_id — each result follows a specific assistant turn
        result_dedup_key = session._last_assistant_msg_id
        if result_dedup_key and result_dedup_key in session._dedup_result_keys:
            return

        if result_dedup_key:
            session._dedup_result_keys.add(result_dedup_key)

        # Detect running → idle transition
        if session.state.get("is_running"):
            session.state["is_running"] = False
            import asyncio
            asyncio.ensure_future(self._notify_ring0_state_change(session, "running→idle"))
            asyncio.ensure_future(self._push_to_native_clients(session.id, "status_change", {
                "agentStatus": "idle",
                "isRunning": False, "isWaitingForPermission": False,
            }))
            # Restart pen timeout when session goes idle
            if session.controlled_by == "user":
                self._schedule_pen_release(session)
        session.state["is_waiting_for_permission"] = False

        # Turn finished — stop thinking tone and re-enable STT.
        self._stop_thinking(session.id)

        session.state["total_cost_usd"] = msg.get("total_cost_usd", 0)
        session.state["num_turns"] = msg.get("num_turns", 0)

        if isinstance(msg.get("total_lines_added"), (int, float)):
            session.state["total_lines_added"] = msg["total_lines_added"]
        if isinstance(msg.get("total_lines_removed"), (int, float)):
            session.state["total_lines_removed"] = msg["total_lines_removed"]

        model_usage = msg.get("modelUsage")
        if model_usage:
            for usage in model_usage.values():
                ctx_window = usage.get("contextWindow", 0)
                if ctx_window > 0:
                    pct = round((usage.get("inputTokens", 0) + usage.get("outputTokens", 0)) / ctx_window * 100)
                    session.state["context_used_percent"] = max(0, min(pct, 100))

        browser_msg: dict[str, Any] = {"type": "result", "data": msg, "timestamp": int(time.time() * 1000)}
        if result_dedup_key:
            browser_msg["result_dedup_key"] = result_dedup_key
        session.message_history.append(browser_msg)
        await self._broadcast_to_browsers(session, browser_msg)
        self._persist_session(session)

        # Safety net: cap active history at 500 messages
        if len(session.message_history) > 500:
            self._archive_and_trim(session, keep_count=500)

        # Refresh git ahead/behind counts (may have changed if agent committed)
        await self._refresh_git_counts(session)

        # Auto-naming
        if (
            not msg.get("is_error")
            and self._on_first_turn_completed
            and session.id not in self._auto_naming_attempted
        ):
            self._auto_naming_attempted.add(session.id)
            first = next((m for m in session.message_history if m.get("type") == "user_message"), None)
            if first:
                self._on_first_turn_completed(session.id, first.get("content", ""))

    async def _refresh_git_counts(self, session: Session) -> None:
        """Recompute git ahead/behind and push to browsers if changed."""
        cwd = session.state.get("repo_root") or session.state.get("cwd")
        if not cwd:
            return
        try:
            import asyncio
            result = await asyncio.to_thread(
                subprocess.run,
                ["git", "rev-list", "--left-right", "--count", "@{upstream}...HEAD"],
                cwd=cwd, capture_output=True, text=True, timeout=3,
            )
            parts = result.stdout.strip().split()
            if len(parts) == 2:
                new_behind, new_ahead = int(parts[0]), int(parts[1])
                if new_ahead != session.state.get("git_ahead") or new_behind != session.state.get("git_behind"):
                    session.state["git_ahead"] = new_ahead
                    session.state["git_behind"] = new_behind
                    await self._broadcast_to_browsers(session, {
                        "type": "session_update",
                        "session": {"git_ahead": new_ahead, "git_behind": new_behind},
                    })
        except Exception:
            pass

    async def _handle_stream_event(self, session: Session, msg: dict[str, Any]) -> None:
        # Agent is streaming a response — stop thinking tone.
        self._stop_thinking(session.id)
        await self._broadcast_to_browsers(session, {
            "type": "stream_event",
            "event": msg.get("event"),
            "parent_tool_use_id": msg.get("parent_tool_use_id"),
        })

    async def _handle_control_request(self, session: Session, msg: dict[str, Any]) -> None:
        # Tool permission request — agent is working, play thinking tone.
        self._start_thinking_now(session.id)
        request = msg.get("request", {})
        if request.get("subtype") == "can_use_tool":
            import time
            perm: dict[str, Any] = {
                "request_id": msg.get("request_id", ""),
                "tool_name": request.get("tool_name", ""),
                "input": request.get("input", {}),
                "permission_suggestions": request.get("permission_suggestions"),
                "description": request.get("description"),
                "tool_use_id": request.get("tool_use_id", ""),
                "agent_id": request.get("agent_id"),
                "timestamp": int(time.time() * 1000),
            }
            session.pending_permissions[msg.get("request_id", "")] = perm
            await self._broadcast_to_browsers(session, {"type": "permission_request", "request": perm})
            await self._push_to_native_clients(session.id, "permission_request", {"request": perm})
            self._persist_session(session)
            # Notify Ring0 of every permission request — not just the first.
            tool_name = perm.get("tool_name", "?")
            desc = perm.get("description") or ""
            short_desc = desc if len(desc) <= 120 else desc[:117].rsplit(" ", 1)[0] + "..."
            detail = f"{tool_name}: {short_desc}" if desc else tool_name
            pending_count = len(session.pending_permissions)
            if pending_count > 1:
                detail = f"{detail} ({pending_count} pending)"
            if not session.state.get("is_waiting_for_permission"):
                session.state["is_waiting_for_permission"] = True
                asyncio.ensure_future(self._notify_ring0_state_change(
                    session, "running→waiting_for_permission", detail=detail))
            else:
                # Already waiting — still notify Ring0 about the new permission
                asyncio.ensure_future(self._notify_ring0_state_change(
                    session, "waiting_for_permission", detail=detail))

    async def _handle_tool_progress(self, session: Session, msg: dict[str, Any]) -> None:
        # Agent is executing a tool — play thinking tone.
        self._start_thinking_now(session.id)
        await self._broadcast_to_browsers(session, {
            "type": "tool_progress",
            "tool_use_id": msg.get("tool_use_id"),
            "tool_name": msg.get("tool_name"),
            "elapsed_time_seconds": msg.get("elapsed_time_seconds"),
        })

    async def _handle_tool_use_summary(self, session: Session, msg: dict[str, Any]) -> None:
        await self._broadcast_to_browsers(session, {
            "type": "tool_use_summary",
            "summary": msg.get("summary"),
            "tool_use_ids": msg.get("preceding_tool_use_ids"),
        })

    async def _handle_auth_status(self, session: Session, msg: dict[str, Any]) -> None:
        await self._broadcast_to_browsers(session, {
            "type": "auth_status",
            "isAuthenticating": msg.get("isAuthenticating"),
            "output": msg.get("output"),
            "error": msg.get("error"),
        })

    # ── Browser message routing ──────────────────────────────────────────

    async def _route_browser_message(self, session: Session, msg: dict[str, Any], ws: web.WebSocketResponse | None = None) -> None:
        # Remote session — forward to node via tunnel
        if self._is_remote_session(session.id):
            node_id, raw_id = self.parse_qualified_id(session.id)
            if self._node_registry:
                node = self._node_registry.get_node(node_id)
                if node and node.tunnel and node.tunnel.connected:
                    source_client_id = session.browser_sockets.get(ws, "") if ws else ""
                    await node.tunnel.send_fire_and_forget({
                        "type": "browser_message",
                        "sessionId": raw_id,
                        "message": msg,
                        "sourceClientId": source_client_id,
                    })
                    return
            logger.warning("[ws-bridge] Cannot route to remote node %s", node_id[:8])
            return

        # For computer-use sessions, delegate to the agent
        if session.backend_type == "computer-use":
            agent = self._computer_use_agents.get(session.id)
            if not agent:
                logger.warning("[ws-bridge] No computer-use agent for session %s", session.id)
                return
            msg_type = msg.get("type")
            if msg_type == "user_message":
                content = msg.get("content", "")
                import time
                source_client_id = session.browser_sockets.get(ws, "") if ws else ""
                history_entry: dict[str, Any] = {
                    "type": "user_message",
                    "content": content,
                    "timestamp": int(time.time() * 1000),
                }
                if source_client_id:
                    history_entry["sourceClientId"] = source_client_id
                session.message_history.append(history_entry)
                self._persist_session(session)
                # Broadcast user message to other browsers
                await self._broadcast_to_browsers(session, {
                    "type": "user_message",
                    "content": content,
                    "timestamp": history_entry["timestamp"],
                })
                from server.computer_use_agent import ExecutionMode
                exec_mode_str = msg.get("executionMode", "auto")
                try:
                    exec_mode = ExecutionMode(exec_mode_str)
                except ValueError:
                    exec_mode = ExecutionMode.AUTO
                agent.submit_task(content, mode=exec_mode)
            elif msg_type == "interrupt":
                agent.interrupt()
            elif msg_type == "approve":
                agent.approve()
            elif msg_type == "reject":
                agent.reject()
            elif msg_type == "watch_start":
                agent.watch_start(
                    prompt=msg.get("prompt"),
                    interval=msg.get("interval", 5.0),
                )
            elif msg_type == "watch_stop":
                agent.watch_stop()
            return

        # For Codex sessions, delegate to the adapter
        if session.backend_type == "codex":
            if msg.get("type") == "user_message":
                import time
                source_client_id = session.browser_sockets.get(ws, "") if ws else ""
                history_entry: dict[str, Any] = {
                    "type": "user_message",
                    "content": msg.get("content", ""),
                    "timestamp": int(time.time() * 1000),
                }
                if source_client_id:
                    history_entry["sourceClientId"] = source_client_id
                session.message_history.append(history_entry)
                self._persist_session(session)
            if msg.get("type") == "permission_response":
                session.pending_permissions.pop(msg.get("request_id", ""), None)
                self._persist_session(session)

            if session.codex_adapter:
                session.codex_adapter.send_browser_message(msg)
            else:
                logger.info(f"[ws-bridge] Codex adapter not yet attached for session {session.id}, queuing {msg.get('type')}")
                session.pending_messages.append(json.dumps(msg))
            return

        # Claude Code path — look up which client sent this message
        source_client_id = session.browser_sockets.get(ws, "") if ws else ""
        msg_type = msg.get("type")
        if msg_type == "user_message":
            await self._handle_user_message(session, msg, source_client_id=source_client_id)
        elif msg_type == "permission_response":
            await self._handle_permission_response(session, msg)
        elif msg_type == "interrupt":
            self._handle_interrupt(session)
        elif msg_type == "set_model":
            self._handle_set_model(session, msg.get("model", ""))
        elif msg_type == "set_permission_mode":
            self._handle_set_permission_mode(session, msg.get("mode", ""))

    async def _handle_user_message(self, session: Session, msg: dict[str, Any], source_client_id: str = "", **kwargs: Any) -> None:
        content = msg.get("content", "")
        preview = content[:80] if isinstance(content, str) else str(type(content))
        logger.info("[ws-bridge] session %s: user_message received %r", session.id, preview)
        import time
        ts = int(time.time() * 1000)

        # "Take the Pen" — user typing in browser UI claims session control
        ring0 = self._ring0_manager
        if source_client_id and ring0 and session.id != ring0.session_id and not msg.get("eventMeta"):
            if session.controlled_by != "user":
                session.controlled_by = "user"
                session.state["controlledBy"] = "user"
                logger.info("[ws-bridge] User took the pen for session %s", session.id[:8])
                await self._broadcast_to_browsers(session, {"type": "session_update", "session": {"controlledBy": "user"}})
            session.pen_taken_at = time.time()
            self._schedule_pen_release(session)
        # Track last prompt time for MRU session ordering (skip Ring0 events)
        if not msg.get("eventMeta"):
            session.last_prompted_at = float(ts)

        session._user_msg_counter += 1
        history_entry: dict[str, Any] = {
            "type": "user_message",
            "content": msg.get("content", ""),
            "timestamp": ts,
            "id": f"user-{ts}-{session._user_msg_counter}",
        }
        if source_client_id:
            history_entry["sourceClientId"] = source_client_id
        if msg.get("eventMeta"):
            history_entry["eventMeta"] = msg["eventMeta"]
        session.message_history.append(history_entry)

        # If this session is Ring0, prepend client context to message content
        raw_content = msg.get("content", "")
        ring0 = self._ring0_manager
        if ring0 and ring0.session_id == session.id and source_client_id:
            raw_content = f"[from client {source_client_id}]\n{raw_content}"

        images = msg.get("images")
        if images:
            blocks: list[Any] = []
            for img in images:
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": img["media_type"], "data": img["data"]},
                })
            blocks.append({"type": "text", "text": raw_content})
            content: Any = blocks
        else:
            content = raw_content

        ndjson_msg: dict[str, Any] = {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
            "session_id": msg.get("session_id") or session.state.get("session_id", ""),
        }
        if source_client_id:
            ndjson_msg["sourceClientId"] = source_client_id
        ndjson = json.dumps(ndjson_msg)
        self._send_to_cli(session, ndjson)
        self._persist_session(session)

    async def _handle_permission_response(self, session: Session, msg: dict[str, Any]) -> None:
        request_id = msg.get("request_id", "")
        pending = session.pending_permissions.pop(request_id, None)

        if msg.get("behavior") == "allow":
            response: dict[str, Any] = {
                "behavior": "allow",
                "updatedInput": msg.get("updated_input") or (pending.get("input", {}) if pending else {}),
            }
            updated_perms = msg.get("updated_permissions")
            if updated_perms:
                response["updatedPermissions"] = updated_perms
            ndjson = json.dumps({
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": response,
                },
            })
        else:
            ndjson = json.dumps({
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": {
                        "behavior": "deny",
                        "message": msg.get("message", "Denied by user"),
                    },
                },
            })
        self._send_to_cli(session, ndjson)
        # Fire state transition based on permission response
        if session.state.get("is_waiting_for_permission"):
            if session.pending_permissions:
                # More permissions still pending — stay in waiting state and
                # re-notify Ring0 so it can approve the next one.
                next_perm = next(iter(session.pending_permissions.values()))
                tool_name = next_perm.get("tool_name", "?")
                desc = next_perm.get("description") or ""
                short_desc = desc if len(desc) <= 120 else desc[:117].rsplit(" ", 1)[0] + "..."
                detail = f"{tool_name}: {short_desc}" if desc else tool_name
                remaining = len(session.pending_permissions)
                detail = f"{detail} ({remaining} pending)"
                asyncio.ensure_future(self._notify_ring0_state_change(
                    session, "waiting_for_permission", detail=detail))
            else:
                session.state["is_waiting_for_permission"] = False
                if msg.get("behavior") == "allow":
                    asyncio.ensure_future(self._notify_ring0_state_change(session, "waiting_for_permission→running"))
                else:
                    session.state["is_running"] = False
                    asyncio.ensure_future(self._notify_ring0_state_change(session, "waiting_for_permission→idle"))
        self._persist_session(session)
        # Notify all browsers and native clients so other devices dismiss the banner.
        await self._broadcast_to_browsers(session, {
            "type": "permission_cancelled",
            "request_id": request_id,
        })
        await self._push_to_native_clients(session.id, "permission_cancelled", {"request_id": request_id})

    def _handle_interrupt(self, session: Session) -> None:
        self.interrupt_session(session.id)

    def interrupt_session(self, session_id: str) -> bool:
        """Send an interrupt (Ctrl+C equivalent) to a session's CLI.

        Returns True if the session was found and interrupt sent.
        """
        # Remote session — forward interrupt through tunnel
        if self._is_remote_session(session_id):
            node_id, raw_id = self.parse_qualified_id(session_id)
            if self._node_registry:
                node = self._node_registry.get_node(node_id)
                if node and node.tunnel and node.tunnel.connected:
                    import asyncio as _asyncio
                    _asyncio.ensure_future(node.tunnel.send_fire_and_forget({
                        "type": "interrupt",
                        "sessionId": raw_id,
                    }))
                    return True
            return False

        session = self._sessions.get(session_id)
        if not session:
            return False
        ndjson = json.dumps({
            "type": "control_request",
            "request_id": str(uuid.uuid4()),
            "request": {"subtype": "interrupt"},
        })
        self._send_to_cli(session, ndjson)
        return True

    def _handle_set_model(self, session: Session, model: str) -> None:
        ndjson = json.dumps({
            "type": "control_request",
            "request_id": str(uuid.uuid4()),
            "request": {"subtype": "set_model", "model": model},
        })
        self._send_to_cli(session, ndjson)

    def _handle_set_permission_mode(self, session: Session, mode: str) -> None:
        ndjson = json.dumps({
            "type": "control_request",
            "request_id": str(uuid.uuid4()),
            "request": {"subtype": "set_permission_mode", "mode": mode},
        })
        self._send_to_cli(session, ndjson)

    # ── Public API for submitting messages programmatically ────────────

    async def submit_user_message(self, session_id: str, text: str, source_client_id: str = "") -> str | None:
        """Submit a user message to the CLI (e.g. from STT transcript).

        Returns None on success, or an error string if blocked.
        """
        # Remote session — forward through node tunnel
        if self._is_remote_session(session_id):
            node_id, raw_id = self.parse_qualified_id(session_id)
            if self._node_registry:
                node = self._node_registry.get_node(node_id)
                if node and node.tunnel and node.tunnel.connected:
                    await node.tunnel.send_fire_and_forget({
                        "type": "submit_message",
                        "sessionId": raw_id,
                        "content": text,
                        "sourceClientId": source_client_id,
                    })
                    return None
            return "Remote node unavailable"

        session = self._sessions.get(session_id)
        if not session:
            logger.warning("[ws-bridge] Cannot submit message: no session %s", session_id)
            return "Session not found"
        # Block Ring0 sends when user has the pen (voice/STT sources are exempt)
        if session.controlled_by == "user" and not source_client_id:
            logger.info("[ws-bridge] Blocked Ring0 send to session %s: user has the pen", session_id[:8])
            return "Session is under user control"
        if source_client_id:
            logger.info("[ws-bridge] Message from client=%s for session %s", source_client_id, session_id)

        msg = {"type": "user_message", "content": text}
        await self._handle_user_message(session, msg, source_client_id=source_client_id)
        # Schedule thinking tone after a delay — gives the agent time to
        # start streaming a response before we play the tone.
        self._start_thinking_delayed(session_id)
        # Also broadcast to browsers so the transcript appears in chat.
        broadcast: dict[str, Any] = {
            "type": "user_message",
            "content": text,
            "source": "voice",
            "sessionId": session_id,
        }
        if source_client_id:
            broadcast["sourceClientId"] = source_client_id
        await self._broadcast_to_browsers(session, broadcast)

    # ── Ring0 event notifications ────────────────────────────────────────

    async def emit_ring0_event(self, event: Any) -> None:
        """Process an event through the router, then send to LLM and/or UI.

        ``event`` is a Ring0Event (imported lazily to avoid circular deps).
        Two orthogonal knobs from the config rule:
          - send: whether to submit to Ring0 CLI as a user message
          - ui: how (or whether) the browser renders it
        """
        ring0 = self._ring0_manager
        if not ring0 or not ring0.is_enabled or not ring0.session_id:
            return
        if ring0.events_muted:
            return
        ring0_session = self._sessions.get(ring0.session_id)
        if not ring0_session or not ring0_session.cli_socket:
            return

        router = getattr(self, "_ring0_event_router", None)
        if router:
            processed = router.process(event)
        else:
            # No router configured — default: send + visible
            import json as _json
            evt_type = event.fields.get("type", "unknown")
            rest = {k: v for k, v in event.fields.items() if k != "type"}
            processed_text = f"[event {evt_type}] {_json.dumps(rest)}"
            from server.ring0_events import ProcessedEvent
            processed = ProcessedEvent(
                text=processed_text, summary=None, ui="visible",
                send=True, event=event,
            )

        # Skip entirely if not sending to LLM and hidden from UI
        if not processed.send and processed.ui == "hidden":
            return

        logger.info("[ws-bridge] Ring0 event (send=%s, ui=%s): %s",
                     processed.send, processed.ui, processed.text[:120])

        session = self._sessions.get(ring0.session_id)
        if not session:
            return

        event_meta = {
            "eventType": processed.event.fields.get("type", "unknown"),
            "summary": processed.summary,
            "ui": processed.ui,
        }

        # Submit to CLI if send=true
        if processed.send:
            msg = {"type": "user_message", "content": processed.text, "eventMeta": event_meta}
            await self._handle_user_message(session, msg)

        # Broadcast to browsers if UI is not hidden
        if processed.ui != "hidden":
            broadcast: dict[str, Any] = {
                "type": "user_message",
                "content": processed.text,
                "source": "event",
                "sessionId": ring0.session_id,
                "eventMeta": event_meta,
            }
            await self._broadcast_to_browsers(session, broadcast)

    async def _notify_ring0_state_change(self, session: Session, transition: str, *, detail: str = "") -> None:
        """Notify Ring0 of a session state transition (e.g. idle->running)."""
        ring0 = self._ring0_manager
        if not ring0:
            return
        # Don't notify Ring0 about its own session
        if session.id == ring0.session_id:
            return
        # Don't notify hub Ring0 about remote node sessions (they handle their own)
        if self._is_remote_session(session.id):
            return
        # Suppress notifications while user has the pen
        if session.controlled_by == "user":
            return
        from server import session_names
        from server.ring0_events import Ring0Event
        name = session_names.get_name(session.id) or session.id[:8]
        # Keep event fields ASCII-clean
        transition = transition.replace("→", "->")
        fields: dict[str, str] = {
            "type": "session_state_change",
            "session": name, "sessionId": session.id[:8], "transition": transition,
        }
        if detail:
            fields["detail"] = detail
        await self.emit_ring0_event(Ring0Event(fields=fields))

    # ── "Take the Pen" helpers ───────────────────────────────────────────

    def _schedule_pen_release(self, session: Session) -> None:
        """Schedule auto-release of pen after 5 minutes of idle."""
        if session._pen_timeout:
            session._pen_timeout.cancel()
        loop = asyncio.get_event_loop()
        session._pen_timeout = loop.call_later(
            300, lambda: asyncio.ensure_future(self._release_pen(session))
        )

    async def _release_pen(self, session: Session) -> None:
        """Release pen back to Ring0 (idempotent)."""
        if session.controlled_by != "user":
            return
        session.controlled_by = "ring0"
        session.pen_taken_at = 0
        if session._pen_timeout:
            session._pen_timeout.cancel()
            session._pen_timeout = None
        session.state["controlledBy"] = "ring0"
        logger.info("[ws-bridge] Pen released for session %s", session.id[:8])
        await self._broadcast_to_browsers(session, {"type": "session_update", "session": {"controlledBy": "ring0"}})

    # ── Client RPC ────────────────────────────────────────────────────────

    def get_all_clients(self) -> dict[str, dict[str, str]]:
        """Return {clientId: {sessionId, role}} for all connected browser clients."""
        return {
            cid: {"sessionId": sid, "role": self._client_roles.get(cid, "primary")}
            for cid, sid in self._client_sessions.items()
        }

    def get_second_screen_clients(self) -> dict[str, str]:
        """Return {clientId: sessionId} for connected second screen clients."""
        return {
            cid: sid
            for cid, sid in self._client_sessions.items()
            if self._client_roles.get(cid) == "secondscreen"
        }

    async def _native_rpc(self, client_id: str, method: str, params: dict | None = None, timeout: float = 5.0) -> dict:
        """Send a command to the native WebSocket and await the response."""
        native_ws = self._native_ws_by_client.get(client_id)
        if not native_ws:
            # Prefix match fallback
            for cid, w in self._native_ws_by_client.items():
                if cid.startswith(client_id):
                    native_ws = w
                    break
        if not native_ws or native_ws.closed:
            raise RuntimeError(f"No native WS for client {client_id}")
        rpc_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._native_rpc_pending[rpc_id] = future
        msg: dict = {"command": method, "id": rpc_id}
        if params:
            msg["params"] = params
        try:
            logger.info("[ws-bridge] Native RPC send: client=%s command=%s id=%s", client_id[:8], method, rpc_id[:8])
            await native_ws.send_str(json.dumps(msg))
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._native_rpc_pending.pop(rpc_id, None)
            raise RuntimeError(f"Native RPC to client {client_id} timed out after {timeout}s")
        except Exception:
            self._native_rpc_pending.pop(rpc_id, None)
            raise

    async def rpc_call(self, client_id: str, method: str, params: dict | None = None, timeout: float = 5.0) -> dict:
        """Send an RPC request to a specific browser client and await the response."""
        # Native-only commands: prefer native socket (works when WebView is paused)
        _native_methods = {"bring_to_foreground", "launch_app"}
        if method in _native_methods:
            native_ws = self._native_ws_by_client.get(client_id)
            if not native_ws:
                for cid in self._native_ws_by_client:
                    if cid.startswith(client_id):
                        native_ws = self._native_ws_by_client[cid]
                        break
            if native_ws and not native_ws.closed:
                return await self._native_rpc(client_id, method, params=params, timeout=timeout)
            # Fall through to JS RPC path (PWA/browser)

        ws = self._ws_by_client.get(client_id)
        if not ws:
            # Prefix match fallback (Ring0 often passes short ID prefixes)
            for cid, w in self._ws_by_client.items():
                if cid.startswith(client_id):
                    ws = w
                    break
        if not ws or ws.closed:
            raise RuntimeError(f"Client {client_id} not connected")
        rpc_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._rpc_pending[rpc_id] = future
        msg = {"type": "rpc_request", "id": rpc_id, "method": method}
        if params:
            msg["params"] = params
        try:
            logger.info("[ws-bridge] RPC send: client=%s method=%s rpc_id=%s ws_closed=%s", client_id[:8], method, rpc_id[:8], ws.closed)
            await ws.send_str(json.dumps(msg))
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._rpc_pending.pop(rpc_id, None)
            logger.warning("[ws-bridge] RPC timeout: client=%s method=%s rpc_id=%s after %.0fs", client_id[:8], method, rpc_id[:8], timeout)
            raise RuntimeError(f"RPC call to client {client_id} timed out after {timeout}s")
        except Exception:
            self._rpc_pending.pop(rpc_id, None)
            raise

    # ── Shutdown ──────────────────────────────────────────────────────────

    async def close_all(self) -> None:
        """Close all WebSocket connections and cancel background timers."""
        # Cancel thinking timers.
        for handle in self._thinking_timers.values():
            handle.cancel()
        self._thinking_timers.clear()

        # Cancel active TTS.
        for tts in self._active_tts.values():
            tts.cancel()
        self._active_tts.clear()

        # Close all WebSocket connections.
        for session in self._sessions.values():
            for ws in list(session.browser_sockets):
                await ws.close()
            if session.cli_socket:
                await session.cli_socket.close()

        # Close all native WebSocket connections and clear subscription state.
        for ws in list(self._native_ws_by_client.values()):
            try:
                await ws.close()
            except Exception:
                pass
        self._native_ws_by_client.clear()
        self._native_subscriptions.clear()
        self._native_subscribe_all.clear()

    # ── Transport helpers ────────────────────────────────────────────────

    def _send_to_cli(self, session: Session, ndjson: str) -> None:
        # Remote session — forward through node tunnel
        if self._is_remote_session(session.id):
            node_id, raw_id = self.parse_qualified_id(session.id)
            if self._node_registry:
                node = self._node_registry.get_node(node_id)
                if node and node.tunnel and node.tunnel.connected:
                    msg = json.loads(ndjson)
                    import asyncio as _asyncio
                    _asyncio.ensure_future(node.tunnel.send_fire_and_forget({
                        "type": "cli_input",
                        "sessionId": raw_id,
                        "message": msg,
                    }))
                    return
            logger.warning("[ws-bridge] Cannot forward to remote node %s — tunnel unavailable", node_id[:8])
            return

        if not session.cli_socket:
            logger.info(f"[ws-bridge] CLI not yet connected for session {session.id}, queuing message")
            session.pending_messages.append(ndjson)
            # Auto-relaunch CLI so queued messages get processed
            if self._on_cli_relaunch_needed:
                self._on_cli_relaunch_needed(session.id)
            return
        import asyncio
        asyncio.ensure_future(session.cli_socket.send_str(ndjson + "\n"))

    async def broadcast_name_update(self, session_id: str, name: str, user_renamed: bool = False) -> None:
        session = self._sessions.get(session_id)
        if not session:
            return
        msg: dict[str, Any] = {"type": "session_name_update", "name": name}
        if user_renamed:
            msg["userRenamed"] = True
        await self._broadcast_to_browsers(session, msg)
        self._persist_session(session)
        asyncio.ensure_future(self._push_to_all_native_clients("sessions_changed"))

    async def broadcast_ring0_switch_ui(self, target_session_id: str, *, client_id: str = "") -> bool:
        """Send ring0_switch_ui to a specific client or broadcast to all browsers.

        Returns True if the message was sent successfully, False if the target client was not found.
        """
        msg = {"type": "ring0_switch_ui", "sessionId": target_session_id}
        data = json.dumps(msg)

        # Target a specific client if provided
        if client_id:
            ws = self._ws_by_client.get(client_id)
            if not ws:
                # Prefix match fallback
                for cid, w in self._ws_by_client.items():
                    if cid.startswith(client_id):
                        ws = w
                        break
            if ws and not ws.closed:
                try:
                    await ws.send_str(data)
                    return True
                except Exception:
                    pass
            return False

        # Broadcast to all browsers
        for session in self._sessions.values():
            dead: list[web.WebSocketResponse] = []
            for ws in session.browser_sockets:
                try:
                    await ws.send_str(data)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                cid = session.browser_sockets.pop(ws, "")
                if cid:
                    self._client_sessions.pop(cid, None)
                    self._ws_by_client.pop(cid, None)
        return True

    def get_message_history(self, session_id: str) -> list[dict[str, Any]]:
        """Get message history for a session (used by Ring0 MCP)."""
        session = self._sessions.get(session_id)
        if not session:
            return []
        return list(session.message_history)

    def get_pending_permissions(self, session_id: str) -> list[dict[str, Any]]:
        """Get pending permission requests for a session."""
        session = self._sessions.get(session_id)
        if not session:
            return []
        return list(session.pending_permissions.values())

    def get_pending_permission_count(self, session_id: str) -> int:
        """Get the number of pending permission requests for a session."""
        session = self._sessions.get(session_id)
        if not session:
            return 0
        return len(session.pending_permissions)

    async def respond_to_permission(
        self, session_id: str, request_id: str, behavior: str, message: str = ""
    ) -> bool:
        """Respond to a pending permission request (used by Ring0 MCP).

        Returns True if the permission was found and responded to.
        """
        session = self._sessions.get(session_id)
        if not session:
            return False
        pending = session.pending_permissions.pop(request_id, None)
        if not pending:
            # Try prefix match
            for rid in list(session.pending_permissions):
                if rid.startswith(request_id):
                    request_id = rid
                    pending = session.pending_permissions.pop(rid, None)
                    break
        if not pending:
            return False

        if behavior == "allow":
            response: dict[str, Any] = {
                "behavior": "allow",
                "updatedInput": pending.get("input", {}),
            }
            ndjson = json.dumps({
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": response,
                },
            })
        else:
            ndjson = json.dumps({
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": {
                        "behavior": "deny",
                        "message": message or "Denied by ring0",
                    },
                },
            })
        self._send_to_cli(session, ndjson)
        # Fire state transition based on permission response
        if session.state.get("is_waiting_for_permission"):
            if session.pending_permissions:
                # More permissions still pending — stay in waiting state and
                # re-notify Ring0 so it can approve the next one.
                next_perm = next(iter(session.pending_permissions.values()))
                tool_name = next_perm.get("tool_name", "?")
                desc = next_perm.get("description") or ""
                short_desc = desc if len(desc) <= 120 else desc[:117].rsplit(" ", 1)[0] + "..."
                detail = f"{tool_name}: {short_desc}" if desc else tool_name
                remaining = len(session.pending_permissions)
                detail = f"{detail} ({remaining} pending)"
                asyncio.ensure_future(self._notify_ring0_state_change(
                    session, "waiting_for_permission", detail=detail))
            else:
                session.state["is_waiting_for_permission"] = False
                if behavior == "allow":
                    asyncio.ensure_future(self._notify_ring0_state_change(session, "waiting_for_permission→running"))
                else:
                    session.state["is_running"] = False
                    asyncio.ensure_future(self._notify_ring0_state_change(session, "waiting_for_permission→idle"))
        self._persist_session(session)
        await self._broadcast_to_browsers(session, {
            "type": "permission_cancelled",
            "request_id": request_id,
        })
        await self._push_to_native_clients(session.id, "permission_cancelled", {"request_id": request_id})
        return True

    async def _broadcast_to_browsers(self, session: Session, msg: dict[str, Any]) -> None:
        # On vibr8-node: forward to hub tunnel instead of local browsers
        if self._broadcast_hook:
            await self._broadcast_hook(session.id, msg)
            return

        msg_type = msg.get("type")
        if msg_type in ("assistant", "result", "user_message"):
            logger.info(
                "[ws-bridge] Broadcasting %s to %d browsers for session %s",
                msg_type, len(session.browser_sockets), session.id[:8],
            )
        data = json.dumps(msg)
        dead: list[web.WebSocketResponse] = []
        for ws in session.browser_sockets:
            try:
                await ws.send_str(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            client_id = session.browser_sockets.pop(ws, "")
            if client_id:
                self._client_sessions.pop(client_id, None)
                self._ws_by_client.pop(client_id, None)

    async def _send_initial_state(self, client_id: str) -> None:
        """Push a full state snapshot to a native client after subscribe."""
        ws = self._native_ws_by_client.get(client_id)
        if not ws or ws.closed:
            return
        subscribe_all = client_id in self._native_subscribe_all
        subscribed_ids = self._native_subscriptions.get(client_id)
        session_states: list[dict[str, Any]] = []
        for session in self._sessions.values():
            if not subscribe_all:
                if not subscribed_ids or session.id not in subscribed_ids:
                    continue
            perms = [
                {"id": req_id, "tool_name": p.get("tool_name", ""), "description": p.get("description", "")}
                for req_id, p in session.pending_permissions.items()
            ]
            session_states.append({
                "sessionId": session.id,
                "cliConnected": session.cli_socket is not None or session.codex_adapter is not None,
                "agentStatus": self._derive_agent_status(session),
                "pendingPermissions": perms,
            })
        msg = json.dumps({"type": "push", "event": "initial_state", "sessions": session_states})
        try:
            await ws.send_str(msg)
        except Exception:
            logger.debug("[ws-bridge] Failed to send initial_state to native client %s", client_id[:8])

    def _derive_agent_status(self, session: Session) -> str:
        """Canonical agent runtime status: idle, running, waiting_for_permission, compacting."""
        if session.pending_permissions:
            return "waiting_for_permission"
        if session.state.get("is_compacting"):
            return "compacting"
        if session.state.get("is_running"):
            return "running"
        return "idle"

    async def _push_to_native_clients(self, session_id: str, event: str, payload: dict[str, Any] | None = None) -> None:
        """Push a notification to native clients subscribed to *session_id*."""
        if not self._native_ws_by_client:
            return
        msg: dict[str, Any] = {"type": "push", "event": event, "sessionId": session_id}
        if payload:
            msg.update(payload)
        data = json.dumps(msg)
        dead: list[str] = []
        for cid, ws in self._native_ws_by_client.items():
            if cid not in self._native_subscribe_all:
                subs = self._native_subscriptions.get(cid)
                if not subs or session_id not in subs:
                    continue
            try:
                await ws.send_str(data)
            except Exception:
                dead.append(cid)
        for cid in dead:
            self._native_ws_by_client.pop(cid, None)
            self._native_subscriptions.pop(cid, None)
            self._native_subscribe_all.discard(cid)

    async def _push_to_all_native_clients(self, event: str, payload: dict[str, Any] | None = None) -> None:
        """Push a notification to ALL connected native clients (not session-scoped)."""
        if not self._native_ws_by_client:
            return
        msg: dict[str, Any] = {"type": "push", "event": event}
        if payload:
            msg.update(payload)
        data = json.dumps(msg)
        dead: list[str] = []
        for cid, ws in self._native_ws_by_client.items():
            try:
                await ws.send_str(data)
            except Exception:
                dead.append(cid)
        for cid in dead:
            self._native_ws_by_client.pop(cid, None)
            self._native_subscriptions.pop(cid, None)
            self._native_subscribe_all.discard(cid)

    async def _send_to_client(self, client_id: str, msg: dict[str, Any]) -> None:
        """Send a message to a specific client's current browser WebSocket."""
        ws = self._ws_by_client.get(client_id)
        if ws and not ws.closed:
            try:
                await ws.send_str(json.dumps(msg))
            except Exception:
                pass

    async def _send_to_browser(self, ws: web.WebSocketResponse, msg: dict[str, Any]) -> None:
        try:
            await ws.send_str(json.dumps(msg))
        except Exception:
            pass
