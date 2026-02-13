"""Fetch OAuth usage limits from the Anthropic API.

Ported from companion/web/server/usage-limits.ts.
Reads the OAuth access token from the macOS keychain (via ``security``) or
from the credentials file on Windows/Linux, then queries the usage endpoint.
Results are cached in memory with a 60-second TTL.
"""

from __future__ import annotations

import json
import platform
import re
import subprocess
import time
from pathlib import Path
from typing import Optional, TypedDict

import aiohttp


# ─── Types ──────────────────────────────────────────────────────────────────


class _RateBucket(TypedDict):
    utilization: float
    resets_at: Optional[str]


class _ExtraUsage(TypedDict):
    is_enabled: bool
    monthly_limit: float
    used_credits: float
    utilization: Optional[float]


class UsageLimits(TypedDict):
    five_hour: Optional[_RateBucket]
    seven_day: Optional[_RateBucket]
    extra_usage: Optional[_ExtraUsage]


# ─── In-memory cache (60 s TTL) ─────────────────────────────────────────────

_CACHE_DURATION_S = 60.0
_cache: dict[str, object] | None = None  # {"data": UsageLimits, "timestamp": float}


# ─── Credential helpers ─────────────────────────────────────────────────────


def get_credentials() -> Optional[str]:
    """Return the Claude OAuth access token, or *None* on failure.

    On macOS the token is read from the system keychain via the ``security``
    CLI.  On Windows/Linux it falls back to
    ``~/.claude/.credentials.json``.
    """
    try:
        system = platform.system()

        if system == "Windows":
            # Windows: read from credentials file
            home = Path.home()
            cred_path = home / ".claude" / ".credentials.json"
            if not cred_path.exists():
                return None
            content = cred_path.read_text(encoding="utf-8")
            parsed = json.loads(content)
            token: Optional[str] = (parsed.get("claudeAiOauth") or {}).get(
                "accessToken"
            )
            return token or None

        # macOS / Linux: read from system keychain
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                "Claude Code-credentials",
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None

        raw = result.stdout.strip()
        if not raw:
            return None

        # The keychain value is either raw JSON or hex-encoded JSON.
        if raw.startswith("{"):
            decoded = raw
        else:
            decoded = bytes.fromhex(raw).decode("utf-8")

        match = re.search(r'"claudeAiOauth":\{"accessToken":"(sk-ant-[^"]+)"', decoded)
        return match.group(1) if match else None
    except Exception:
        return None


# ─── HTTP fetch ──────────────────────────────────────────────────────────────


async def fetch_usage_limits(token: str) -> Optional[UsageLimits]:
    """Query the Anthropic OAuth usage endpoint and return parsed limits."""
    try:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "claude-code/2.0.31",
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.anthropic.com/api/oauth/usage",
                headers=headers,
            ) as response:
                if response.status != 200:
                    return None

                data = await response.json()
                return UsageLimits(
                    five_hour=data.get("five_hour") or None,
                    seven_day=data.get("seven_day") or None,
                    extra_usage=data.get("extra_usage") or None,
                )
    except Exception:
        return None


# ─── Cached public API ───────────────────────────────────────────────────────

_EMPTY: UsageLimits = UsageLimits(
    five_hour=None,
    seven_day=None,
    extra_usage=None,
)


async def get_usage_limits() -> UsageLimits:
    """Return current usage limits, cached for 60 seconds."""
    global _cache

    try:
        if _cache is not None:
            ts: float = _cache["timestamp"]  # type: ignore[index]
            if time.time() - ts < _CACHE_DURATION_S:
                return _cache["data"]  # type: ignore[index,return-value]

        token = get_credentials()
        if token is None:
            return _EMPTY

        limits = await fetch_usage_limits(token)
        if limits is None:
            return _EMPTY

        _cache = {"data": limits, "timestamp": time.time()}
        return limits
    except Exception:
        return _EMPTY
