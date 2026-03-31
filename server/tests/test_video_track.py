"""Tests for server.video_track — ScreenShareTrack unit tests."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import av
import numpy as np
import pytest

from server.video_track import ScreenShareTrack


def _make_mock_capture(width=1920, height=1080, fps=30):
    """Create a mock ScreenCapture that returns synthetic frames."""
    capture = MagicMock()
    capture.target_fps = fps
    capture.capture_width = width
    capture.capture_height = height

    # Generate a yuv420p frame
    frame = av.VideoFrame(width, height, "yuv420p")
    for i, plane in enumerate(frame.planes):
        h = height if i == 0 else height // 2
        plane.update(b"\x80" * (plane.line_size * h))

    capture.get_frame = AsyncMock(return_value=frame)
    return capture, frame


class TestScreenShareTrack:
    def test_kind_is_video(self):
        capture, _ = _make_mock_capture()
        track = ScreenShareTrack(capture)
        assert track.kind == "video"

    async def test_recv_returns_video_frame(self):
        capture, _ = _make_mock_capture()
        track = ScreenShareTrack(capture)

        frame = await track.recv()
        assert isinstance(frame, av.VideoFrame)
        assert frame.width == 1920
        assert frame.height == 1080

    async def test_recv_stamps_pts(self):
        capture, _ = _make_mock_capture()
        track = ScreenShareTrack(capture)

        f1 = await track.recv()
        pts1 = f1.pts
        f2 = await track.recv()
        pts2 = f2.pts
        assert pts1 == 0
        assert pts2 > pts1  # PTS advances each frame

    async def test_recv_reuses_last_frame_when_capture_returns_none(self):
        capture, frame = _make_mock_capture()
        track = ScreenShareTrack(capture)

        # First call returns a real frame
        f1 = await track.recv()
        assert f1 is not None

        # Subsequent calls return None (screen unchanged)
        capture.get_frame = AsyncMock(return_value=None)
        f2 = await track.recv()
        assert f2 is not None  # Should reuse last frame
        assert f2.width == 1920

    async def test_recv_generates_blank_when_no_prior_frame(self):
        capture, _ = _make_mock_capture()
        capture.get_frame = AsyncMock(return_value=None)
        track = ScreenShareTrack(capture)

        # No prior frame — should generate blank
        frame = await track.recv()
        assert isinstance(frame, av.VideoFrame)
        assert frame.format.name == "yuv420p"

    async def test_frames_sent_counter(self):
        capture, _ = _make_mock_capture()
        track = ScreenShareTrack(capture)

        await track.recv()
        await track.recv()
        await track.recv()
        assert track._frames_sent == 3

    async def test_time_base(self):
        capture, _ = _make_mock_capture()
        track = ScreenShareTrack(capture)

        frame = await track.recv()
        assert frame.time_base is not None
