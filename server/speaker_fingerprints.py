"""Manage speaker voice profiles stored as JSON files.

Each user has a directory of profile JSON files under
``$VIBR8_DATA_DIR/voice/fingerprints/{username}/``.

v2 profiles hold multiple labeled embeddings (one per device/environment).
v3 profiles add an optional ``embedding_wespeaker`` field per entry — the same
voiceprint encoded by the WeSpeaker ECAPA model, used to condition the WeSep
BSRNN target-speaker extractor (TSE). The two embedding spaces are
incompatible (different ECAPA training), so both are stored.

v1 files (single embedding) are lazily migrated on read. v2→v3 migration only
bumps the version number — recomputing ``embedding_wespeaker`` requires the
original audio and is done out-of-band by ``server.scripts.migrate_v3_wespeaker``.

An ``active.json`` file tracks the selected speaker name and threshold.
"""

from __future__ import annotations

import io
import json
import os
import time
import uuid
import wave
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


# ── Audio storage ───────────────────────────────────────────────────────────


def _save_audio(username: str, embedding_id: str, audio: np.ndarray) -> str:
    """Save int16 16kHz mono PCM as WAV. Returns the filename."""
    filename = f"{embedding_id}.wav"
    path = _user_dir(username) / filename
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(audio.astype(np.int16).tobytes())
    path.write_bytes(buf.getvalue())
    return filename


def _delete_audio(username: str, filename: str) -> None:
    path = _user_dir(username) / filename
    if path.exists():
        path.unlink()


# ── Schema migrations ──────────────────────────────────────────────────────

CURRENT_VERSION = 3


def _migrate_v1(data: dict, username: str, fp_id: str) -> dict:
    """Convert v1 fingerprint to v2 profile format."""
    emb_id = str(uuid.uuid4())
    return {
        "version": 2,
        "id": data["id"],
        "name": data["name"],
        "user": data.get("user", username),
        "createdAt": data.get("createdAt", time.time()),
        "embeddings": [
            {
                "id": emb_id,
                "label": "Default",
                "embedding": data["embedding"],
                "createdAt": data.get("createdAt", time.time()),
            }
        ],
    }


def _migrate_v2_to_v3(data: dict) -> dict:
    """Bump v2 → v3. Adds an ``embedding_wespeaker`` placeholder (None) to
    each embedding entry; actual recomputation happens out-of-band."""
    data["version"] = 3
    for e in data.get("embeddings", []):
        e.setdefault("embedding_wespeaker", None)
    return data


def _migrate_to_current(data: dict, username: str, fp_id: str) -> dict:
    """Apply all known migrations and persist if anything changed."""
    original_version = data.get("version", 1)
    if original_version == 1:
        data = _migrate_v1(data, username, fp_id)
    if data.get("version") == 2:
        data = _migrate_v2_to_v3(data)
    if data.get("version") != original_version:
        _fp_path(username, fp_id).write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )
    return data


# ── CRUD ─────────────────────────────────────────────────────────────────────


def list_fingerprints(username: str) -> list[dict]:
    """List all profiles (without embedding vectors, for UI display)."""
    d = _user_dir(username)
    fps: list[dict] = []
    for f in d.iterdir():
        if f.suffix == ".json" and f.name != "active.json":
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if data.get("version") != CURRENT_VERSION:
                    data = _migrate_to_current(data, username, data["id"])
                embeddings = data.get("embeddings", [])
                fps.append({
                    "id": data["id"],
                    "name": data["name"],
                    "user": data.get("user", username),
                    "createdAt": data.get("createdAt", 0),
                    "embeddingCount": len(embeddings),
                    "embeddingLabels": [e.get("label", "") for e in embeddings],
                    "embeddingIds": [e.get("id", "") for e in embeddings],
                })
            except Exception:
                pass
    fps.sort(key=lambda p: p.get("name", ""))
    return fps


