"""Authentication — user/password auth with session cookies.

Users stored in ~/.vibr8/users.json (bcrypt-hashed passwords).
Auth is opt-in: if no users.json exists, auth is disabled.

Session tokens are HMAC-signed (stateless) so they survive server restarts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Optional

import bcrypt
from aiohttp import web

logger = logging.getLogger(__name__)

VIBR8_DIR = Path.home() / ".vibr8"
USERS_FILE = VIBR8_DIR / "users.json"
SECRET_FILE = VIBR8_DIR / "secret.key"

SESSION_MAX_AGE = 30 * 86400  # 30 days

# Routes that don't require auth
PUBLIC_PREFIXES = (
    "/ws/cli/",
    "/ws/native/",
    "/ws/browser/",
    "/api/auth/login",
    "/api/auth/me",
    "/api/ring0/",
    "/api/second-screen/",
    "/api/clients/",
    "/assets/",
    "/sw.js",
    "/manifest.json",
    "/logo",
    "/favicon",
    "/apple-touch-icon",
)


def _load_or_create_secret() -> str:
    """Load persistent HMAC secret from disk, creating one if needed."""
    VIBR8_DIR.mkdir(parents=True, exist_ok=True)
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text().strip()
    secret = secrets.token_hex(32)
    SECRET_FILE.write_text(secret)
    SECRET_FILE.chmod(0o600)
    logger.info("[auth] Generated new signing secret at %s", SECRET_FILE)
    return secret


class AuthManager:
    def __init__(self) -> None:
        self._users: dict[str, str] = {}  # username → bcrypt hash
        self._secret = ""
        self._load_users()

    def _load_users(self) -> None:
        if USERS_FILE.exists():
            try:
                data = json.loads(USERS_FILE.read_text())
                self._users = data.get("users", {})
                self._secret = _load_or_create_secret()
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
        """Create an HMAC-signed session token: username:timestamp:signature."""
        ts = str(int(time.time()))
        payload = f"{username}:{ts}"
        sig = hmac.new(
            self._secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return f"{payload}:{sig}"

    def validate_session(self, token: str) -> Optional[str]:
        """Validate an HMAC-signed token. Stateless — survives server restarts."""
        parts = token.split(":", 2)
        if len(parts) != 3:
            return None
        username, ts_str, sig = parts
        # Verify signature
        payload = f"{username}:{ts_str}"
        expected = hmac.new(
            self._secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        # Check expiry
        try:
            created = int(ts_str)
        except ValueError:
            return None
        if time.time() - created > SESSION_MAX_AGE:
            return None
        # Verify user still exists
        if username not in self._users:
            return None
        return username

    def revoke_session(self, token: str) -> None:
        # Stateless tokens can't be individually revoked server-side.
        # The cookie is deleted client-side via del_cookie.
        pass


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
