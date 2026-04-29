"""Text-to-speech via Kokoro (local neural TTS) with Opus output.

Generates speech locally using the Kokoro model, resamples from 24kHz
to 48kHz, encodes to Opus, and delivers frames via callback.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable, Optional

import numpy as np
from av import AudioFrame
from av.audio.codeccontext import AudioCodecContext
from scipy.signal import resample_poly

logger = logging.getLogger(__name__)

_OPUS_RATE = 48000
_SAMPLES_PER_FRAME = 960  # 20ms at 48kHz
_KOKORO_RATE = 24000

_pipeline = None


def _ensure_pipeline() -> None:
    global _pipeline
    if _pipeline is not None:
        return
    from kokoro import KPipeline
    logger.info("[tts-kokoro] Loading Kokoro pipeline...")
    _pipeline = KPipeline(lang_code="a")
    logger.info("[tts-kokoro] Kokoro pipeline ready")


def _create_opus_codec() -> AudioCodecContext:
    codec = AudioCodecContext.create("libopus", "w")
    codec.sample_rate = _OPUS_RATE
    codec.layout = "mono"
    codec.format = "s16"
    codec.open()
    return codec


def _encode_pcm(pcm_int16: np.ndarray, codec: AudioCodecContext, pts: int) -> tuple[list[bytes], int]:
    """Encode complete 960-sample frames, return (opus_frames, new_pts)."""
    frames: list[bytes] = []
    for i in range(0, len(pcm_int16), _SAMPLES_PER_FRAME):
        chunk = pcm_int16[i : i + _SAMPLES_PER_FRAME]
        if len(chunk) < _SAMPLES_PER_FRAME:
            break  # residual — caller handles it
        af = AudioFrame.from_ndarray(
            chunk.reshape(1, -1), format="s16", layout="mono"
        )
        af.sample_rate = _OPUS_RATE
        af.pts = pts
        pts += _SAMPLES_PER_FRAME
        for pkt in codec.encode(af):
            frames.append(bytes(pkt))
    return frames, pts


def _generate_and_encode(gen, codec, residual: np.ndarray | None, pts: int):
    """Get next sentence from Kokoro, resample, encode. Single executor call."""
    try:
        _gs, _ps, audio = next(gen)
    except StopIteration:
        return None

    if audio is None or len(audio) == 0:
        return ([], residual, pts)

    resampled = resample_poly(audio, 2, 1).astype(np.float32)
    pcm_int16 = (np.clip(resampled, -1.0, 1.0) * 32767).astype(np.int16)

    if residual is not None and len(residual) > 0:
        pcm_int16 = np.concatenate([residual, pcm_int16])

    frames, pts = _encode_pcm(pcm_int16, codec, pts)

    n_encoded = (len(pcm_int16) // _SAMPLES_PER_FRAME) * _SAMPLES_PER_FRAME
    new_residual = pcm_int16[n_encoded:] if n_encoded < len(pcm_int16) else None

    return (frames, new_residual, pts)


def _flush_codec(codec: AudioCodecContext, residual: np.ndarray | None, pts: int) -> list[bytes]:
    """Encode any residual samples (zero-padded) and flush the codec."""
    frames: list[bytes] = []
    if residual is not None and len(residual) > 0:
        padded = np.pad(residual, (0, _SAMPLES_PER_FRAME - len(residual)))
        af = AudioFrame.from_ndarray(
            padded.reshape(1, -1), format="s16", layout="mono"
        )
        af.sample_rate = _OPUS_RATE
        af.pts = pts
        for pkt in codec.encode(af):
            frames.append(bytes(pkt))
    for pkt in codec.encode(None):
        frames.append(bytes(pkt))
    return frames


class TTS_Kokoro:
    """Stream text-to-speech from Kokoro (local) as Opus frames.

    Each Opus frame is delivered to *opus_frame_handler(frame_bytes)*
    as soon as the sentence chunk is processed.

    Call :meth:`cancel` to abort an in-progress synthesis (e.g. on barge-in).
    """

    def __init__(self, opus_frame_handler: Optional[Callable[[bytes], None]] = None) -> None:
        self._opus_frame_handler = opus_frame_handler
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    async def say(self, text: str) -> None:
        if not text.strip():
            return

        loop = asyncio.get_running_loop()
        voice = os.getenv("VIBR8_TTS_VOICE", "af_sarah")
        speed = float(os.getenv("VIBR8_TTS_SPEED", "1.0"))

        await loop.run_in_executor(None, _ensure_pipeline)

        codec = _create_opus_codec()
        gen = _pipeline(text, voice=voice, speed=speed)
        residual: np.ndarray | None = None
        pts = 0

        while not self._cancelled:
            result = await loop.run_in_executor(
                None, _generate_and_encode, gen, codec, residual, pts
            )
            if result is None:
                break

            frames, residual, pts = result

            for frame in frames:
                if self._cancelled:
                    logger.info("[tts-kokoro] TTS cancelled (barge-in)")
                    return
                if self._opus_frame_handler:
                    self._opus_frame_handler(frame)

        if not self._cancelled:
            flush_frames = await loop.run_in_executor(
                None, _flush_codec, codec, residual, pts
            )
            for frame in flush_frames:
                if self._opus_frame_handler:
                    self._opus_frame_handler(frame)
