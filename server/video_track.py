"""WebRTC video track that streams screen capture frames.

Follows the QueuedAudioTrack pattern (audio_track.py) — a MediaStreamTrack
subclass with real-time pacing in recv().  Pulls frames from ScreenCapture
and delivers them to the WebRTC peer connection at 30fps.

Usage:
    capture = ScreenCapture(target_fps=30, max_height=1080)
    await capture.start()
    track = ScreenShareTrack(capture)
    pc.addTrack(track)
    # ... on teardown:
    track.stop()
    await capture.stop()
"""

from __future__ import annotations

import asyncio
import logging
from fractions import Fraction
from time import time

from aiortc.mediastreams import MediaStreamTrack
from av import VideoFrame

from server.screen_capture import ScreenCapture

logger = logging.getLogger(__name__)

_VIDEO_TIME_BASE = Fraction(1, 90000)  # standard RTP video time base
_VIDEO_CLOCK_RATE = 90000


class ScreenShareTrack(MediaStreamTrack):
    """A MediaStreamTrack that delivers screen capture frames.

    The WebRTC transport calls :meth:`recv` at the frame rate interval.
    If no new frame is available from screen capture (e.g. screen unchanged),
    the previous frame is re-sent to keep the stream alive.
    """

    kind = "video"

    def __init__(self, capture: ScreenCapture) -> None:
        super().__init__()
        self._capture = capture
        self._next_pts: int = 0
        self._stream_time: float | None = None
        self._frame_duration: float = 1.0 / capture.target_fps
        self._pts_per_frame: int = int(_VIDEO_CLOCK_RATE / capture.target_fps)
        self._last_frame: VideoFrame | None = None
        self._frames_sent: int = 0

    def _make_blank_frame(self) -> VideoFrame:
        """Generate a black frame at capture resolution."""
        w = self._capture.capture_width or 1920
        h = self._capture.capture_height or 1080
        frame = VideoFrame(w, h, "yuv420p")
        # yuv420p black: Y=0, U=128, V=128
        for i, plane in enumerate(frame.planes):
            if i == 0:
                plane.update(b"\x00" * (plane.line_size * h))
            else:
                plane.update(b"\x80" * (plane.line_size * (h // 2)))
        return frame

    async def recv(self) -> VideoFrame:
        """Return the next video frame, paced to real-time."""
        # Pace to target frame rate
        if self._stream_time is None:
            self._stream_time = time()
            logger.info("[video-track] recv() first call — starting frame delivery")

        wait = self._stream_time - time()
        if wait > 0:
            await asyncio.sleep(wait)
        self._stream_time += self._frame_duration

        # Get frame from screen capture
        frame = await self._capture.get_frame()

        if frame is not None:
            self._last_frame = frame
            self._null_streak = 0
        elif self._last_frame is not None:
            frame = self._last_frame
            self._null_streak = getattr(self, "_null_streak", 0) + 1
        else:
            frame = self._make_blank_frame()
            self._last_frame = frame
            self._null_streak = getattr(self, "_null_streak", 0) + 1

        # Stamp PTS for proper timing
        frame.pts = self._next_pts
        frame.time_base = _VIDEO_TIME_BASE
        self._next_pts += self._pts_per_frame
        self._frames_sent += 1

        # Periodic diagnostics
        if self._frames_sent == 1:
            logger.info(
                "[video-track] first frame sent: %dx%d format=%s (from_capture=%s)",
                frame.width, frame.height, frame.format.name,
                self._capture._frames_captured > 0,
            )
        elif self._frames_sent % 300 == 0:  # every ~10s at 30fps
            logger.info(
                "[video-track] stats: sent=%d captured=%d null_streak=%d",
                self._frames_sent, self._capture._frames_captured,
                getattr(self, "_null_streak", 0),
            )

        return frame
