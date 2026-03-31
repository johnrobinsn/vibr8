"""Async voice logger — persists audio segments and full recordings to disk.

All disk I/O runs via ``asyncio.to_thread()`` to avoid blocking the event loop
or the STT ThreadWorker.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
import wave
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("VIBR8_DATA_DIR", str(Path.home() / ".vibr8" / "data")))
SEGMENT_SAMPLE_RATE = 16000  # segments are 16kHz mono (for Whisper)
RECORDING_SAMPLE_RATE = 48000  # recordings stay at WebRTC native rate
RECORDING_CHANNELS = 2  # stereo — preserve original WebRTC audio
SEGMENT_CHANNELS = 1
SAMPLE_WIDTH = 2  # int16

FLUSH_INTERVAL_BYTES = 480_000  # ~2.5s of 48kHz stereo int16


class VoiceLogger:
    """One per WebRTC session — records audio and logs segments."""

    def __init__(self, user: str, session_id: str) -> None:
        self._user = user
        self._session_id = session_id
        self._base = DATA_DIR / "voice" / "logs" / user
        self._segments_dir = self._base / "segments"
        self._recordings_dir = self._base / "recordings"
        self._seg_params_dir = self._base / "seg_params"
        self._index_path = self._base / "index.jsonl"

        # Recording state
        self._recording_id: str | None = None
        self._recording_buf: list[np.ndarray] = []
        self._recording_buf_bytes: int = 0
        self._wav_writer: wave.Wave_write | None = None
        self._recording_started: float = 0.0

    # ── Recording lifecycle ──────────────────────────────────────────────

    async def start_recording(self) -> str:
        """Open a new full-session recording WAV file."""
        self._recording_id = str(uuid.uuid4())
        self._recording_started = time.time()

        def _open():
            self._segments_dir.mkdir(parents=True, exist_ok=True)
            self._recordings_dir.mkdir(parents=True, exist_ok=True)
            path = self._recordings_dir / f"{self._recording_id}.wav"
            wf = wave.open(str(path), "wb")
            wf.setnchannels(RECORDING_CHANNELS)
            wf.setsampwidth(SAMPLE_WIDTH)
            wf.setframerate(RECORDING_SAMPLE_RATE)
            self._wav_writer = wf
            # Write recording metadata
            meta = {
                "id": self._recording_id,
                "sessionId": self._session_id,
                "duration": 0.0,
                "startedAt": self._recording_started,
                "endedAt": None,
            }
            meta_path = self._recordings_dir / f"{self._recording_id}.json"
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        await asyncio.to_thread(_open)
        logger.info("[voice-log] Started recording %s for session %s",
                     self._recording_id, self._session_id)
        return self._recording_id

    async def log_chunk(self, audio_16k: np.ndarray) -> None:
        """Buffer audio and flush to WAV periodically."""
        self._recording_buf.append(audio_16k)
        self._recording_buf_bytes += audio_16k.nbytes

        if self._recording_buf_bytes >= FLUSH_INTERVAL_BYTES:
            await self._flush_buffer()

    async def _flush_buffer(self) -> None:
        if not self._recording_buf or not self._wav_writer:
            return
        combined = np.concatenate(self._recording_buf)
        self._recording_buf.clear()
        self._recording_buf_bytes = 0
        wf = self._wav_writer

        def _write():
            wf.writeframes(combined.tobytes())

        await asyncio.to_thread(_write)

    def _ensure_seg_params(self, params_dict: dict, profile_id: str | None, profile_name: str | None) -> str:
        """Compute content-addressed ID for params and write file if new. Returns ID."""
        # Hash only the param values (sorted, deterministic)
        canonical = json.dumps(params_dict, sort_keys=True, separators=(",", ":"))
        hash_id = hashlib.sha256(canonical.encode()).hexdigest()[:12]

        self._seg_params_dir.mkdir(parents=True, exist_ok=True)
        path = self._seg_params_dir / f"{hash_id}.json"
        if not path.exists():
            record = {
                "id": hash_id,
                "profileId": profile_id,
                "profileName": profile_name,
                "params": params_dict,
                "createdAt": time.time(),
            }
            path.write_text(json.dumps(record, indent=2), encoding="utf-8")
            logger.debug("[voice-log] New seg_params %s", hash_id)
        return hash_id

    async def log_segment(self, audio_16k: np.ndarray, transcript_data: dict) -> str:
        """Write segment WAV and append to JSONL index. Returns segment_id."""
        segment_id = str(uuid.uuid4())

        entry = {
            "id": segment_id,
            "sessionId": self._session_id,
            "transcript": transcript_data.get("transcript", ""),
            "timeBegin": transcript_data.get("timeBegin", 0.0),
            "timeEnd": transcript_data.get("timeEnd", 0.0),
            "recordingId": self._recording_id,
            "profileId": transcript_data.get("profileId"),
            "createdAt": time.time(),
        }

        # Content-addressed seg_params if params are present
        params_dict = transcript_data.get("params")
        if params_dict:
            seg_params_id = self._ensure_seg_params(
                params_dict,
                transcript_data.get("profileId"),
                transcript_data.get("profileName"),
            )
            entry["segParamsId"] = seg_params_id

        eou_prob = transcript_data.get("eouProb")
        if eou_prob is not None:
            entry["eouProb"] = eou_prob

        def _write_segment():
            self._segments_dir.mkdir(parents=True, exist_ok=True)
            # Write segment WAV
            seg_path = self._segments_dir / f"{segment_id}.wav"
            with wave.open(str(seg_path), "wb") as wf:
                wf.setnchannels(SEGMENT_CHANNELS)
                wf.setsampwidth(SAMPLE_WIDTH)
                wf.setframerate(SEGMENT_SAMPLE_RATE)
                wf.writeframes(audio_16k.tobytes())
            # Append to index
            self._base.mkdir(parents=True, exist_ok=True)
            with open(self._index_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

        await asyncio.to_thread(_write_segment)
        logger.debug("[voice-log] Segment %s: %s", segment_id, entry["transcript"][:50])
        return segment_id

    async def stop_recording(self) -> None:
        """Flush buffer, close WAV, update recording metadata."""
        await self._flush_buffer()
        recording_id = self._recording_id
        wf = self._wav_writer
        started = self._recording_started

        self._recording_id = None
        self._wav_writer = None

        if not wf or not recording_id:
            return

        def _close():
            wf.close()
            # Update metadata with final duration
            ended = time.time()
            meta_path = self._recordings_dir / f"{recording_id}.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                meta["duration"] = ended - started
                meta["endedAt"] = ended
                meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        await asyncio.to_thread(_close)
        logger.info("[voice-log] Stopped recording %s", recording_id)


# ── Query functions (for API endpoints) ──────────────────────────────────────


def list_segments(
    username: str,
    query: str = "",
    offset: int = 0,
    limit: int = 50,
) -> list[dict]:
    """List segments from JSONL index, newest first, with optional transcript search."""
    index_path = DATA_DIR / "voice" / "logs" / username / "index.jsonl"
    if not index_path.exists():
        return []

    segments: list[dict] = []
    with open(index_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                seg = json.loads(line)
                if query and query.lower() not in seg.get("transcript", "").lower():
                    continue
                segments.append(seg)
            except Exception:
                continue

    # Newest first
    segments.sort(key=lambda s: s.get("createdAt", 0), reverse=True)
    return segments[offset:offset + limit]


def get_segment_audio_path(username: str, segment_id: str) -> Path | None:
    """Return the path to a segment WAV file, or None if not found."""
    p = DATA_DIR / "voice" / "logs" / username / "segments" / f"{segment_id}.wav"
    return p if p.exists() else None


def get_recording_audio_path(username: str, recording_id: str) -> Path | None:
    """Return the path to a recording WAV file, or None if not found."""
    p = DATA_DIR / "voice" / "logs" / username / "recordings" / f"{recording_id}.wav"
    return p if p.exists() else None


def delete_segment(username: str, segment_id: str) -> bool:
    """Delete a segment's audio and remove from index."""
    base = DATA_DIR / "voice" / "logs" / username

    # Delete audio file
    audio_path = base / "segments" / f"{segment_id}.wav"
    if audio_path.exists():
        audio_path.unlink()

    # Remove from index
    index_path = base / "index.jsonl"
    if not index_path.exists():
        return False

    lines = index_path.read_text(encoding="utf-8").splitlines()
    new_lines = []
    found = False
    for line in lines:
        try:
            seg = json.loads(line)
            if seg.get("id") == segment_id:
                found = True
                continue
        except Exception:
            pass
        new_lines.append(line)

    if found:
        index_path.write_text("\n".join(new_lines) + "\n" if new_lines else "", encoding="utf-8")
    return found


