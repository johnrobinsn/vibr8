"""Tests for server.screen_capture — unit tests that don't require a display."""

from unittest.mock import patch, MagicMock

import av
import numpy as np
import pytest

from server.screen_capture import ScreenCapture, NoDisplayError


class TestScreenCaptureInit:
    def test_defaults(self):
        cap = ScreenCapture()
        assert cap.target_fps == 30
        assert cap.max_height == 1080
        assert cap._frame_interval == pytest.approx(1.0 / 30)

    def test_custom_params(self):
        cap = ScreenCapture(target_fps=15, max_height=720)
        assert cap.target_fps == 15
        assert cap.max_height == 720

    def test_display_info_before_start(self):
        cap = ScreenCapture()
        info = cap.get_display_info()
        assert info["targetFps"] == 30
        assert info["nativeWidth"] == 0
        assert info["platform"] in ("macos", "linux", "windows", "unknown")

    def test_stats_before_start(self):
        cap = ScreenCapture()
        stats = cap.get_stats()
        assert stats["running"] is False
        assert stats["framesCaptured"] == 0
        assert stats["framesSkipped"] == 0


class TestResolutionScaling:
    def test_downscale_1440p(self):
        cap = ScreenCapture(max_height=1080)
        cap.native_width = 2560
        cap.native_height = 1440
        cap._compute_capture_dims()
        assert cap.capture_width == 1920
        assert cap.capture_height == 1080

    def test_no_downscale_1080p(self):
        cap = ScreenCapture(max_height=1080)
        cap.native_width = 1920
        cap.native_height = 1080
        cap._compute_capture_dims()
        assert cap.capture_width == 1920
        assert cap.capture_height == 1080

    def test_downscale_4k(self):
        cap = ScreenCapture(max_height=1080)
        cap.native_width = 3840
        cap.native_height = 2160
        cap._compute_capture_dims()
        assert cap.capture_width == 1920
        assert cap.capture_height == 1080

    def test_even_dimensions(self):
        cap = ScreenCapture(max_height=1080)
        cap.native_width = 1921
        cap.native_height = 1081
        cap._compute_capture_dims()
        assert cap.capture_width % 2 == 0
        assert cap.capture_height % 2 == 0


class TestVideoFramePipeline:
    def test_frame_from_ndarray(self):
        rgb = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame = av.VideoFrame.from_ndarray(rgb, format="rgb24")
        assert frame.width == 1280
        assert frame.height == 720

    def test_frame_reformat_to_yuv420p(self):
        rgb = np.zeros((1080, 1920, 3), dtype=np.uint8)
        frame = av.VideoFrame.from_ndarray(rgb, format="rgb24")
        out = frame.reformat(format="yuv420p")
        assert out.format.name == "yuv420p"

    def test_bgra_to_yuv420p_with_scale(self):
        bgra = np.zeros((1440, 2560, 4), dtype=np.uint8)
        frame = av.VideoFrame(2560, 1440, "bgra")
        frame.planes[0].update(bytes(bgra))
        out = frame.reformat(width=1920, height=1080, format="yuv420p")
        assert out.width == 1920
        assert out.height == 1080
        assert out.format.name == "yuv420p"


class TestNoDisplayError:
    async def test_no_display_mss_error(self):
        from mss.exception import ScreenShotError

        cap = ScreenCapture()
        with patch("mss.mss", side_effect=ScreenShotError("$DISPLAY not set")):
            with pytest.raises(NoDisplayError, match="DISPLAY not set"):
                cap._init_mss()

    async def test_no_monitors_mss(self):
        mock_sct = MagicMock()
        mock_sct.monitors = [{"left": 0, "top": 0, "width": 0, "height": 0}]

        cap = ScreenCapture()
        with patch("mss.mss", return_value=mock_sct):
            with pytest.raises(NoDisplayError, match="No monitors detected"):
                cap._init_mss()

    async def test_no_display_x11grab(self):
        cap = ScreenCapture()
        with patch.dict("os.environ", {"DISPLAY": ""}, clear=False):
            with pytest.raises(NoDisplayError, match="DISPLAY not set"):
                cap._init_x11grab()


class TestMssBackend:
    """Tests using a mocked mss backend."""

    @staticmethod
    def _make_mock_sct(width=1920, height=1080):
        sct = MagicMock()
        sct.monitors = [
            {"left": 0, "top": 0, "width": width * 2, "height": height * 2},
            {"left": 0, "top": 0, "width": width, "height": height},
        ]
        bgra = np.zeros((height, width, 4), dtype=np.uint8)
        bgra[:, :, 0] = 100  # B
        bgra[:, :, 1] = 150  # G
        bgra[:, :, 2] = 200  # R
        bgra[:, :, 3] = 255  # A

        shot = MagicMock()
        shot.raw = bytes(bgra)
        shot.width = width
        shot.height = height
        sct.grab.return_value = shot
        return sct

    async def test_init_sets_dimensions(self):
        cap = ScreenCapture(max_height=1080)
        mock_sct = self._make_mock_sct(2560, 1440)

        with patch("mss.mss", return_value=mock_sct):
            cap._init_mss()

        assert cap.native_width == 2560
        assert cap.native_height == 1440
        assert cap.capture_width == 1920
        assert cap.capture_height == 1080
        assert cap._backend == "mss"

    async def test_capture_returns_yuv420p(self):
        cap = ScreenCapture(max_height=1080)
        mock_sct = self._make_mock_sct(1920, 1080)

        with patch("mss.mss", return_value=mock_sct):
            cap._init_mss()

        frame = cap._capture_mss()
        assert frame is not None
        assert isinstance(frame, av.VideoFrame)
        assert frame.format.name == "yuv420p"
        assert frame.width == 1920
        assert frame.height == 1080

    async def test_capture_downscales(self):
        cap = ScreenCapture(max_height=1080)
        mock_sct = self._make_mock_sct(2560, 1440)

        with patch("mss.mss", return_value=mock_sct):
            cap._init_mss()

        frame = cap._capture_mss()
        assert frame is not None
        assert frame.width == 1920
        assert frame.height == 1080

    async def test_capture_increments_stats(self):
        cap = ScreenCapture()
        mock_sct = self._make_mock_sct(1920, 1080)

        with patch("mss.mss", return_value=mock_sct):
            cap._init_mss()

        cap._capture_mss()
        cap._capture_mss()
        assert cap._frames_captured == 2


class TestLifecycle:
    async def test_get_frame_when_stopped(self):
        cap = ScreenCapture()
        frame = await cap.get_frame()
        assert frame is None

    async def test_start_stop(self):
        cap = ScreenCapture()
        with patch.object(cap, "_init_capture", return_value={
            "nativeWidth": 1920, "nativeHeight": 1080,
            "captureWidth": 1920, "captureHeight": 1080,
            "targetFps": 30, "platform": "linux", "backend": "x11grab",
        }):
            info = await cap.start()
            assert info["nativeWidth"] == 1920
            assert cap._running

        # Idempotent start
        with patch.object(cap, "_init_capture") as mock_init:
            await cap.start()
            mock_init.assert_not_called()

        with patch.object(cap, "_cleanup_capture"):
            await cap.stop()
            assert not cap._running

        # Idempotent stop
        with patch.object(cap, "_cleanup_capture") as mock_cleanup:
            await cap.stop()
            mock_cleanup.assert_not_called()
