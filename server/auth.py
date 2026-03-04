"""Authentication — user/password auth with session cookies.

Users stored in ~/.companion/users.json (bcrypt-hashed passwords).
Auth is opt-in: if no users.json exists, auth is disabled.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from pathlib import Path
from typing import Optional

import bcrypt
from aiohttp import web

logger = logging.getLogger(__name__)

USERS_FILE = Path.home() / ".companion" / "users.json"

# Routes that don't require auth
PUBLIC_PREFIXES = (
    "/api/auth/login",
    "/api/auth/me",
    "/assets/",
    "/sw.js",
    "/manifest.json",
    "/logo",
    "/favicon",
    "/apple-touch-icon",
)


class AuthManager:
    def __init__(self) -> None:
        self._users: dict[str, str] = {}  # username → bcrypt hash
        self._sessions: dict[str, dict] = {}  # token → {username, created_at}
        self._load_users()

    def _load_users(self) -> None:
        if USERS_FILE.exists():
            try:
                data = json.loads(USERS_FILE.read_text())
                self._users = data.get("users", {})
                logger.info(
                    "[auth] Loaded %d user(s) from %s", len(self._users), USERS_FILE
                )
            except Exception:
                logger.exception("[auth] Failed to load users file")
        else:
            logger.info("[auth] No users file at %s — auth disabled", USERS_FILE)

    @property
    def enabled(self) -> bool:
        return len(self._users) > 0

    def verify(self, username: str, password: str) -> bool:
        stored_hash = self._users.get(username)
        if not stored_hash:
            return False
        return bcrypt.checkpw(password.encode(), stored_hash.encode())

    def create_session(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[token] = {
            "username": username,
            "created_at": time.time(),
        }
        return token

    def validate_session(self, token: str) -> Optional[str]:
        session = self._sessions.get(token)
        if not session:
            return None
        # 30-day expiry
        if time.time() - session["created_at"] > 30 * 86400:
            self._sessions.pop(token, None)
            return None
        return session["username"]

    def revoke_session(self, token: str) -> None:
        self._sessions.pop(token, None)


@web.middleware
async def auth_middleware(
    request: web.Request, handler: web.RequestHandler
) -> web.StreamResponse:
    auth_mgr: Optional[AuthManager] = request.app.get("auth_manager")

    # If auth not enabled, pass through
    if not auth_mgr or not auth_mgr.enabled:
        return await handler(request)

    path = request.path

    # Public routes
    if any(path.startswith(p) for p in PUBLIC_PREFIXES):
        return await handler(request)

    # Non-API, non-WS routes (SPA static files) — always serve so login page works
    if not path.startswith("/api/") and not path.startswith("/ws/"):
        return await handler(request)

    # Check session cookie
    token = request.cookies.get("vibr8_session")
    if token:
        username = auth_mgr.validate_session(token)
        if username:
            request["auth_user"] = username
            return await handler(request)

    # Unauthorized
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return web.Response(status=401, text="Unauthorized")

    return web.json_response({"error": "Unauthorized"}, status=401)
