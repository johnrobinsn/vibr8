"""Offline CLI replay tool — run recorded audio through the STT pipeline.

Usage examples:
    # Replay a single segment's time range from its parent recording
    uv run python -m server.voice_replay --user johnrobinsn --segment 10f2aa4e

    # Replay a range between two segments
    uv run python -m server.voice_replay --user johnrobinsn \
        --from-segment 1e78386a --to-segment 10f2aa4e

    # Replay explicit time range from a recording
    uv run python -m server.voice_replay --user johnrobinsn --recording e6e053bc \
        --from 22010 --to 22020

    # Use original seg_params (reproduce exactly what happened)
    uv run python -m server.voice_replay --user johnrobinsn --segment 10f2aa4e \
        --original-params

    # Apply a different voice profile
    uv run python -m server.voice_replay --user johnrobinsn --segment 10f2aa4e \
        --profile "Sensitive"

    # Override individual params
    uv run python -m server.voice_replay --user johnrobinsn --segment 10f2aa4e \
        --vad-threshold -20 --silero-threshold 0.6

    # List recordings and segments
    uv run python -m server.voice_replay --user johnrobinsn --list
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import wave
from pathlib import Path

import numpy as np

from server.voice_logger import (
    DATA_DIR,
    RECORDING_SAMPLE_RATE,
    RECORDING_CHANNELS,
    list_recordings,
    list_segments,
    get_seg_params,
)
from server.stt import STT, STTParams

logger = logging.getLogger(__name__)

# Lead-in/trail seconds when extracting from recording
LEAD_IN_SECONDS = 5.0
TRAIL_SECONDS = 2.0

# Chunk size matching real-time pipeline (160ms stereo 48kHz)
CHUNK_DURATION_MS = 160
CHUNK_SAMPLES = int(RECORDING_SAMPLE_RATE * CHUNK_DURATION_MS / 1000)  # 7680 samples per channel


def _base_dir(username: str) -> Path:
    return DATA_DIR / "voice" / "logs" / username


def _find_segment(username: str, prefix: str) -> dict | None:
    """Find a segment by ID prefix in the JSONL index."""
    index_path = _base_dir(username) / "index.jsonl"
    if not index_path.exists():
        return None
    with open(index_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                seg = json.loads(line)
                if seg.get("id", "").startswith(prefix):
                    return seg
            except Exception:
                continue
    return None


def _read_recording_wav(username: str, recording_id: str) -> tuple[np.ndarray, int, int] | None:
    """Read a recording WAV file. Returns (samples, sample_rate, num_channels) or None."""
    # Try prefix match for recording ID
    recordings_dir = _base_dir(username) / "recordings"
    if not recordings_dir.exists():
        return None

    wav_path = None
    for f in recordings_dir.iterdir():
        if f.suffix == ".wav" and f.stem.startswith(recording_id):
            wav_path = f
            break

    if not wav_path:
        return None

    with wave.open(str(wav_path), "rb") as wf:
        sr = wf.getframerate()
        nch = wf.getnchannels()
        nframes = wf.getnframes()
        raw = wf.readframes(nframes)
        samples = np.frombuffer(raw, dtype=np.int16)
    return samples, sr, nch


def _extract_time_range(samples: np.ndarray, sr: int, nch: int,
                        t_start: float, t_end: float) -> np.ndarray:
    """Extract a time range from interleaved audio samples."""
    frame_start = max(0, int(t_start * sr))
    frame_end = min(len(samples) // nch, int(t_end * sr))
    sample_start = frame_start * nch
    sample_end = frame_end * nch
    return samples[sample_start:sample_end]


def _resolve_params(args, segment: dict | None) -> STTParams:
    """Resolve STTParams from CLI args, profile, or segment's original params."""
    base = STTParams()

    # Priority 3: --original-params from segment's segParamsId
    if args.original_params and segment:
        sp_id = segment.get("segParamsId")
        if sp_id:
            sp = get_seg_params(args.user, sp_id)
            if sp and "params" in sp:
                p = sp["params"]
                base = STTParams(
                    mic_gain=p.get("mic_gain", base.mic_gain),
                    vad_threshold_db=p.get("vad_threshold_db", base.vad_threshold_db),
                    silero_vad_threshold=p.get("silero_vad_threshold", base.silero_vad_threshold),
                    eou_threshold=p.get("eou_threshold", base.eou_threshold),
                    eou_max_retries=p.get("eou_max_retries", base.eou_max_retries),
                    min_segment_duration=p.get("min_segment_duration", base.min_segment_duration),
                )
                print(f"Using original seg_params: {sp_id}")
            else:
                print(f"Warning: seg_params {sp_id} not found, using defaults")

    # Priority 2: --profile
    if args.profile:
        from server.voice_profiles import list_profiles, get_stt_params
        profiles = list_profiles(args.user)
        match = None
        for prof in profiles:
            if prof["name"].lower() == args.profile.lower():
                match = prof
                break
        if match:
            base = get_stt_params(args.user, match["id"])
            print(f"Using profile: {match['name']} ({match['id'][:8]})")
        else:
            print(f"Warning: profile '{args.profile}' not found, using defaults")

    # Priority 1: explicit CLI overrides
    if args.mic_gain is not None:
        base.mic_gain = args.mic_gain
    if args.vad_threshold is not None:
        base.vad_threshold_db = args.vad_threshold
    if args.silero_threshold is not None:
        base.silero_vad_threshold = args.silero_threshold
    if args.eou_threshold is not None:
        base.eou_threshold = args.eou_threshold
    if args.eou_max_retries is not None:
        base.eou_max_retries = args.eou_max_retries
    if args.min_segment_duration is not None:
        base.min_segment_duration = args.min_segment_duration

    # Always enable verbose for replay
    base.verbose = True
    return base


