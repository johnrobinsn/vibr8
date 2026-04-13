"""FrameStream — ring-buffered frame stream for video-capable agents.

Wraps any AgentTarget and provides:
  - Push-based delivery via ``subscribe(callback)``
  - Pull-based history via ``recent(n)`` and ``recent_seconds(seconds)``
  - Optional downsampling to a target FPS
  - Auto-detects push vs polling based on target capabilities

Single-frame agents (like UITarsAgent) ignore this entirely and use
``target.get_frame()`` directly.  Video agents create a FrameStream
in their factory function.

Usage:
    stream = FrameStream(target, max_buffer=300, target_fps=10)
    await stream.start()

    # Pull: last 3 seconds of frames
    frames = stream.recent_seconds(3.0)

    # Push: subscribe to new frames
    unsub = stream.subscribe(my_callback)

    await stream.stop()
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Callable

import av

logger = logging.getLogger(__name__)

# Type alias for frame subscriber callbacks
FrameCallback = Callable[["TimestampedFrame"], None]


class TimestampedFrame:
    """A video frame with a monotonic capture timestamp."""

    __slots__ = ("frame", "timestamp")

    def __init__(self, frame: av.VideoFrame, timestamp: float) -> None:
        self.frame = frame
        self.timestamp = timestamp


class FrameStream:
    """Ring-buffered frame stream with push and pull interfaces.

    Automatically uses push delivery (``on_frame`` callback) if the target
    supports it, otherwise falls back to polling ``get_frame()`` in a
    background task.

    Parameters:
        target: Any AgentTarget. If it has an ``on_frame`` method, push
                delivery is used; otherwise polling.
        max_buffer: Maximum frames in the ring buffer. At 30fps, 300 = 10s.
        target_fps: If set, downsample to this rate. Frames arriving faster
                    than this are dropped before buffering. None = no limit.
    """

    def __init__(
        self,
        target: Any,
        max_buffer: int = 300,
        target_fps: float | None = None,
    ) -> None:
        self._target = target
        self._max_buffer = max_buffer
        self._target_fps = target_fps
        self._min_interval = 1.0 / target_fps if target_fps else 0.0

        self._buffer: deque[TimestampedFrame] = deque(maxlen=max_buffer)
        self._subscribers: list[FrameCallback] = []
        self._unsubscribe_target: Callable[[], None] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._running = False
        self._last_buffer_time: float = 0.0

        # Measured FPS
        self._frame_count = 0
        self._fps_window_start: float = 0.0
        self._measured_fps: float = 0.0

    async def start(self) -> None:
        """Start the frame stream. Attaches to the target's frame delivery."""
        if self._running:
            return
        self._running = True
        self._fps_window_start = time.monotonic()

        # Prefer push if the target supports it
        if hasattr(self._target, "on_frame") and callable(self._target.on_frame):
            self._unsubscribe_target = self._target.on_frame(self._on_frame_push)
            logger.info("[frame-stream] Using push delivery from target")
        else:
            # Fall back to polling
            poll_fps = self._target_fps or 30.0
            self._poll_task = asyncio.create_task(self._poll_loop(poll_fps))
            logger.info("[frame-stream] Using polling at %.1f fps", poll_fps)

    async def stop(self) -> None:
        """Stop the frame stream and release resources."""
        self._running = False

        if self._unsubscribe_target:
            self._unsubscribe_target()
            self._unsubscribe_target = None

        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

        self._subscribers.clear()
        self._buffer.clear()

    # ── Pull interface ───────────────────────────────────────────────────

    def latest(self) -> av.VideoFrame | None:
        """Return the most recent frame, or None if buffer is empty."""
        if self._buffer:
            return self._buffer[-1].frame
        return None

    def recent(self, n: int) -> list[TimestampedFrame]:
        """Return the last *n* frames (oldest first)."""
        buf = self._buffer
        start = max(0, len(buf) - n)
        return list(buf)[start:]

    def recent_seconds(self, seconds: float) -> list[TimestampedFrame]:
        """Return all frames from the last *seconds* of wall time."""
        cutoff = time.monotonic() - seconds
        result: list[TimestampedFrame] = []
        for entry in self._buffer:
            if entry.timestamp >= cutoff:
                result.append(entry)
        return result

    # ── Push interface ───────────────────────────────────────────────────

    def subscribe(self, callback: FrameCallback) -> Callable[[], None]:
        """Subscribe to new frames. Returns unsubscribe function.

        Callbacks are synchronous and must not block — they run inside
        the target's frame delivery path (drain loop or decode loop).
        """
        self._subscribers.append(callback)
        return lambda: self._subscribers.remove(callback) if callback in self._subscribers else None

    # ── Properties ───────────────────────────────────────────────────────

    @property
    def fps(self) -> float:
        """Measured frames-per-second entering the buffer."""
        return self._measured_fps

    @property
    def buffer_size(self) -> int:
        """Current number of frames in the ring buffer."""
        return len(self._buffer)

    @property
    def buffer_capacity(self) -> int:
        """Maximum ring buffer size."""
        return self._max_buffer

    # ── Internal: frame ingestion ────────────────────────────────────────

    def _on_frame_push(self, frame: av.VideoFrame) -> None:
        """Called synchronously by the target's drain/decode loop."""
        now = time.monotonic()

        # Downsample: skip if too soon since last buffered frame
        if self._min_interval > 0 and (now - self._last_buffer_time) < self._min_interval:
            return

        self._last_buffer_time = now
        entry = TimestampedFrame(frame, now)
        self._buffer.append(entry)

        # Update FPS measurement (rolling 1-second window)
        self._frame_count += 1
        elapsed = now - self._fps_window_start
        if elapsed >= 1.0:
            self._measured_fps = self._frame_count / elapsed
            self._frame_count = 0
            self._fps_window_start = now

        # Notify subscribers
        for cb in self._subscribers:
            try:
                cb(entry)
            except Exception:
                pass

    async def _poll_loop(self, poll_fps: float) -> None:
        """Fallback: poll get_frame() at the given rate."""
        interval = 1.0 / poll_fps
        last_frame_id: int | None = None

        while self._running:
            try:
                frame = await self._target.get_frame()
                if frame is not None:
                    # Only buffer if it's a different frame object
                    frame_id = id(frame)
                    if frame_id != last_frame_id:
                        last_frame_id = frame_id
                        self._on_frame_push(frame)
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[frame-stream] Poll loop error")
                await asyncio.sleep(1)
