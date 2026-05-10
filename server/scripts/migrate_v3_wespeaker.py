"""Backfill ``embedding_wespeaker`` on existing voice fingerprints.

For each enrollment entry that has an ``audioPath`` but no
``embedding_wespeaker`` (or has it as ``None``), re-encode the stored WAV
through the WeSpeaker ECAPA model and write the vector back into the
profile JSON. Schema is bumped to v3 if not already.

Entries without an audio file are left untouched — they predate audio
capture and must be re-enrolled to participate in TSE.

Usage:
    uv run python -m server.scripts.migrate_v3_wespeaker [--user USERNAME] [--dry-run]
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import sys
import wave
from pathlib import Path

import numpy as np

from server import speaker_fingerprints as sf

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("migrate_v3")


def _load_wav_16k_mono(path: Path) -> np.ndarray:
    """Load a 16 kHz mono int16 WAV (the format produced by ``_save_audio``)."""
    with wave.open(str(path), "rb") as wf:
        if wf.getframerate() != 16000:
            raise ValueError(f"{path}: expected 16 kHz, got {wf.getframerate()}")
        if wf.getnchannels() != 1:
            raise ValueError(f"{path}: expected mono, got {wf.getnchannels()} channels")
        if wf.getsampwidth() != 2:
            raise ValueError(f"{path}: expected int16, got {wf.getsampwidth() * 8}-bit")
        raw = wf.readframes(wf.getnframes())
    pcm = np.frombuffer(raw, dtype=np.int16)
    return pcm.astype(np.float32) / np.iinfo(np.int16).max


def _iter_user_dirs(only_user: str | None):
    if not sf.FINGERPRINTS_DIR.exists():
        return
    for d in sf.FINGERPRINTS_DIR.iterdir():
        if not d.is_dir():
            continue
        if only_user and d.name != only_user:
            continue
        yield d


def migrate_user(user_dir: Path, *, dry_run: bool) -> tuple[int, int, int]:
    """Returns (encoded, skipped_no_audio, already_done)."""
    from server.wespeaker_model import embed

    encoded = skipped = already = 0
    for fp_path in sorted(user_dir.iterdir()):
        if fp_path.suffix != ".json" or fp_path.name == "active.json":
            continue
        try:
            data = json.loads(fp_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to parse %s: %s", fp_path, exc)
            continue

        # Apply lazy schema migration (v1→v2→v3). This guarantees every entry
        # has the ``embedding_wespeaker`` slot before we start encoding.
        data = sf._migrate_to_current(data, user_dir.name, data["id"])
        changed = False

        for emb in data.get("embeddings", []):
            if emb.get("embedding_wespeaker") is not None:
                already += 1
                continue
            audio_path = emb.get("audioPath")
            if not audio_path:
                logger.info(
                    "  [skip] %s/%s — no audioPath (re-enroll required for TSE)",
                    data.get("name"), emb.get("label"),
                )
                skipped += 1
                continue

            wav_file = user_dir / audio_path
            if not wav_file.exists():
                logger.warning(
                    "  [skip] %s/%s — audio file missing: %s",
                    data.get("name"), emb.get("label"), wav_file,
                )
                skipped += 1
                continue

            try:
                wav = _load_wav_16k_mono(wav_file)
            except Exception as exc:
                logger.warning(
                    "  [skip] %s/%s — failed to load %s: %s",
                    data.get("name"), emb.get("label"), wav_file, exc,
                )
                skipped += 1
                continue

            logger.info(
                "  [encode] %s/%s (%.2fs) → wespeaker embedding",
                data.get("name"), emb.get("label"), len(wav) / 16000.0,
            )
            ws_emb = embed(wav)
            if not dry_run:
                emb["embedding_wespeaker"] = ws_emb.tolist()
                changed = True
            encoded += 1

        if changed and not dry_run:
            fp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            logger.info("  wrote %s", fp_path)

    return encoded, skipped, already


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--user", help="Only migrate this username")
    ap.add_argument("--dry-run", action="store_true", help="Don't write changes")
    args = ap.parse_args()

    logger.info("FINGERPRINTS_DIR = %s", sf.FINGERPRINTS_DIR)

    total_encoded = total_skipped = total_already = 0
    for user_dir in _iter_user_dirs(args.user):
        logger.info("user: %s", user_dir.name)
        e, s, a = migrate_user(user_dir, dry_run=args.dry_run)
        total_encoded += e
        total_skipped += s
        total_already += a

    logger.info(
        "done: encoded=%d skipped_no_audio=%d already_done=%d (dry_run=%s)",
        total_encoded, total_skipped, total_already, args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