def do_list(args):
    """List recordings and their segments."""
    recs = list_recordings(args.user)
    if not recs:
        print(f"No recordings found for user '{args.user}'")
        return

    all_segs = list_segments(args.user, limit=10000)
    seg_by_rec: dict[str, list[dict]] = {}
    for seg in all_segs:
        rid = seg.get("recordingId")
        if rid:
            seg_by_rec.setdefault(rid, []).append(seg)

    for rec in recs:
        rid = rec["id"]
        started = rec.get("startedAt", 0)
        duration = rec.get("duration", 0)
        ended = rec.get("endedAt")
        status = f"{duration:.0f}s" if ended else "in-progress"

        from datetime import datetime
        dt = datetime.fromtimestamp(started)
        print(f"\n  rec: {rid[:8]}  {dt:%Y-%m-%d %H:%M}  {status}")
        print(f"       recordings/{rid}.wav")

        segs = seg_by_rec.get(rid, [])
        segs.sort(key=lambda s: s.get("timeBegin", 0))
        for seg in segs:
            sid = seg["id"]
            t = seg.get("timeBegin", 0)
            dur = seg.get("timeEnd", 0) - t
            text = seg.get("transcript", "")[:60]
            sp_id = seg.get("segParamsId", "")
            eou = seg.get("eouProb")
            eou_str = f"  eou={eou:.2f}" if eou is not None else ""
            sp_str = f"  params={sp_id}" if sp_id else ""
            print(f"    [{t:>7.1f}s] {sid[:8]}  \"{text}\"  {dur:.1f}s{eou_str}{sp_str}")


