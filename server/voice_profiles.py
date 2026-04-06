"""Manage voice profiles stored as JSON files.

Each user has a directory of profile JSON files under
``$VIBR8_DATA_DIR/voice/profiles/{username}/``.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict
from pathlib import Path


# ── Paths ────────────────────────────────────────────────────────────────────

DATA_DIR = Path(os.environ.get("VIBR8_DATA_DIR", str(Path.home() / ".vibr8" / "data")))
PROFILES_DIR = DATA_DIR / "voice" / "profiles"


def _user_dir(username: str) -> Path:
    d = PROFILES_DIR / username
    d.mkdir(parents=True, exist_ok=True)
    return d


def _profile_path(username: str, profile_id: str) -> Path:
    return _user_dir(username) / f"{profile_id}.json"


# ── CRUD ─────────────────────────────────────────────────────────────────────


def list_profiles(username: str) -> list[dict]:
    d = _user_dir(username)
    profiles: list[dict] = []
    for f in d.iterdir():
        if f.suffix == ".json":
            try:
                profiles.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                pass
    profiles.sort(key=lambda p: p.get("name", ""))
    return profiles


def get_profile(username: str, profile_id: str) -> dict | None:
    p = _profile_path(username, profile_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def create_profile(username: str, data: dict) -> dict:
    profile_id = str(uuid.uuid4())
    now = time.time()
    profile = {
        "id": profile_id,
        "name": data.get("name", "Default"),
        "user": username,
        "micGain": float(data.get("micGain", 1.0)),
        "vadThresholdDb": float(data.get("vadThresholdDb", -30.0)),
        "sileroVadThreshold": float(data.get("sileroVadThreshold", 0.4)),
        "eouThreshold": float(data.get("eouThreshold", 0.15)),
        "eouMaxRetries": int(data.get("eouMaxRetries", 3)),
        "eouRetryDelayMs": float(data.get("eouRetryDelayMs", 100.0)),
        "minSegmentDuration": float(data.get("minSegmentDuration", 0.4)),
        "isActive": bool(data.get("isActive", False)),
        "createdAt": now,
        "updatedAt": now,
    }
    _profile_path(username, profile_id).write_text(
        json.dumps(profile, indent=2), encoding="utf-8"
    )
    return profile


def update_profile(username: str, profile_id: str, data: dict) -> dict | None:
    existing = get_profile(username, profile_id)
    if not existing:
        return None

    # Update mutable fields
    for key in ("name", "micGain", "vadThresholdDb", "sileroVadThreshold",
                "eouThreshold", "eouMaxRetries", "eouRetryDelayMs",
                "minSegmentDuration", "isActive"):
        if key in data:
            existing[key] = data[key]
    existing["updatedAt"] = time.time()

    _profile_path(username, profile_id).write_text(
        json.dumps(existing, indent=2), encoding="utf-8"
    )
    return existing


def delete_profile(username: str, profile_id: str) -> bool:
    p = _profile_path(username, profile_id)
    if not p.exists():
        return False
    p.unlink()
    return True


def activate_profile(username: str, profile_id: str) -> dict | None:
    """Set *profile_id* as active, deactivating all others for this user."""
    target = get_profile(username, profile_id)
    if not target:
        return None

    for prof in list_profiles(username):
        if prof["id"] == profile_id:
            continue
        if prof.get("isActive"):
            prof["isActive"] = False
            prof["updatedAt"] = time.time()
            _profile_path(username, prof["id"]).write_text(
                json.dumps(prof, indent=2), encoding="utf-8"
            )

    target["isActive"] = True
    target["updatedAt"] = time.time()
    _profile_path(username, profile_id).write_text(
        json.dumps(target, indent=2), encoding="utf-8"
    )
    return target


def deactivate_all(username: str) -> bool:
    """Deactivate all profiles for *username*, reverting to defaults."""
    changed = False
    for prof in list_profiles(username):
        if prof.get("isActive"):
            prof["isActive"] = False
            prof["updatedAt"] = time.time()
            _profile_path(username, prof["id"]).write_text(
                json.dumps(prof, indent=2), encoding="utf-8"
            )
            changed = True
    return changed


def get_active_profile(username: str) -> dict | None:
    """Return the active profile for *username*, or None if none set."""
    for prof in list_profiles(username):
        if prof.get("isActive"):
            return prof
    return None


def get_stt_params(username: str, profile_id: str | None = None):
    """Resolve a profile (or active profile) to an STTParams instance.

    Imports STTParams lazily to avoid pulling in heavy ML deps at import time.
    """
    from server.stt import STTParams

    prof = None
    if profile_id:
        prof = get_profile(username, profile_id)
    if not prof:
        prof = get_active_profile(username)
    if not prof:
        return STTParams()

    return STTParams(
        mic_gain=prof.get("micGain", 1.0),
        vad_threshold_db=prof.get("vadThresholdDb", -30.0),
        silero_vad_threshold=prof.get("sileroVadThreshold", 0.4),
        eou_threshold=prof.get("eouThreshold", 0.15),
        eou_max_retries=prof.get("eouMaxRetries", 3),
        eou_retry_delay_ms=prof.get("eouRetryDelayMs", 100.0),
        min_segment_duration=prof.get("minSegmentDuration", 0.4),
    )
