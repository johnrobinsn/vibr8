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
import random
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
DEVICE_TOKENS_FILE = VIBR8_DIR / "device-tokens.json"

SESSION_MAX_AGE = 30 * 86400  # 30 days
PAIRING_TTL = 600  # 10 minutes
PAIRING_RATE_LIMIT = 10  # max codes per IP per minute
PAIRING_RATE_WINDOW = 60  # seconds
PAIRING_FAIL_THRESHOLD = 5  # failed lookups before cooldown
PAIRING_FAIL_COOLDOWN = 1.0  # seconds

# Routes that don't require auth
PUBLIC_PREFIXES = (
    "/ws/cli/",
    "/ws/node/",
    "/api/auth/login",
    "/api/auth/me",
    "/api/pairing/request",
    "/api/pairing/status/",
    "/api/ring0/",
    "/api/nodes/register",
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
        self._device_tokens: list[dict] = []  # device token metadata
        self._revoked_device_sigs: set[str] = set()  # revoked token signatures
        self._pairing_codes: dict[str, dict] = {}  # code → {type, expiresAt, ...}
        self._pairing_rate: dict[str, list[float]] = {}  # ip → [timestamps]
        self._pairing_fails: dict[str, list[float]] = {}  # ip → [fail timestamps]
        self._load_users()
        self._load_device_tokens()

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
        """Create an HMAC-signed session token: s:username:timestamp:signature."""
        ts = str(int(time.time()))
        payload = f"{username}:{ts}"
        sig = hmac.new(
            self._secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return f"s:{payload}:{sig}"

    def validate_session(self, token: str) -> Optional[str]:
        """Validate an HMAC-signed token (session or device). Stateless — survives server restarts.

        Token formats:
          s:username:timestamp:signature   (session, 30-day expiry)
          d:username:timestamp:signature   (device, no expiry, revocable)
          username:timestamp:signature     (legacy session, backward compat)
        """
        # Determine token type
        token_type = "s"  # default: session (legacy compat)
        if token.startswith("s:") or token.startswith("d:"):
            token_type = token[0]
            token = token[2:]  # strip prefix

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
        # Check expiry (session tokens only)
        try:
            created = int(ts_str)
        except ValueError:
            return None
        if token_type == "s":
            if time.time() - created > SESSION_MAX_AGE:
                return None
        # Check revocation (device tokens only)
        if token_type == "d":
            if sig in self._revoked_device_sigs:
                return None
            # Track last-used time
            self._update_device_last_used(sig)
        # Verify user still exists
        if username not in self._users:
            return None
        return username

    def revoke_session(self, token: str) -> None:
        # Stateless tokens can't be individually revoked server-side.
        # The cookie is deleted client-side via del_cookie.
        pass

    # ── Device tokens ────────────────────────────────────────────────────

    def _load_device_tokens(self) -> None:
        if DEVICE_TOKENS_FILE.exists():
            try:
                data = json.loads(DEVICE_TOKENS_FILE.read_text())
                self._device_tokens = data.get("tokens", [])
                self._revoked_device_sigs = {
                    t["signature"] for t in self._device_tokens if t.get("revoked")
                }
                logger.info(
                    "[auth] Loaded %d device token(s)", len(self._device_tokens)
                )
            except Exception:
                logger.exception("[auth] Failed to load device tokens")

    def _save_device_tokens(self) -> None:
        VIBR8_DIR.mkdir(parents=True, exist_ok=True)
        DEVICE_TOKENS_FILE.write_text(json.dumps({"tokens": self._device_tokens}, indent=2))

    def _update_device_last_used(self, sig: str) -> None:
        now = int(time.time())
        for t in self._device_tokens:
            if t.get("signature") == sig and not t.get("revoked"):
                t["lastUsedAt"] = now
                self._save_device_tokens()
                return

    def create_device_token(self, username: str, name: str) -> dict:
        """Create a device token (d: prefix, no expiry). Returns full token only once."""
        ts = str(int(time.time()))
        payload = f"{username}:{ts}"
        sig = hmac.new(
            self._secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        full_token = f"d:{payload}:{sig}"
        token_id = f"dt_{secrets.token_hex(6)}"
        meta = {
            "id": token_id,
            "name": name,
            "username": username,
            "signature": sig,
            "createdAt": int(ts),
            "lastUsedAt": None,
            "revoked": False,
        }
        self._device_tokens.append(meta)
        self._save_device_tokens()
        logger.info("[auth] Created device token %s (%s) for user %s", token_id, name, username)
        return {
            "token": full_token,
            "tokenId": token_id,
            "name": name,
            "createdAt": int(ts),
        }

    def list_device_tokens(self, username: str) -> list[dict]:
        """List non-revoked device tokens for a user (without the actual token)."""
        return [
            {
                "id": t["id"],
                "name": t["name"],
                "createdAt": t["createdAt"],
                "lastUsedAt": t.get("lastUsedAt"),
            }
            for t in self._device_tokens
            if t["username"] == username and not t.get("revoked")
        ]

    def revoke_device_token(self, username: str, token_id: str) -> bool:
        """Revoke a device token by id. Returns True if found and revoked."""
        for t in self._device_tokens:
            if t["id"] == token_id and t["username"] == username and not t.get("revoked"):
                t["revoked"] = True
                self._revoked_device_sigs.add(t["signature"])
                self._save_device_tokens()
                logger.info("[auth] Revoked device token %s for user %s", token_id, username)
                return True
        return False

    # ── Unified pairing ────────────────────────────────────────────────

    def check_pairing_rate_limit(self, ip: str) -> bool:
        """Return True if IP is rate-limited for pairing requests."""
        now = time.time()
        cutoff = now - PAIRING_RATE_WINDOW
        timestamps = [t for t in self._pairing_rate.get(ip, []) if t > cutoff]
        self._pairing_rate[ip] = timestamps
        return len(timestamps) >= PAIRING_RATE_LIMIT

    def check_pairing_brute_force(self, ip: str) -> bool:
        """Return True if IP is in cooldown from too many failed status lookups."""
        now = time.time()
        cutoff = now - PAIRING_FAIL_COOLDOWN
        timestamps = [t for t in self._pairing_fails.get(ip, []) if t > cutoff]
        self._pairing_fails[ip] = timestamps
        return len(timestamps) >= PAIRING_FAIL_THRESHOLD

    def record_pairing_fail(self, ip: str) -> None:
        self._pairing_fails.setdefault(ip, []).append(time.time())

    def request_pairing(self, device_type: str, ip: str, client_id: str = "") -> dict:
        """Generate a 6-digit pairing code. Returns {code, expiresAt}.

        device_type: "native" or "second-screen"
        client_id: required for second-screen (the device's persistent UUID)
        """
        self._purge_expired_pairings()
        # Record rate
        self._pairing_rate.setdefault(ip, []).append(time.time())
        code = f"{random.SystemRandom().randint(0, 999999):06d}"
        while code in self._pairing_codes:
            code = f"{random.SystemRandom().randint(0, 999999):06d}"
        expires_at = int(time.time() + PAIRING_TTL)
        entry: dict = {"type": device_type, "expiresAt": expires_at}
        if client_id:
            entry["clientId"] = client_id
        self._pairing_codes[code] = entry
        logger.info("[auth] Created %s pairing code (expires in %ds)", device_type, PAIRING_TTL)
        return {"code": code, "expiresAt": expires_at}

    def confirm_pairing(self, code: str, username: str, name: str) -> Optional[dict]:
        """Confirm a pairing code. Returns {type, ...} or None on failure.

        For native: creates device token, stores in code entry for polling.
        For second-screen: returns clientId for caller to handle registration.
        """
        self._purge_expired_pairings()
        entry = self._pairing_codes.get(code)
        if not entry:
            return None
        if entry.get("confirmed"):
            return None  # already confirmed
        device_type = entry["type"]
        entry["confirmed"] = True
        if device_type == "native":
            result = self.create_device_token(username, name)
            entry["token"] = result["token"]
            entry["tokenId"] = result["tokenId"]
            logger.info("[auth] Confirmed native pairing for user %s, token %s", username, result["tokenId"])
            return {"type": "native", "tokenId": result["tokenId"]}
        else:  # second-screen
            entry["pairedUser"] = username
            entry["name"] = name
            client_id = entry.get("clientId", "")
            logger.info("[auth] Confirmed second-screen pairing for user %s, client %s", username, client_id[:8])
            return {"type": "second-screen", "clientId": client_id, "pairedUser": username}

    def get_pairing_status(self, code: str, ip: str) -> dict:
        """Check pairing status. Returns {status, ...}. Single-use token delivery for native."""
        self._purge_expired_pairings()
        entry = self._pairing_codes.get(code)
        if not entry:
            self.record_pairing_fail(ip)
            return {"status": "expired"}
        if not entry.get("confirmed"):
            return {"status": "pending", "expiresAt": entry["expiresAt"]}
        device_type = entry["type"]
        if device_type == "native":
            token = entry.get("token", "")
            del self._pairing_codes[code]  # single-use
            return {"status": "complete", "token": token}
        else:  # second-screen
            client_id = entry.get("clientId", "")
            paired_user = entry.get("pairedUser", "")
            del self._pairing_codes[code]
            return {"status": "complete", "clientId": client_id, "pairedUser": paired_user}

    def _purge_expired_pairings(self) -> None:
        now = time.time()
        self._pairing_codes = {c: v for c, v in self._pairing_codes.items() if v["expiresAt"] > now}


def _extract_auth_user(request: web.Request, auth_mgr: AuthManager) -> Optional[str]:
    """Extract authenticated username from cookie, Bearer header, or ?token= query param."""
    # 1. Cookie
    token = request.cookies.get("vibr8_session")
    if token:
        username = auth_mgr.validate_session(token)
        if username:
            return username
    # 2. Authorization: Bearer <token>
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if token:
            username = auth_mgr.validate_session(token)
            if username:
                return username
    # 3. ?token=<token> query param (for WebSocket connections)
    token = request.query.get("token")
    if token:
        username = auth_mgr.validate_session(token)
        if username:
            return username
    return None


@web.middleware
async def auth_middleware(
    request: web.Request, handler: web.RequestHandler
) -> web.StreamResponse:
    auth_mgr: Optional[AuthManager] = request.app.get("auth_manager")

    # If auth not enabled, pass through
    if not auth_mgr or not auth_mgr.enabled:
        return await handler(request)

    path = request.path

    # Public routes — still extract user from credentials if available (non-blocking)
    if any(path.startswith(p) for p in PUBLIC_PREFIXES):
        username = _extract_auth_user(request, auth_mgr)
        if username:
            request["auth_user"] = username
        return await handler(request)

    # Non-API, non-WS routes (SPA static files) — always serve so login page works
    if not path.startswith("/api/") and not path.startswith("/ws/"):
        return await handler(request)

    # Check credentials: cookie → Bearer header → ?token= query param
    username = _extract_auth_user(request, auth_mgr)
    if username:
        request["auth_user"] = username
        return await handler(request)

    # Unauthorized
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return web.Response(status=401, text="Unauthorized")

    return web.json_response({"error": "Unauthorized"}, status=401)