def do_replay(args):
    """Run audio through the STT pipeline with verbose diagnostics."""
    # Resolve segment(s)
    segment = None
    from_seg = None
    to_seg = None
    recording_id = None
    t_start = None
    t_end = None

    if args.segment:
        segment = _find_segment(args.user, args.segment)
        if not segment:
            print(f"Error: segment '{args.segment}' not found")
            sys.exit(1)
        recording_id = segment.get("recordingId")
        if not recording_id:
            print(f"Error: segment '{args.segment}' has no recordingId")
            sys.exit(1)
        t_start = max(0, segment["timeBegin"] - LEAD_IN_SECONDS)
        t_end = segment["timeEnd"] + TRAIL_SECONDS
        print(f"Segment: {segment['id'][:8]}  \"{segment.get('transcript', '')[:60]}\"")
        print(f"Time: {segment['timeBegin']:.1f}s — {segment['timeEnd']:.1f}s  "
              f"(extracting {t_start:.1f}s — {t_end:.1f}s with lead-in/trail)")

    elif args.from_segment and args.to_segment:
        from_seg = _find_segment(args.user, args.from_segment)
        to_seg = _find_segment(args.user, args.to_segment)
        if not from_seg:
            print(f"Error: from-segment '{args.from_segment}' not found")
            sys.exit(1)
        if not to_seg:
            print(f"Error: to-segment '{args.to_segment}' not found")
            sys.exit(1)
        recording_id = from_seg.get("recordingId") or to_seg.get("recordingId")
        if not recording_id:
            print("Error: neither segment has a recordingId")
            sys.exit(1)
        t_start = max(0, min(from_seg["timeBegin"], to_seg["timeBegin"]) - LEAD_IN_SECONDS)
        t_end = max(from_seg["timeEnd"], to_seg["timeEnd"]) + TRAIL_SECONDS
        print(f"Range: {from_seg['id'][:8]} — {to_seg['id'][:8]}")
        print(f"Time: {t_start:.1f}s — {t_end:.1f}s")

    elif args.recording:
        recording_id = args.recording
        t_start = args.time_from or 0
        t_end = args.time_to
        if t_end is None:
            print("Error: --to is required with --recording")
            sys.exit(1)
        print(f"Recording: {recording_id}  time: {t_start:.1f}s — {t_end:.1f}s")

    else:
        print("Error: provide --segment, --from-segment/--to-segment, or --recording with --from/--to")
        sys.exit(1)

    # Read recording WAV
    result = _read_recording_wav(args.user, recording_id)
    if not result:
        print(f"Error: recording WAV not found for '{recording_id}'")
        sys.exit(1)

    samples, sr, nch = result
    total_duration = len(samples) / (sr * nch)
    print(f"Recording: {sr}Hz {nch}ch  duration={total_duration:.1f}s")

    # Extract time range
    chunk = _extract_time_range(samples, sr, nch, t_start, t_end)
    chunk_duration = len(chunk) / (sr * nch)
    print(f"Extracted: {chunk_duration:.1f}s ({len(chunk)} samples)")

    # Resolve params
    params = _resolve_params(args, segment or from_seg)
    print(f"\nParams: gain={params.mic_gain}  vad={params.vad_threshold_db}dB  "
          f"silero={params.silero_vad_threshold}  eou={params.eou_threshold}  "
          f"retries={params.eou_max_retries}  min_dur={params.min_segment_duration}s")

    # Create STT instance (synchronous, single-threaded)
    print("\nLoading models...")
    stt = STT(sample_rate=sr, num_channels=nch, params=params)

    # Collect events
    events: list[tuple[str, dict]] = []

    def listener(stt_instance, event_type, data):
        if event_type == "voice_level":
            return  # too noisy
        events.append((event_type, data if isinstance(data, dict) else {}))
        if event_type == "final_transcript":
            text = data.get("transcript", "") if data else ""
            eou = data.get("eouProb", "?") if data else "?"
            print(f"\n>>> FINAL TRANSCRIPT: \"{text}\"  eou={eou}")
        elif event_type == "voice_was_detected":
            print("  [voice detected]")
        elif event_type == "voice_not_detected":
            pass  # verbose logging covers this

    stt.add_listener(listener)

    # Feed chunks (160ms at recording sample rate)
    chunk_size = CHUNK_SAMPLES * nch  # interleaved samples per chunk
    num_chunks = len(chunk) // chunk_size
    print(f"\nFeeding {num_chunks} chunks ({CHUNK_DURATION_MS}ms each)...\n")

    for i in range(num_chunks):
        start = i * chunk_size
        end = start + chunk_size
        buf = chunk[start:end].reshape(1, -1)  # shape (1, N) for mono interface
        stt.process_buffer(buf)

    # Flush remaining
    stt.flush()

    # Summary
    transcripts = [(e, d) for e, d in events if e == "final_transcript"]
    print(f"\n{'='*60}")
    print(f"Replay complete. {len(transcripts)} segment(s) produced:")
    for _, d in transcripts:
        text = d.get("transcript", "")
        t0 = d.get("timeBegin", 0)
        t1 = d.get("timeEnd", 0)
        eou = d.get("eouProb", "?")
        print(f"  [{t0:.1f}s — {t1:.1f}s] \"{text}\"  eou={eou}")


def main():
    parser = argparse.ArgumentParser(
        description="Replay recorded audio through the STT pipeline with diagnostics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--user", required=True, help="Username (matches voice log directory)")

    # Mode: list
    parser.add_argument("--list", action="store_true", help="List recordings and segments")

    # Mode: replay by segment
    parser.add_argument("--segment", help="Segment ID (or prefix) to replay")
    parser.add_argument("--from-segment", help="Start of segment range")
    parser.add_argument("--to-segment", help="End of segment range")

    # Mode: replay by recording + time
    parser.add_argument("--recording", help="Recording ID (or prefix)")
    parser.add_argument("--from", dest="time_from", type=float, help="Start time in seconds")
    parser.add_argument("--to", dest="time_to", type=float, help="End time in seconds")

    # Param resolution
    parser.add_argument("--original-params", action="store_true",
                        help="Use the segment's original seg_params")
    parser.add_argument("--profile", help="Voice profile name to use")

    # Individual param overrides
    parser.add_argument("--mic-gain", type=float)
    parser.add_argument("--vad-threshold", type=float, help="RMS dB threshold (e.g. -30)")
    parser.add_argument("--silero-threshold", type=float, help="Silero VAD threshold (e.g. 0.4)")
    parser.add_argument("--eou-threshold", type=float, help="EOU probability threshold (e.g. 0.15)")
    parser.add_argument("--eou-max-retries", type=int)
    parser.add_argument("--min-segment-duration", type=float)

    args = parser.parse_args()

    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
    )

    if args.list:
        do_list(args)
    else:
        do_replay(args)


if __name__ == "__main__":
    main()