def list_recordings(username: str) -> list[dict]:
    """List all recording metadata, newest first."""
    recordings_dir = DATA_DIR / "voice" / "logs" / username / "recordings"
    if not recordings_dir.exists():
        return []
    recordings = []
    for f in recordings_dir.iterdir():
        if f.suffix == ".json":
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
                if not rec.get("id"):
                    continue  # skip corrupted entries with null/missing id
                recordings.append(rec)
            except Exception:
                continue
    recordings.sort(key=lambda r: r.get("startedAt", 0), reverse=True)
    return recordings


def get_seg_params(username: str, seg_params_id: str) -> dict | None:
    """Read a single seg_params record by ID."""
    p = DATA_DIR / "voice" / "logs" / username / "seg_params" / f"{seg_params_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_seg_params(username: str) -> list[dict]:
    """List all seg_params records for a user."""
    d = DATA_DIR / "voice" / "logs" / username / "seg_params"
    if not d.exists():
        return []
    results: list[dict] = []
    for f in d.iterdir():
        if f.suffix == ".json":
            try:
                results.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                continue
    results.sort(key=lambda r: r.get("createdAt", 0), reverse=True)
    return results


def clear_all_logs(username: str) -> bool:
    """Delete all segments, recordings, and index for a user."""
    import shutil
    base = DATA_DIR / "voice" / "logs" / username
    if not base.exists():
        return False
    shutil.rmtree(base)
    return True
