"""WebRTC video track that streams frames from a ScrcpyClient.

Same pattern as ScreenShareTrack (video_track.py) but pulls frames
from a ScrcpyClient instead of ScreenCapture.
"""

from __future__ import annotations

import asyncio
import logging
from fractions import Fraction
from time import time

from aiortc.mediastreams import MediaStreamTrack
from av import VideoFrame

from server.scrcpy_client import ScrcpyClient

logger = logging.getLogger(__name__)

_VIDEO_TIME_BASE = Fraction(1, 90000)
_VIDEO_CLOCK_RATE = 90000


class ScrcpyVideoTrack(MediaStreamTrack):
    """A MediaStreamTrack that delivers scrcpy-decoded video frames.

    Pulls from ScrcpyClient.get_frame() at the target frame rate.
    Re-sends the previous frame if no new frame is available.
    """

    kind = "video"

    def __init__(self, client: ScrcpyClient, target_fps: int = 30) -> None:
        super().__init__()
        self._client = client
        self._target_fps = target_fps
        self._next_pts: int = 0
        self._stream_time: float | None = None
        self._frame_duration: float = 1.0 / target_fps
        self._pts_per_frame: int = int(_VIDEO_CLOCK_RATE / target_fps)
        self._last_frame: VideoFrame | None = None
        self._frames_sent: int = 0

    def _make_blank_frame(self) -> VideoFrame:
        """Generate a black frame at the client's resolution."""
        w = self._client.screen_width or 1080
        h = self._client.screen_height or 1920
        frame = VideoFrame(w, h, "yuv420p")
        for i, plane in enumerate(frame.planes):
            ph = h if i == 0 else h // 2
            fill = b"\x00" if i == 0 else b"\x80"
            plane.update(fill * (plane.line_size * ph))
        return frame

    async def recv(self) -> VideoFrame:
        """Return the next video frame, paced to real-time."""
        if self._stream_time is None:
            self._stream_time = time()

        wait = self._stream_time - time()
        if wait > 0:
            await asyncio.sleep(wait)
        self._stream_time += self._frame_duration

        frame = await self._client.get_frame()

        if frame is not None:
            # Ensure frame is in yuv420p for WebRTC encoding
            if frame.format.name != "yuv420p":
                frame = frame.reformat(format="yuv420p")
            self._last_frame = frame
        elif self._last_frame is not None:
            frame = self._last_frame
        else:
            frame = self._make_blank_frame()
            self._last_frame = frame

        frame.pts = self._next_pts
        frame.time_base = _VIDEO_TIME_BASE
        self._next_pts += self._pts_per_frame
        self._frames_sent += 1

        if self._frames_sent == 1:
            logger.info("[scrcpy-track] first frame: %dx%d format=%s",
                        frame.width, frame.height, frame.format.name)

        return frame
