"""Manage speaker voice fingerprints stored as JSON files.

Each user has a directory of fingerprint JSON files under
``$VIBR8_DATA_DIR/voice/fingerprints/{username}/``.

Each fingerprint contains a 192-dim ECAPA-TDNN embedding (L2-normalized).
An ``active.json`` file tracks the selected fingerprint and threshold.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import numpy as np

DATA_DIR = Path(os.environ.get("VIBR8_DATA_DIR", str(Path.home() / ".vibr8" / "data")))
FINGERPRINTS_DIR = DATA_DIR / "voice" / "fingerprints"


def _user_dir(username: str) -> Path:
    d = FINGERPRINTS_DIR / username
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fp_path(username: str, fp_id: str) -> Path:
    return _user_dir(username) / f"{fp_id}.json"


def _active_path(username: str) -> Path:
    return _user_dir(username) / "active.json"


# ── CRUD ─────────────────────────────────────────────────────────────────────


def list_fingerprints(username: str) -> list[dict]:
    """List all fingerprints (without embedding vectors, for UI display)."""
    d = _user_dir(username)
    fps: list[dict] = []
    for f in d.iterdir():
        if f.suffix == ".json" and f.name != "active.json":
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                fps.append({
                    "id": data["id"],
                    "name": data["name"],
                    "user": data.get("user", username),
                    "createdAt": data.get("createdAt", 0),
                })
            except Exception:
                pass
    fps.sort(key=lambda p: p.get("name", ""))
    return fps


def get_fingerprint(username: str, fp_id: str) -> dict | None:
    """Get a fingerprint including its embedding vector."""
    p = _fp_path(username, fp_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def create_fingerprint(username: str, name: str, embedding: list[float] | np.ndarray) -> dict:
    """Create a new fingerprint from a 192-dim embedding."""
    fp_id = str(uuid.uuid4())
    if isinstance(embedding, np.ndarray):
        embedding = embedding.tolist()
    fp = {
        "id": fp_id,
        "name": name,
        "user": username,
        "embedding": embedding,
        "createdAt": time.time(),
    }
    _fp_path(username, fp_id).write_text(json.dumps(fp, indent=2), encoding="utf-8")
    return fp


def delete_fingerprint(username: str, fp_id: str) -> bool:
    p = _fp_path(username, fp_id)
    if not p.exists():
        return False
    p.unlink()
    active = get_active(username)
    if active and active.get("fingerprintId") == fp_id:
        clear_active(username)
    return True


# ── Active fingerprint selection ─────────────────────────────────────────────


def get_active(username: str) -> dict | None:
    """Get active fingerprint config: {fingerprintId, threshold}, or None."""
    p = _active_path(username)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not data.get("fingerprintId"):
            return None
        return data
    except Exception:
        return None


def set_active(username: str, fp_id: str, threshold: float = 0.45) -> dict | None:
    """Set the active fingerprint for gating. Returns the config or None if fingerprint not found."""
    fp = get_fingerprint(username, fp_id)
    if not fp:
        return None
    config = {"fingerprintId": fp_id, "threshold": float(threshold)}
    _active_path(username).write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config


def clear_active(username: str) -> None:
    """Disable speaker gating."""
    p = _active_path(username)
    if p.exists():
        p.unlink()


def get_active_gate(username: str) -> dict | None:
    """Get the active gate data for STT: {embedding: np.ndarray, threshold: float}, or None.

    This loads the full fingerprint including embedding, ready for cosine comparison.
    """
    active = get_active(username)
    if not active:
        return None
    fp = get_fingerprint(username, active["fingerprintId"])
    if not fp or "embedding" not in fp:
        return None
    return {
        "embedding": np.array(fp["embedding"], dtype=np.float32),
        "threshold": active["threshold"],
    }