def get_fingerprint(username: str, fp_id: str) -> dict | None:
    """Get a profile including its embedding vectors."""
    p = _fp_path(username, fp_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("version") != CURRENT_VERSION:
            data = _migrate_to_current(data, username, fp_id)
        return data
    except Exception:
        return None


def _find_profile_by_name(username: str, name: str) -> dict | None:
    """Find a profile by speaker name (case-insensitive)."""
    d = _user_dir(username)
    for f in d.iterdir():
        if f.suffix == ".json" and f.name != "active.json":
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if data.get("version") != CURRENT_VERSION:
                    data = _migrate_to_current(data, username, data["id"])
                if data.get("name", "").lower() == name.lower():
                    return data
            except Exception:
                pass
    return None


def _to_list(v: list[float] | np.ndarray | None) -> list[float] | None:
    if v is None:
        return None
    if isinstance(v, np.ndarray):
        return v.tolist()
    return v


def create_fingerprint(
    username: str,
    name: str,
    embedding: list[float] | np.ndarray,
    label: str = "Default",
    audio: np.ndarray | None = None,
    embedding_wespeaker: list[float] | np.ndarray | None = None,
) -> dict:
    """Create a new v3 profile, or add an embedding to an existing profile with the same name.

    ``embedding`` is the SpeechBrain ECAPA vector (used for the speaker gate).
    ``embedding_wespeaker`` is the WeSpeaker ECAPA vector (used to condition
    the WeSep BSRNN target-speaker extractor); optional but required for TSE.
    """
    embedding = _to_list(embedding)

    existing = _find_profile_by_name(username, name)
    if existing:
        return add_embedding(
            username, existing["id"], embedding, label, audio, embedding_wespeaker
        )

    fp_id = str(uuid.uuid4())
    emb_id = str(uuid.uuid4())
    now = time.time()

    audio_path = None
    if audio is not None:
        audio_path = _save_audio(username, emb_id, audio)

    emb_entry: dict = {
        "id": emb_id,
        "label": label,
        "embedding": embedding,
        "embedding_wespeaker": _to_list(embedding_wespeaker),
        "createdAt": now,
    }
    if audio_path:
        emb_entry["audioPath"] = audio_path

    fp = {
        "version": CURRENT_VERSION,
        "id": fp_id,
        "name": name,
        "user": username,
        "createdAt": now,
        "embeddings": [emb_entry],
    }
    _fp_path(username, fp_id).write_text(json.dumps(fp, indent=2), encoding="utf-8")
    return fp


def add_embedding(
    username: str,
    profile_id: str,
    embedding: list[float] | np.ndarray,
    label: str = "Default",
    audio: np.ndarray | None = None,
    embedding_wespeaker: list[float] | np.ndarray | None = None,
) -> dict:
    """Add a device embedding to an existing profile."""
    fp = get_fingerprint(username, profile_id)
    if not fp:
        raise ValueError(f"Profile {profile_id} not found")

    embedding = _to_list(embedding)

    emb_id = str(uuid.uuid4())
    now = time.time()

    audio_path = None
    if audio is not None:
        audio_path = _save_audio(username, emb_id, audio)

    emb_entry: dict = {
        "id": emb_id,
        "label": label,
        "embedding": embedding,
        "embedding_wespeaker": _to_list(embedding_wespeaker),
        "createdAt": now,
    }
    if audio_path:
        emb_entry["audioPath"] = audio_path

    fp["embeddings"].append(emb_entry)
    _fp_path(username, profile_id).write_text(json.dumps(fp, indent=2), encoding="utf-8")
    return fp


def remove_embedding(username: str, profile_id: str, embedding_id: str) -> dict | None:
    """Remove one embedding from a profile. Deletes profile if last embedding removed."""
    fp = get_fingerprint(username, profile_id)
    if not fp:
        return None

    emb = next((e for e in fp["embeddings"] if e["id"] == embedding_id), None)
    if not emb:
        return None

    if emb.get("audioPath"):
        _delete_audio(username, emb["audioPath"])

    fp["embeddings"] = [e for e in fp["embeddings"] if e["id"] != embedding_id]

    if not fp["embeddings"]:
        delete_fingerprint(username, profile_id)
        return None

    _fp_path(username, profile_id).write_text(json.dumps(fp, indent=2), encoding="utf-8")
    return fp


def delete_fingerprint(username: str, fp_id: str) -> bool:
    """Delete an entire profile and its associated audio files."""
    fp = get_fingerprint(username, fp_id)
    if not fp:
        p = _fp_path(username, fp_id)
        if p.exists():
            p.unlink()
            return True
        return False

    for emb in fp.get("embeddings", []):
        if emb.get("audioPath"):
            _delete_audio(username, emb["audioPath"])

    p = _fp_path(username, fp_id)
    if p.exists():
        p.unlink()

    active = get_active(username)
    if active:
        active_name = active.get("speakerName") or ""
        if active_name.lower() == fp.get("name", "").lower():
            clear_active(username)

    return True


# ── Active speaker selection ────────────────────────────────────────────────


DEFAULT_THRESHOLD = 0.45
DEFAULT_TSE_THRESHOLD = 0.35


def _normalize_active(data: dict) -> dict:
    """Fill in defaults for the optional TSE fields."""
    return {
        "speakerName": data.get("speakerName"),
        "threshold": float(data.get("threshold", DEFAULT_THRESHOLD)),
        "tseEnabled": bool(data.get("tseEnabled", False)),
        "tseThreshold": float(data.get("tseThreshold", DEFAULT_TSE_THRESHOLD)),
    }


def get_active(username: str) -> dict | None:
    """Get active speaker config: ``{speakerName, threshold, tseEnabled, tseThreshold}``,
    or ``None`` if gating is disabled."""
    p = _active_path(username)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        # Legacy v1 format: {fingerprintId, threshold}
        if "fingerprintId" in data and "speakerName" not in data:
            fp = get_fingerprint(username, data["fingerprintId"])
            if fp:
                migrated = _normalize_active(
                    {"speakerName": fp["name"], "threshold": data.get("threshold", DEFAULT_THRESHOLD)}
                )
                p.write_text(json.dumps(migrated, indent=2), encoding="utf-8")
                return migrated
            return None
        if not data.get("speakerName"):
            return None
        return _normalize_active(data)
    except Exception:
        return None


def set_active(
    username: str,
    speaker_name: str,
    threshold: float = DEFAULT_THRESHOLD,
    tse_enabled: bool = False,
    tse_threshold: float = DEFAULT_TSE_THRESHOLD,
) -> dict | None:
    """Set the active speaker for gating by name.

    Returns the persisted config (with ``tseEnabled``/``tseThreshold``), or
    ``None`` if no matching profile exists.
    """
    profile = _find_profile_by_name(username, speaker_name)
    if not profile:
        return None
    config = _normalize_active({
        "speakerName": profile["name"],
        "threshold": float(threshold),
        "tseEnabled": bool(tse_enabled),
        "tseThreshold": float(tse_threshold),
    })
    _active_path(username).write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config


def clear_active(username: str) -> None:
    """Disable speaker gating."""
    p = _active_path(username)
    if p.exists():
        p.unlink()


def get_embeddings_for_speaker(username: str, speaker_name: str) -> list[np.ndarray] | None:
    """Resolve a speaker name to its (SpeechBrain) embedding vectors, or None.

    Kept for backwards compatibility — TSE-aware callers should use
    :func:`get_speaker_entries` to get both embeddings paired together.
    """
    entries = get_speaker_entries(username, speaker_name)
    if not entries:
        return None
    return [e["embedding"] for e in entries]


def get_speaker_entries(username: str, speaker_name: str) -> list[dict] | None:
    """Resolve a speaker name to a list of paired embeddings.

    Each entry: ``{"id": str, "label": str,
    "embedding": np.ndarray (SpeechBrain),
    "embedding_wespeaker": np.ndarray | None (WeSpeaker)}``
    """
    profile = _find_profile_by_name(username, speaker_name)
    if not profile or not profile.get("embeddings"):
        return None
    entries: list[dict] = []
    for e in profile["embeddings"]:
        if "embedding" not in e:
            continue
        ws = e.get("embedding_wespeaker")
        entries.append({
            "id": e.get("id", ""),
            "label": e.get("label", ""),
            "embedding": np.array(e["embedding"], dtype=np.float32),
            "embedding_wespeaker": (
                np.array(ws, dtype=np.float32) if ws is not None else None
            ),
        })
    return entries or None


def get_active_gate(username: str) -> dict | None:
    """Get the active gate data for STT, or None.

    Returns ``{entries: [{embedding, embedding_wespeaker}, ...],
    threshold: float, tseEnabled: bool, tseThreshold: float}``.
    """
    active = get_active(username)
    if not active:
        return None
    speaker_name = active.get("speakerName")
    if not speaker_name:
        return None
    entries = get_speaker_entries(username, speaker_name)
    if not entries:
        return None
    return {
        "entries": entries,
        "threshold": active["threshold"],
        "tseEnabled": active["tseEnabled"],
        "tseThreshold": active["tseThreshold"],
    }
