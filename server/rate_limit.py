"""Small in-memory sliding-window rate limit helpers."""

from __future__ import annotations

import ipaddress
import os
import time
from collections.abc import Iterable, Mapping
from typing import Any

TRUST_PROXY_ENV = "VIBR8_TRUST_PROXY"


def _env_flag(environ: Mapping[str, str], name: str) -> bool:
    return environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _strip_ip_port(value: str) -> str:
    value = value.strip().strip('"')
    if value.startswith("[") and "]" in value:
        return value[1:value.index("]")]
    if value.count(":") == 1:
        host, port = value.rsplit(":", 1)
        if port.isdigit():
            return host
    return value


def _normalize_ip_rate_limit_key(value: str) -> str | None:
    candidate = _strip_ip_port(value) or "unknown"
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        network = ipaddress.ip_network(f"{address}/64", strict=False)
        return f"{network.network_address}/64"
    return str(address)


def normalize_rate_limit_key(value: str) -> str:
    """Normalize an address for rate-limit bucketing."""
    return _normalize_ip_rate_limit_key(value) or (_strip_ip_port(value) or "unknown")


def _forwarded_for_candidates(headers: Any) -> Iterable[str]:
    forwarded_for = headers.get("X-Forwarded-For", "")
    for part in forwarded_for.split(","):
        value = part.strip()
        if value:
            yield value

    forwarded = headers.get("Forwarded", "")
    for entry in forwarded.split(","):
        for param in entry.split(";"):
            name, separator, value = param.strip().partition("=")
            if separator and name.strip().lower() == "for":
                value = value.strip()
                if value:
                    yield value
                break


def get_client_rate_limit_key(
    request: Any,
    *,
    environ: Mapping[str, str] = os.environ,
) -> str:
    """Return the request key used for in-memory rate limits.

    X-Forwarded-For is trusted only when VIBR8_TRUST_PROXY is explicitly set.
    """
    remote = request.remote or "unknown"
    if _env_flag(environ, TRUST_PROXY_ENV):
        for forwarded_ip in _forwarded_for_candidates(request.headers):
            key = _normalize_ip_rate_limit_key(forwarded_ip)
            if key:
                return key
    return normalize_rate_limit_key(remote)


def check_rate_limit(
    buckets: dict[str, list[float]],
    key: str,
    *,
    limit: int,
    window: float,
) -> bool:
    """Record one request and return True when the caller is already limited."""
    now = time.time()
    cutoff = now - window
    timestamps = [t for t in buckets.get(key, []) if t > cutoff]
    if not timestamps:
        buckets.pop(key, None)
    else:
        buckets[key] = timestamps
    if len(timestamps) >= limit:
        return True
    buckets.setdefault(key, timestamps).append(now)
    return False
