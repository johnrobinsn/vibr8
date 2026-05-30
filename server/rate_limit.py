"""Small in-memory sliding-window rate limit helpers."""

from __future__ import annotations

import time


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
