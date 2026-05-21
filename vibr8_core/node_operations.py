"""NodeOperations — canonical implementation of all node-scoped operations.

Every operation a hub or remote client can perform against a node — listing
sessions, sending messages, controlling Ring0, managing CLI subprocesses, etc.
— lives here as a single method. Both the remote node tunnel dispatcher and
(eventually) the hub's NodeClient layer call into this class. There is one
implementation per operation; the only thing that varies is the transport.

Method naming is snake_case to match Python convention. The remote-node
tunnel dispatcher converts incoming camelCase payload keys to snake_case
before calling. See node_agent.py:_dispatch_command.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional

from vibr8_core.cli_launcher import CliLauncher, LaunchOptions
from vibr8_core.ws_bridge import WsBridge
from vibr8_core.session_store import SessionStore
from vibr8_core.ring0 import Ring0Manager
from vibr8_core import session_names

logger = logging.getLogger(__name__)


SessionsChangedCallback = Callable[[], Awaitable[None]]


class NodeOperations:
    """Canonical node operations. Backend-agnostic, transport-agnostic."""

    def __init__(
        self,
        *,
        launcher: CliLauncher,
        bridge: WsBridge,
        store: SessionStore,
        ring0: Ring0Manager | None,
        desktop_webrtc: Any = None,
        default_backend: str = "claude",
        work_dir: str = "",
        on_sessions_changed: Optional[SessionsChangedCallback] = None,
    ) -> None:
        self._launcher = launcher
        self._bridge = bridge
        self._store = store
        self._ring0 = ring0
        self._desktop_webrtc = desktop_webrtc
        self._default_backend = default_backend
        self._work_dir = work_dir
        self._on_sessions_changed = on_sessions_changed

    async def _notify_sessions_changed(self) -> None:
        if self._on_sessions_changed:
            try:
                await self._on_sessions_changed()
            except Exception:
                logger.exception("on_sessions_changed callback failed")

    # ── Session listing & lifecycle ───────────────────────────────────────

    async def list_sessions(self) -> dict:
        sessions = []
        names = session_names.get_all_names()
        for s in self._launcher.list_sessions():
            s_dict = s.to_dict() if hasattr(s, "to_dict") else s.__dict__
            sid = s_dict.get("sessionId", "")
            s_dict["name"] = names.get(sid, s_dict.get("name"))
            lpa = self._bridge.get_last_prompted_at(sid)
            if lpa:
                s_dict["lastPromptedAt"] = lpa
            if self._ring0 and sid == self._ring0.session_id:
                s_dict["isRing0"] = True
            sessions.append(s_dict)
        return {"sessions": sessions}

    async def create_session(self, options: dict | None = None) -> dict:
        options = options or {}
        opts = LaunchOptions(
            model=options.get("model"),
            permissionMode=options.get("permissionMode"),
            cwd=options.get("cwd") or self._work_dir or None,
            backendType=options.get("backend", self._default_backend),
        )
        info = self._launcher.launch(opts)
        result = info.to_dict() if hasattr(info, "to_dict") else info.__dict__
        await self._notify_sessions_changed()
        return result

    async def kill_session(self, session_id: str = "") -> dict:
        killed = await self._launcher.kill(session_id)
        await self._notify_sessions_changed()
        return {"ok": killed}

    async def relaunch_session(self, session_id: str = "") -> dict:
        ok = await self._launcher.relaunch(session_id)
        await self._notify_sessions_changed()
        return {"ok": ok}

    async def delete_session(self, session_id: str = "") -> dict:
        await self._launcher.kill(session_id)
        self._launcher.remove_session(session_id)
        await self._bridge.close_session(session_id)
        await self._notify_sessions_changed()
        return {"ok": True}

    async def archive_session(self, session_id: str = "") -> dict:
        await self._launcher.kill(session_id)
        self._launcher.set_archived(session_id, True)
        await self._notify_sessions_changed()
        return {"ok": True}

    async def unarchive_session(self, session_id: str = "") -> dict:
        self._launcher.set_archived(session_id, False)
        await self._notify_sessions_changed()
        return {"ok": True}

    async def rename_session(self, session_id: str = "", name: str = "") -> dict:
        name = (name or "").strip()
        if not name:
            return {"error": "name is required"}
        session_names.set_name(session_id, name, unique=False)
        await self._notify_sessions_changed()
        return {"ok": True, "name": name}

    # ── Messaging & permission ────────────────────────────────────────────

    async def submit_message(
        self,
        session_id: str = "",
        content: str = "",
        source_client_id: str = "",
    ) -> dict:
        err = await self._bridge.submit_user_message(
            session_id, content, source_client_id=source_client_id,
        )
        if err:
            return {"error": err}
        return {"ok": True}

    async def cli_input(self, session_id: str = "", message: dict | None = None) -> dict:
        """Forward raw CLI input (NDJSON message) to local session."""
        session = self._bridge._sessions.get(session_id)
        if not session:
            return {"error": f"Session {session_id} not found"}
        ndjson = json.dumps(message or {})
        await self._bridge._send_to_cli(session, ndjson)
        return {"ok": True}

    async def interrupt(self, session_id: str = "") -> dict:
        ok = self._bridge.interrupt_session(session_id)
        return {"ok": ok}

    async def browser_message(
        self,
        session_id: str = "",
        message: dict | None = None,
        source_client_id: str = "",
    ) -> dict:
        session = self._bridge._sessions.get(session_id)
        if not session:
            return {"error": f"Session {session_id} not found"}
        await self._bridge._route_browser_message(
            session, message or {}, ws=None, source_client_id=source_client_id,
        )
        return {"ok": True}

    async def get_session_output(self, session_id: str = "") -> dict:
        session = self._bridge._sessions.get(session_id)
        if not session:
            return {"error": f"Session {session_id} not found"}
        return {"messages": session.message_history[-500:]}

    async def set_permission_mode(self, session_id: str = "", mode: str = "") -> dict:
        session = self._bridge._sessions.get(session_id)
        if not session:
            return {"error": f"Session {session_id} not found"}
        self._bridge._handle_set_permission_mode(session, mode)
        return {"ok": True}

    async def respond_permission(self, session_id: str = "", **payload) -> dict:
        session = self._bridge._sessions.get(session_id)
        if not session:
            return {"error": f"Session {session_id} not found"}
        # _handle_permission_response expects the original camelCase payload
        msg = {"sessionId": session_id, **_snake_to_camel_dict(payload)}
        await self._bridge._handle_permission_response(session, msg)
        return {"ok": True}

    # ── Ring0 ─────────────────────────────────────────────────────────────

    async def ring0_input(self, text: str = "", source_client_id: str = "") -> dict:
        if not self._ring0 or not self._ring0.is_enabled:
            return {"error": "Ring0 not enabled on this node"}
        if not text:
            return {"error": "Empty text"}
        r0sid = self._ring0.session_id
        if not r0sid:
            r0sid = await self._ring0.ensure_session(
                self._launcher, self._bridge, backend_type=self._default_backend,
            )
        if not r0sid:
            return {"error": "Ring0 session not available"}
        await self._bridge.submit_user_message(
            r0sid, text, source_client_id=source_client_id,
        )
        return {"ok": True}

    # ── WebRTC ────────────────────────────────────────────────────────────

    async def webrtc_offer(
        self,
        client_id: str = "",
        sdp: str = "",
        sdp_type: str = "offer",
        desktop_role: str = "controller",
        ice_servers: list | None = None,
    ) -> dict:
        if not self._desktop_webrtc:
            return {"error": "Desktop WebRTC not available on this node"}
        if not client_id or not sdp:
            return {"error": "clientId and sdp required"}
        try:
            answer = await self._desktop_webrtc.handle_offer(
                client_id, sdp, sdp_type,
                desktop_role=desktop_role,
                ice_servers=ice_servers,
            )
            return answer
        except Exception as e:
            logger.exception("Failed to handle desktop WebRTC offer")
            return {"error": str(e)}


# ── Payload key translation helpers ────────────────────────────────────────

import re

_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")


def camel_to_snake(name: str) -> str:
    return _CAMEL_BOUNDARY.sub(r"\1_\2", name).lower()


def snake_to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


def _snake_to_camel_dict(d: dict) -> dict:
    return {snake_to_camel(k): v for k, v in d.items()}


def payload_to_kwargs(msg: dict, *, drop: tuple = ("type", "requestId")) -> dict:
    """Translate a wire payload (camelCase keys) to NodeOperations kwargs (snake_case)."""
    return {
        camel_to_snake(k): v
        for k, v in msg.items()
        if k not in drop
    }
