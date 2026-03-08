"""Speech-to-text using Whisper with VAD and end-of-utterance detection.

Adapted from neortc2 (Copyright 2024 John Robinson, Apache 2.0).

Combines the synchronous STT core and async wrapper into one module.
Simplified for vibr8: mono input only, no file capture.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import warnings
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Callable, Optional

import numpy as np
import torch
from scipy.signal import resample_poly
from transformers import pipeline

warnings.filterwarnings("ignore", message=".*logits.*", category=UserWarning)
logging.getLogger("transformers").setLevel(logging.ERROR)

from server.eou import create_eou

warnings.simplefilter(action="ignore", category=FutureWarning)

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

WHISPER_SAMPLE_RATE = 16000
SILERO_SAMPLE_RATE = WHISPER_SAMPLE_RATE  # Must match whisper


@dataclass
class STTParams:
    """Tunable parameters for the STT pipeline."""
    mic_gain: float = 1.0
    vad_threshold_db: float = -30.0
    silero_vad_threshold: float = 0.4
    eou_threshold: float = 0.15
    eou_max_retries: int = 3
    min_segment_duration: float = 0.4

# Common Whisper hallucination patterns on silence/noise
_HALLUCINATION_PATTERNS = {
    "thank you", "thanks for watching", "thank you for watching",
    "thanks for listening", "thank you for listening",
    "subscribe", "like and subscribe", "please subscribe",
    "subtitle", "subtitles", "subtitled by",
    "you", "bye", "the end",
    "...", "…",
}


# ── Synchronous STT core ──────────────────────────────────────────────────────


class STT:
    """Synchronous Whisper-based STT with Silero VAD and EOU detection.

    Processes audio buffers through a state machine that detects voice
    activity, transcribes speech segments, and determines utterance boundaries.
    """

    shared_resources: dict[str, Any] = {}

    class Event(Enum):
        VOICE_WAS_DETECTED = auto()
        VOICE_NOT_DETECTED = auto()

    class State(Enum):
        IDLE = auto()
        SEGMENT_0 = auto()  # First voice segment (may be noise)
        SEGMENT_N = auto()  # Confirmed voice
        SILENCE_0 = auto()
        SILENCE_1 = auto()
        SILENCE_2 = auto()

    def __init__(self, sample_rate: int, num_channels: int = 1, params: STTParams | None = None) -> None:
        self._sample_rate = sample_rate
        self._num_channels = num_channels
        self._params = params or STTParams()
        self._listeners: list[Callable] = []

        self._segment: list[np.ndarray] = []
        self._capture_time: float = 0.0
        self._segment_time_begin: float = 0.0
        self._segment_time_end: float = 0.0

        self._state_machine = self._create_state_machine()

    # ── Shared model management ──────────────────────────────────────────

    @staticmethod
    def preload_shared_resources() -> None:
        """Load Whisper, Silero VAD, and EOU models (once, shared across instances)."""
        if "vad" not in STT.shared_resources:
            logger.info("[stt] Loading Silero VAD...")
            model, _utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
            )
            STT.shared_resources["vad"] = model
            logger.info("[stt] Silero VAD loaded")

        if "asr" not in STT.shared_resources:
            logger.info("[stt] Loading Whisper...")
            device = "cuda" if torch.cuda.is_available() else "cpu"
            asr = pipeline(
                "automatic-speech-recognition",
                model="openai/whisper-large-v3",
                device=device,
            )
            STT.shared_resources["asr"] = asr
            logger.info("[stt] Whisper loaded (device=%s)", device)

        if "eou" not in STT.shared_resources:
            logger.info("[stt] Loading EOU model...")
            STT.shared_resources["eou"] = create_eou()
            logger.info("[stt] EOU model loaded")

    @staticmethod
    def unload_shared_resources() -> None:
        STT.shared_resources = {}

    # ── Listener management ──────────────────────────────────────────────

    def add_listener(self, listener: Callable) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable) -> None:
        self._listeners.remove(listener)

    def _notify_listeners(self, event_type: str, data: Any) -> None:
        for listener in self._listeners:
            listener(self, event_type, data)

    # ── Audio processing ─────────────────────────────────────────────────

    def update_params(self, params: STTParams) -> None:
        """Update tunable parameters (safe: called from single ThreadWorker)."""
        self._params = params

    def process_buffer(self, buffer: np.ndarray) -> None:
        """Process an audio buffer chunk through the STT pipeline.

        *buffer* is a numpy int16 array, shape ``(1, N)`` for mono.
        """
        STT.preload_shared_resources()
        params = self._params

        # Convert to 1-D mono float.
        mono = buffer.mean(axis=0)  # (N,) — implicit float conversion
        if self._num_channels == 2:
            mono = ((mono[::2] + mono[1::2]) / 2).astype(np.int16)

        # Apply mic gain before resampling.
        if params.mic_gain != 1.0:
            mono = np.clip(mono.astype(np.float32) * params.mic_gain,
                           np.iinfo(np.int16).min, np.iinfo(np.int16).max).astype(np.int16)

        # Resample to whisper sample rate (e.g. 48kHz → 16kHz = factor 1/3).
        resampled = resample_poly(mono, up=WHISPER_SAMPLE_RATE, down=self._sample_rate).astype(np.int16)

        # RMS-based silence detection.
        float_buf = resampled.astype(np.float32) / np.iinfo(np.int16).max
        rms = np.sqrt(np.mean(float_buf**2))
        rms_db = 20 * np.log10(max(rms, 1e-10))

        # Emit voice level for playground mic meter.
        self._notify_listeners("voice_level", {"rmsDb": float(rms_db)})

        is_silent = rms_db < params.vad_threshold_db

        if not is_silent:
            # Silero VAD on 512-sample chunks.
            float_buf_2d = float_buf.reshape(-1, 512)
            vad = STT.shared_resources["vad"]
            p = vad(torch.from_numpy(float_buf_2d), SILERO_SAMPLE_RATE)
            is_silent = bool(torch.all(p < params.silero_vad_threshold))

        event = STT.Event.VOICE_NOT_DETECTED if is_silent else STT.Event.VOICE_WAS_DETECTED
        self._state_machine.handle_event(event, self._segment, resampled)
        self._capture_time += 160.0 / 1000.0

    def flush(self) -> None:
        """Reset the state machine and emit a 'flushed' event."""
        self._segment = []
        self._capture_time = 0.0
        self._segment_time_begin = 0.0
        self._segment_time_end = 0.0
        self._state_machine.state = STT.State.IDLE
        self._notify_listeners("flushed", None)

    # ── State machine ────────────────────────────────────────────────────

    def _create_state_machine(self) -> _StateMachine:
        stt = self

        @dataclass
        class Transition:
            next_state: STT.State
            action: Optional[Callable] = None

        class _StateMachine:
            def __init__(self) -> None:
                self.state = STT.State.IDLE
                self.eou_counter = 0

                def process_segment(s: list, b: np.ndarray) -> None:
                    s.append(b)
                    stt._segment_time_end = stt._capture_time + 160.0 / 1000.0
                    params = stt._params

                    # Skip very short segments (likely noise bursts)
                    duration = stt._segment_time_end - stt._segment_time_begin
                    if duration < params.min_segment_duration:
                        logger.debug("[stt] Discarding short segment: %.2fs", duration)
                        s.clear()
                        return

                    combined = np.concatenate(s, axis=0)
                    float_buf = combined.astype(np.float32) / np.iinfo(np.int16).max

                    asr = STT.shared_resources["asr"]
                    text = asr(float_buf)["text"]

                    # Filter Whisper hallucinations
                    text_stripped = text.strip().rstrip(".!?,").strip()
                    if text_stripped.lower() in _HALLUCINATION_PATTERNS:
                        logger.info("[stt] Filtered hallucination: %r", text)
                        s.clear()
                        stt._notify_listeners("voice_not_detected", None)
                        return

                    eou = STT.shared_resources["eou"]
                    eou_prob = eou(text)

                    if eou_prob < params.eou_threshold and self.eou_counter < params.eou_max_retries:
                        self.state = STT.State.SEGMENT_N
                        self.eou_counter += 1
                        return

                    stt._notify_listeners("voice_not_detected", None)
                    stt._notify_listeners("final_transcript", {
                        "timeBegin": stt._segment_time_begin,
                        "timeEnd": stt._segment_time_end,
                        "transcript": text,
                        "audio": combined,
                    })
                    s.clear()

                def capture_segment(s: list, b: np.ndarray) -> None:
                    s.append(b)
                    stt._segment_time_begin = stt._capture_time

                def voice_was_detected(s: list, b: np.ndarray) -> None:
                    s.append(b)
                    stt._notify_listeners("voice_was_detected", None)
                    self.eou_counter = 0

                self.transitions = {
                    (STT.State.IDLE, STT.Event.VOICE_NOT_DETECTED): Transition(
                        STT.State.IDLE, lambda s, b: s.clear(),
                    ),
                    (STT.State.IDLE, STT.Event.VOICE_WAS_DETECTED): Transition(
                        STT.State.SEGMENT_0, capture_segment,
                    ),
                    (STT.State.SEGMENT_0, STT.Event.VOICE_NOT_DETECTED): Transition(
                        STT.State.IDLE, lambda s, b: s.clear(),
                    ),
                    (STT.State.SEGMENT_0, STT.Event.VOICE_WAS_DETECTED): Transition(
                        STT.State.SEGMENT_N, voice_was_detected,
                    ),
                    (STT.State.SEGMENT_N, STT.Event.VOICE_NOT_DETECTED): Transition(
                        STT.State.SILENCE_0, lambda s, b: s.append(b),
                    ),
                    (STT.State.SEGMENT_N, STT.Event.VOICE_WAS_DETECTED): Transition(
                        STT.State.SEGMENT_N, lambda s, b: s.append(b),
                    ),
                    (STT.State.SILENCE_0, STT.Event.VOICE_NOT_DETECTED): Transition(
                        STT.State.SILENCE_1, lambda s, b: s.append(b),
                    ),
                    (STT.State.SILENCE_0, STT.Event.VOICE_WAS_DETECTED): Transition(
                        STT.State.SEGMENT_N, lambda s, b: s.append(b),
                    ),
                    (STT.State.SILENCE_1, STT.Event.VOICE_NOT_DETECTED): Transition(
                        STT.State.SILENCE_2, lambda s, b: s.append(b),
                    ),
                    (STT.State.SILENCE_1, STT.Event.VOICE_WAS_DETECTED): Transition(
                        STT.State.SEGMENT_N, lambda s, b: s.append(b),
                    ),
                    (STT.State.SILENCE_2, STT.Event.VOICE_NOT_DETECTED): Transition(
                        STT.State.IDLE, process_segment,
                    ),
                    (STT.State.SILENCE_2, STT.Event.VOICE_WAS_DETECTED): Transition(
                        STT.State.SEGMENT_N, lambda s, b: s.append(b),
                    ),
                }

            def handle_event(self, event: STT.Event, *args) -> None:
                key = (self.state, event)
                if key not in self.transitions:
                    logger.error("[stt] Invalid transition: %s in state %s", event.name, self.state.name)
                    return
                transition = self.transitions[key]
                self.state = transition.next_state
                if transition.action:
                    try:
                        transition.action(*args)
                    except Exception:
                        logger.exception("[stt] Error in state machine action")

        return _StateMachine()


# ── Async wrapper ──────────────────────────────────────────────────────────────


class AsyncSTT(STT):
    """Non-blocking wrapper around :class:`STT`.

    Heavy processing (Whisper, VAD) runs on a :class:`ThreadWorker` so
    the asyncio event loop is never blocked.
    """

    def __init__(self, sample_rate: int = 48000, num_channels: int = 1, params: STTParams | None = None) -> None:
        super().__init__(sample_rate, num_channels, params=params)
        from server.threadworker import ThreadWorker

        self._worker = ThreadWorker(support_out_q=False)
        self._lock = threading.Lock()
        self._loop = asyncio.get_event_loop()

    def stop(self) -> None:
        """Shut down the worker thread."""
        self._worker.stop()

    # Override listener notification to dispatch back to the event loop.

    async def _async_notify_listeners(self, event_type: str, data: Any) -> None:
        for listener in self._listeners:
            if asyncio.iscoroutinefunction(listener):
                await listener(self, event_type, data)
            else:
                listener(self, event_type, data)

    def _notify_listeners(self, event_type: str, data: Any) -> None:
        asyncio.run_coroutine_threadsafe(
            self._async_notify_listeners(event_type, data), self._loop,
        )

    # Thread-safe wrappers that run on the worker thread.

    def _locked_update_params(self, params: STTParams) -> None:
        with self._lock:
            super().update_params(params)

    def update_params(self, params: STTParams) -> None:
        """Enqueue a params update on the worker thread."""
        self._worker.add_task(self._locked_update_params, params)

    def _locked_process_buffer(self, buffer: np.ndarray) -> None:
        with self._lock:
            super().process_buffer(buffer)

    def process_buffer(self, buffer: np.ndarray) -> None:
        """Enqueue audio buffer for processing on the worker thread."""
        self._worker.add_task(self._locked_process_buffer, buffer)

    def _locked_flush(self) -> None:
        with self._lock:
            super().flush()

    def flush(self) -> None:
        """Enqueue a flush operation on the worker thread."""
        self._worker.add_task(self._locked_flush)


# Make _StateMachine accessible as a nested name for type hints.
_StateMachine = type(STT(48000, 1)._state_machine) if False else object  # noqa: E501 — type stub only
