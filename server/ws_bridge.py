"""Core WebSocket message router — bridges CLI ↔ browser connections."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import uuid
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
    syncing: bool = False  # True while syncing CLI replay to last known UUID
    sync_uuid: str | None = None  # Target UUID to sync to during replay
    cli_init_seen: bool = False  # True after first init from current CLI connection


# ── Bridge ───────────────────────────────────────────────────────────────────

class WsBridge:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._store: SessionStore | None = None
        self._webrtc_manager: WebRTCManager | None = None
        self._ring0_manager: Any = None  # Ring0Manager (avoid circular import)
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

    # ── Native WebSocket (Android foreground service) ───────────────────

    def register_native_ws(self, client_id: str, ws: web.WebSocketResponse) -> None:
        self._native_ws_by_client[client_id] = ws
        logger.info("[ws-bridge] Native WS registered for client %s", client_id[:8])

    def unregister_native_ws(self, client_id: str) -> None:
        self._native_ws_by_client.pop(client_id, None)
        logger.info("[ws-bridge] Native WS unregistered for client %s", client_id[:8])

    def handle_native_message(self, client_id: str, data: dict) -> None:
        """Handle an incoming message from a native WebSocket (command response)."""
        msg_id = data.get("id")
        if msg_id and msg_id in self._native_rpc_pending:
            future = self._native_rpc_pending.pop(msg_id)
            if not future.done():
                future.set_result(data)

    # ── WebRTC manager ─────────────────────────────────────────────────

    def set_webrtc_manager(self, manager: WebRTCManager) -> None:
        self._webrtc_manager = manager

    def set_ring0_manager(self, manager: Any) -> None:
        self._ring0_manager = manager

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
            self._webrtc_manager.set_thinking(session_id, True)

    def _stop_thinking(self, session_id: str) -> None:
        """Stop the thinking tone and cancel any pending timer."""
        self._cancel_thinking_timer(session_id)
        if self._webrtc_manager:
            self._webrtc_manager.set_thinking(session_id, False)

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
            self._sessions[sid] = session
            if p.name:
                session_names.set_name(sid, p.name, unique=False)
            if session.state.get("num_turns", 0) > 0:
                self._auto_naming_attempted.add(sid)
            count += 1
        if count > 0:
            logger.info(f"[ws-bridge] Restored {count} session(s) from disk")
        return count

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
        ))

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

    async def broadcast_guard_state(self, session_id: str, enabled: bool) -> None:
        """Broadcast guard mode state change to browser clients."""
        session = self._sessions.get(session_id)
        if session:
            await self._broadcast_to_browsers(
                session, {"type": "guard_state", "enabled": enabled}
            )

    async def broadcast_audio_off(self, session_id: str) -> None:
        """Tell browser to disconnect WebRTC audio."""
        session = self._sessions.get(session_id)
        if session:
            await self._broadcast_to_browsers(
                session, {"type": "audio_off"}
            )

    async def broadcast_tts_muted(self, session_id: str, muted: bool) -> None:
        """Broadcast TTS mute state change to browser clients."""
        session = self._sessions.get(session_id)
        if session:
            await self._broadcast_to_browsers(
                session, {"type": "tts_muted", "muted": muted}
            )

    async def broadcast_voice_mode(self, session_id: str, mode: str | None) -> None:
        """Broadcast voice mode change to browser clients."""
        session = self._sessions.get(session_id)
        if session:
            await self._broadcast_to_browsers(
                session, {"type": "voice_mode", "mode": mode}
            )

    def get_all_sessions(self) -> list[dict[str, Any]]:
        return [s.state for s in self._sessions.values()]

    def is_cli_connected(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if not session:
            return False
        if session.backend_type == "codex":
            return session.codex_adapter is not None and session.codex_adapter.is_connected()
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

        # Notify browsers
        import asyncio
        asyncio.ensure_future(self._broadcast_to_browsers(session, {"type": "cli_connected"}))
        logger.info(f"[ws-bridge] Codex adapter attached for session {session_id}")

    # ── CLI WebSocket handlers ───────────────────────────────────────────

    def handle_cli_open(self, ws: web.WebSocketResponse, session_id: str) -> None:
        session = self.get_or_create_session(session_id)
        session.cli_socket = ws
        session.cli_init_seen = False
        logger.info(f"[ws-bridge] CLI connected for session {session_id}")
        import asyncio
        asyncio.ensure_future(self._broadcast_to_browsers(session, {"type": "cli_connected"}))

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
        logger.info(f"[ws-bridge] CLI disconnected for session {session_id}")
        await self._broadcast_to_browsers(session, {"type": "cli_disconnected"})

        for req_id in list(session.pending_permissions):
            await self._broadcast_to_browsers(session, {"type": "permission_cancelled", "request_id": req_id})
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
            if client_id:
                self._client_sessions[client_id] = session_id
                self._ws_by_client[client_id] = ws
                self._client_roles[client_id] = role
            logger.info(f"[ws-bridge] Browser connected for session {session_id} client={client_id or '(none)'} role={role} ({len(session.browser_sockets)} browsers)")

            # Notify Ring0 when a paired second screen connects (not for mirror)
            if client_id and role == "secondscreen" and self._ring0_manager:
                ring0 = self._ring0_manager
                if ring0.is_enabled:
                    await self.submit_user_message(
                        ring0.session_id,
                        f"[event second_screen_connected] clientId={client_id[:8]}"
                    )

        # Send current session state
        await self._send_to_browser(ws, {"type": "session_init", "session": session.state})

        # Replay message history — defer if CLI hasn't sent init yet (sync
        # decision hasn't been made, history may be stale and about to be cleared).
        if session.cli_init_seen:
            if session.message_history:
                await self._send_to_browser(ws, {"type": "message_history", "messages": session.message_history})
        else:
            logger.info("[ws-bridge] session %s: deferring message_history until CLI init", session.id)

        # Send pending permissions
        for perm in session.pending_permissions.values():
            await self._send_to_browser(ws, {"type": "permission_request", "request": perm})

        # Check backend connectivity
        backend_connected = (
            (session.codex_adapter and session.codex_adapter.is_connected())
            if session.backend_type == "codex"
            else session.cli_socket is not None
        )
        if not backend_connected:
            await self._send_to_browser(ws, {"type": "cli_disconnected"})
            if self._on_cli_relaunch_needed:
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
                    if was_second_screen and client_id and self._ring0_manager:
                        ring0 = self._ring0_manager
                        if ring0.is_enabled:
                            await self.submit_user_message(
                                ring0.session_id,
                                f"[event second_screen_disconnected] clientId={client_id[:8]}"
                            )
                break

    # ── CLI message routing ──────────────────────────────────────────────

    async def _route_cli_message(self, session: Session, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type")
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

            cli_sid = msg.get("session_id", "")

            # Replay sync: only on first init after CLI connects (subsequent
            # inits are per-turn during normal operation, not replay).
            if not session.cli_init_seen:
                session.cli_init_seen = True
                stored_sid = session.state.get("cli_session_id", "")
                last_uuid = None
                for entry in reversed(session.message_history):
                    if entry.get("uuid"):
                        last_uuid = entry["uuid"]
                        break

                if cli_sid == stored_sid and last_uuid:
                    # Same session, history has UUIDs — sync to last known UUID
                    session.syncing = True
                    session.sync_uuid = last_uuid
                    logger.info("[ws-bridge] session %s: replay sync started, target uuid=%s", session.id, last_uuid[:8])
                else:
                    # Different session or no UUIDs — clean break
                    if cli_sid != stored_sid:
                        logger.info("[ws-bridge] session %s: CLI session changed (%s → %s), clearing history",
                                    session.id, stored_sid[:8] if stored_sid else "none", cli_sid[:8] if cli_sid else "none")
                    else:
                        logger.info("[ws-bridge] session %s: no UUIDs in history, clearing for fresh sync", session.id)
                    session.message_history.clear()
                    session.syncing = False
                    session.sync_uuid = None

                # Send deferred message history now that sync decision is made.
                # On clean break this sends empty list (clears browser view).
                # On UUID sync this sends existing history (browser catches up).
                await self._broadcast_to_browsers(session, {"type": "message_history", "messages": session.message_history})

            session.state["cli_session_id"] = cli_sid

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
        # During replay sync, suppress until we pass the target UUID
        if session.syncing:
            msg_uuid = msg.get("uuid")
            if msg_uuid == session.sync_uuid:
                session.syncing = False
                session.sync_uuid = None
                logger.info("[ws-bridge] session %s: replay sync complete (matched uuid=%s)", session.id, msg_uuid[:8])
            return

        # Detect idle → running transition
        if not session.state.get("is_running"):
            session.state["is_running"] = True
            import asyncio
            asyncio.ensure_future(self._notify_ring0_state_change(session, "idle→running"))

        text = msg.get("message")

        # TTS: speak assistant response if audio is active and TTS not muted.
        # When Ring0 is enabled, only Ring0's responses trigger TTS.
        if text and self._webrtc_manager:
            ring0 = self._ring0_manager
            is_ring0_session = ring0 and ring0.is_enabled and session.id == ring0.session_id
            tts_allowed = not ring0 or not ring0.is_enabled or is_ring0_session
            # Look up outgoing track — the audio connection may be on a
            # different session than the one producing the response (e.g.
            # Ring0 responds but audio is connected via another session).
            audio_session_id = session.id
            track = self._webrtc_manager.get_outgoing_track(session.id)
            if not track:
                fallback = self._webrtc_manager.get_any_outgoing_track()
                if fallback:
                    audio_session_id, track = fallback
            tts_muted = self._webrtc_manager.is_tts_muted(audio_session_id)
            text_preview = repr(text)[:200] if not isinstance(text, str) else f"{len(text)} chars"
            logger.info(
                "[ws-bridge] TTS check: session=%s, audio_session=%s, text_type=%s, preview=%s, track=%s, tts_muted=%s, tts_allowed=%s",
                session.id, audio_session_id, type(text).__name__, text_preview, track is not None, tts_muted, tts_allowed,
            )
            if track and not tts_muted and tts_allowed:
                import asyncio
                asyncio.ensure_future(self._speak_text(session.id, text, track))

        browser_msg: dict[str, Any] = {
            "type": "assistant",
            "message": text,
            "parent_tool_use_id": msg.get("parent_tool_use_id"),
        }
        if msg.get("uuid"):
            browser_msg["uuid"] = msg["uuid"]
        session.message_history.append(browser_msg)
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
                self._webrtc_manager.set_thinking(session_id, False)
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
        # During replay sync, suppress until we pass the target UUID
        if session.syncing:
            msg_uuid = msg.get("uuid")
            if msg_uuid == session.sync_uuid:
                session.syncing = False
                session.sync_uuid = None
                logger.info("[ws-bridge] session %s: replay sync complete (matched uuid=%s)", session.id, msg_uuid[:8])
            return

        # Detect running → idle transition
        if session.state.get("is_running"):
            session.state["is_running"] = False
            import asyncio
            asyncio.ensure_future(self._notify_ring0_state_change(session, "running→idle"))
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

        browser_msg: dict[str, Any] = {"type": "result", "data": msg}
        if msg.get("uuid"):
            browser_msg["uuid"] = msg["uuid"]
        session.message_history.append(browser_msg)
        await self._broadcast_to_browsers(session, browser_msg)
        self._persist_session(session)

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
        if session.syncing:
            return
        # Agent is streaming a response — stop thinking tone.
        self._stop_thinking(session.id)
        await self._broadcast_to_browsers(session, {
            "type": "stream_event",
            "event": msg.get("event"),
            "parent_tool_use_id": msg.get("parent_tool_use_id"),
        })

    async def _handle_control_request(self, session: Session, msg: dict[str, Any]) -> None:
        if session.syncing:
            return
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
            self._persist_session(session)
            # Fire state transition: running → waiting_for_permission
            if session.state.get("is_running") and not session.state.get("is_waiting_for_permission"):
                session.state["is_waiting_for_permission"] = True
                asyncio.ensure_future(self._notify_ring0_state_change(session, "running→waiting_for_permission"))

    async def _handle_tool_progress(self, session: Session, msg: dict[str, Any]) -> None:
        if session.syncing:
            return
        # Agent is executing a tool — play thinking tone.
        self._start_thinking_now(session.id)
        await self._broadcast_to_browsers(session, {
            "type": "tool_progress",
            "tool_use_id": msg.get("tool_use_id"),
            "tool_name": msg.get("tool_name"),
            "elapsed_time_seconds": msg.get("elapsed_time_seconds"),
        })

    async def _handle_tool_use_summary(self, session: Session, msg: dict[str, Any]) -> None:
        if session.syncing:
            return
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

    async def _handle_user_message(self, session: Session, msg: dict[str, Any], source_client_id: str = "") -> None:
        content = msg.get("content", "")
        preview = content[:80] if isinstance(content, str) else str(type(content))
        logger.info("[ws-bridge] session %s: user_message received %r", session.id, preview)
        # Safety fallback: user message always ends sync
        if session.syncing:
            logger.info("[ws-bridge] session %s: replay sync cleared by user_message", session.id)
            session.syncing = False
            session.sync_uuid = None
        import time
        history_entry: dict[str, Any] = {
            "type": "user_message",
            "content": msg.get("content", ""),
            "timestamp": int(time.time() * 1000),
        }
        if source_client_id:
            history_entry["sourceClientId"] = source_client_id
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
            session.state["is_waiting_for_permission"] = False
            if msg.get("behavior") == "allow":
                asyncio.ensure_future(self._notify_ring0_state_change(session, "waiting_for_permission→running"))
            else:
                session.state["is_running"] = False
                asyncio.ensure_future(self._notify_ring0_state_change(session, "waiting_for_permission→idle"))
        self._persist_session(session)
        # Notify all browsers so other devices dismiss the banner.
        await self._broadcast_to_browsers(session, {
            "type": "permission_cancelled",
            "request_id": request_id,
        })

    def _handle_interrupt(self, session: Session) -> None:
        self.interrupt_session(session.id)

    def interrupt_session(self, session_id: str) -> bool:
        """Send an interrupt (Ctrl+C equivalent) to a session's CLI.

        Returns True if the session was found and interrupt sent.
        """
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

    async def submit_user_message(self, session_id: str, text: str, source_client_id: str = "") -> None:
        """Submit a user message to the CLI (e.g. from STT transcript)."""
        session = self._sessions.get(session_id)
        if not session:
            logger.warning("[ws-bridge] Cannot submit message: no session %s", session_id)
            return
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

    async def _notify_ring0_state_change(self, session: Session, transition: str) -> None:
        """Notify Ring0 of a session state transition (e.g. idle→running)."""
        ring0 = self._ring0_manager
        if not ring0 or not ring0.is_enabled or not ring0.session_id:
            return
        # Don't notify Ring0 about its own session
        if session.id == ring0.session_id:
            return
        # Only notify if Ring0's CLI is connected
        ring0_session = self._sessions.get(ring0.session_id)
        if not ring0_session or not ring0_session.cli_socket:
            return
        from server import session_names
        name = session_names.get_name(session.id) or session.id[:8]
        event_text = f"[event session_state_change] session={name} (id={session.id[:8]}) transition={transition}"
        logger.info("[ws-bridge] Ring0 event: %s", event_text)
        await self.submit_user_message(ring0.session_id, event_text)

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

    # ── Transport helpers ────────────────────────────────────────────────

    def _send_to_cli(self, session: Session, ndjson: str) -> None:
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
        return True

    async def _broadcast_to_browsers(self, session: Session, msg: dict[str, Any]) -> None:
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

    async def _send_to_browser(self, ws: web.WebSocketResponse, msg: dict[str, Any]) -> None:
        try:
            await ws.send_str(json.dumps(msg))
        except Exception:
            pass
