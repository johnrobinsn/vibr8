"""Queue-based WebRTC audio track for streaming Opus frames.

Follows the proven pattern from neortc2's TTSTrack (agent.py lines 119-181).
Opus frames are queued and delivered via ``recv()`` with proper PTS timing
and real-time pacing.
"""

from __future__ import annotations

import asyncio
import logging
from fractions import Fraction
from time import time

import numpy as np
from aiortc.mediastreams import MediaStreamTrack
from av import AudioFrame
from av.audio.codeccontext import AudioCodecContext
from av.packet import Packet

logger = logging.getLogger(__name__)

# Opus DTX silence frame (3 bytes).
_SILENCE_BYTES = bytes.fromhex("f8fffe")

# 20ms at 48kHz = 960 samples per frame.
_SAMPLES_PER_FRAME = 960
_FRAME_DURATION = 0.02  # 20ms
_TIME_BASE = 48000
_TIME_BASE_FRACTION = Fraction(1, _TIME_BASE)

# ── Thinking-tone generator ──────────────────────────────────────────────────

_thinking_frames_cache: list[bytes] = []


def _generate_thinking_frames() -> list[bytes]:
    """Pre-encode a ~2s looping thinking tone as Opus frames.

    Produces a very quiet, warm sine with gentle amplitude modulation
    ("breathing") so the user knows the agent is working.
    """
    if _thinking_frames_cache:
        return _thinking_frames_cache

    sample_rate = 48000
    duration = 2.0  # seconds — loop length
    num_samples = int(sample_rate * duration)
    t = np.arange(num_samples, dtype=np.float32) / sample_rate

    # Gentle tone with slow pulsing envelope
    freq = 280.0        # warm frequency between C4 and D4
    mod_freq = 0.5      # breathing rate (Hz)
    amplitude = 0.025   # very quiet (~2.5% of full scale)

    envelope = 0.5 * (1.0 + np.sin(2 * np.pi * mod_freq * t))
    signal = amplitude * np.sin(2 * np.pi * freq * t) * envelope
    pcm = (signal * 32767).astype(np.int16)

    # Encode to Opus using PyAV
    codec = AudioCodecContext.create("libopus", "w")
    codec.sample_rate = sample_rate
    codec.layout = "mono"
    codec.format = "s16"
    codec.open()

    frames: list[bytes] = []
    for i in range(0, len(pcm), _SAMPLES_PER_FRAME):
        chunk = pcm[i : i + _SAMPLES_PER_FRAME]
        if len(chunk) < _SAMPLES_PER_FRAME:
            chunk = np.pad(chunk, (0, _SAMPLES_PER_FRAME - len(chunk)))
        af = AudioFrame.from_ndarray(
            chunk.reshape(1, -1), format="s16", layout="mono"
        )
        af.sample_rate = sample_rate
        af.pts = i
        for pkt in codec.encode(af):
            frames.append(bytes(pkt))

    # Flush encoder
    for pkt in codec.encode(None):
        frames.append(bytes(pkt))

    logger.info("[audio_track] Generated %d thinking-tone Opus frames", len(frames))
    _thinking_frames_cache.extend(frames)
    return _thinking_frames_cache


# ── Track ─────────────────────────────────────────────────────────────────────


class QueuedAudioTrack(MediaStreamTrack):
    """A MediaStreamTrack that serves Opus frames from a queue.

    Push raw Opus frame bytes via :meth:`push_opus_frame`. The WebRTC
    transport calls :meth:`recv` at ~20ms intervals; if the queue is
    empty, a silence frame is returned (or a thinking tone if enabled).
    """

    kind = "audio"

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self._session_id = session_id
        self._packetq: list[bytes] = []
        self._next_pts: int = 0
        self._stream_time: float | None = None
        self._thinking: bool = False
        self._thinking_idx: int = 0

    def push_opus_frame(self, frame: bytes) -> None:
        """Enqueue a raw Opus frame to be sent to the browser."""
        self._packetq.insert(0, frame)

    def clear_audio(self) -> None:
        """Discard all queued audio."""
        self._packetq.clear()

    def set_thinking(self, thinking: bool) -> None:
        """Enable or disable the thinking-tone loop."""
        self._thinking = thinking
        if thinking:
            self._thinking_idx = 0

    def _get_silence_packet(self) -> tuple[Packet, float]:
        pkt = Packet(_SILENCE_BYTES)
        pkt.pts = self._next_pts
        pkt.dts = self._next_pts
        pkt.time_base = _TIME_BASE_FRACTION
        self._next_pts += _SAMPLES_PER_FRAME
        return pkt, _FRAME_DURATION

    def _get_thinking_packet(self) -> tuple[Packet, float] | None:
        frames = _generate_thinking_frames()
        if not frames:
            return None
        chunk = frames[self._thinking_idx % len(frames)]
        self._thinking_idx += 1
        pkt = Packet(chunk)
        pkt.pts = self._next_pts
        pkt.dts = self._next_pts
        pkt.time_base = _TIME_BASE_FRACTION
        self._next_pts += _SAMPLES_PER_FRAME
        return pkt, _FRAME_DURATION

    def _get_audio_packet(self) -> tuple[Packet, float]:
        if self._packetq:
            try:
                chunk = self._packetq.pop()
                pkt = Packet(chunk)
                pkt.pts = self._next_pts
                pkt.dts = self._next_pts
                pkt.time_base = _TIME_BASE_FRACTION
                self._next_pts += _SAMPLES_PER_FRAME
                return pkt, _FRAME_DURATION
            except Exception:
                pass  # Fall through.
        # No TTS audio queued — play thinking tone if active, else silence.
        if self._thinking:
            result = self._get_thinking_packet()
            if result:
                return result
        return self._get_silence_packet()

    async def recv(self) -> Packet:
        """Return the next Opus packet, paced to real-time."""
        try:
            packet, duration = self._get_audio_packet()

            if self._stream_time is None:
                self._stream_time = time()

            wait = self._stream_time - time()
            if wait > 0:
                await asyncio.sleep(wait)

            self._stream_time += duration
            return packet
        except Exception as e:
            logger.error("[audio_track] Error in recv: %s", e)
            raise
