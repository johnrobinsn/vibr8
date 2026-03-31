"""Screen capture — cross-platform primary display capture for remote desktop streaming.

Linux: Uses av x11grab (ffmpeg) for efficient X11 capture with built-in scaling.
macOS: Uses mss (CGDisplay) with av reformat for color conversion and scaling.

Returns av.VideoFrame objects (yuv420p) ready for WebRTC VideoStreamTrack consumption.

Usage:
    capture = ScreenCapture(target_fps=30, max_height=1080)
    await capture.start()
    frame = await capture.get_frame()  # av.VideoFrame (yuv420p) or None if unchanged
    await capture.stop()
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import av

logger = logging.getLogger("vibr8-node")


class NoDisplayError(Exception):
    """Raised when no display is available for screen capture."""


# Thread pool for blocking capture calls
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="screen-capture")


def _get_platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    elif sys.platform.startswith("linux"):
        return "linux"
    elif sys.platform == "win32":
        return "windows"
    return "unknown"


def _get_x11_display_size(display: str) -> tuple[int, int]:
    """Query the X11 display dimensions using xdpyinfo or xrandr."""
    import subprocess

    # Try xdpyinfo first
    try:
        result = subprocess.run(
            ["xdpyinfo", "-display", display],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "dimensions:" in line:
                    # "  dimensions:    2560x1440 pixels ..."
                    parts = line.split()
                    idx = parts.index("dimensions:") + 1
                    w, h = parts[idx].split("x")
                    return int(w), int(h)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: xrandr
    try:
        result = subprocess.run(
            ["xrandr", "--display", display, "--current"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if " connected " in line and "+" in line:
                    # "DP-1 connected 2560x1440+0+0 ..."
                    for part in line.split():
                        if "x" in part and "+" in part:
                            res = part.split("+")[0]
                            w, h = res.split("x")
                            return int(w), int(h)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    raise NoDisplayError(f"Cannot determine display size for {display}")


class ScreenCapture:
    """Captures the primary display and returns av.VideoFrame objects.

    On Linux, uses av x11grab (ffmpeg) for efficient capture with built-in
    scaling. On macOS, falls back to mss with av reformat.

    Frames are returned in yuv420p format, ready for WebRTC VP8 encoding.
    """

    def __init__(self, target_fps: int = 30, max_height: int = 1080) -> None:
        self.target_fps = target_fps
        self.max_height = max_height
        self._frame_interval = 1.0 / target_fps
        self._running = False
        self._lock = asyncio.Lock()

        # Backend state
        self._backend: str = ""  # "x11grab" or "mss"
        self._needs_scale: bool = False
        self._container: av.InputContainer | None = None  # x11grab
        self._stream: av.video.stream.VideoStream | None = None
        self._demux_iter: object | None = None
        self._sct: object | None = None  # mss
        self._monitor: dict | None = None

        # Display info (populated on start)
        self.native_width: int = 0
        self.native_height: int = 0
        self.capture_width: int = 0
        self.capture_height: int = 0

        # Stats
        self._frames_captured: int = 0
        self._frames_skipped: int = 0
        self._last_capture_time: float = 0
        self._dpms_inhibited: bool = False

    async def start(self) -> dict:
        """Initialize capture. Returns display info dict.

        Raises NoDisplayError if no display is available.
        """
        async with self._lock:
            if self._running:
                return self.get_display_info()

            info = await asyncio.get_event_loop().run_in_executor(
                _executor, self._init_capture
            )
            self._running = True
            logger.info(
                "[screen-capture] Started (%s): native %dx%d, capture %dx%d, %dfps",
                self._backend,
                self.native_width, self.native_height,
                self.capture_width, self.capture_height,
                self.target_fps,
            )
            return info

    async def stop(self) -> None:
        """Release capture resources."""
        async with self._lock:
            if not self._running:
                return
            self._running = False
            await asyncio.get_event_loop().run_in_executor(
                _executor, self._cleanup_capture
            )
            logger.info(
                "[screen-capture] Stopped: %d captured, %d skipped",
                self._frames_captured, self._frames_skipped,
            )

    async def get_frame(self) -> Optional[av.VideoFrame]:
        """Capture a frame and return as yuv420p VideoFrame.

        Returns None if not running. Runs blocking capture in thread executor.
        """
        if not self._running:
            return None
        return await asyncio.get_event_loop().run_in_executor(
            _executor, self._capture_frame
        )

    def get_display_info(self) -> dict:
        """Return current display/capture dimensions."""
        return {
            "nativeWidth": self.native_width,
            "nativeHeight": self.native_height,
            "captureWidth": self.capture_width,
            "captureHeight": self.capture_height,
            "targetFps": self.target_fps,
            "platform": _get_platform(),
            "backend": self._backend,
        }

    def get_stats(self) -> dict:
        """Return capture statistics."""
        return {
            "framesCaptured": self._frames_captured,
            "framesSkipped": self._frames_skipped,
            "running": self._running,
        }

    # --- Blocking methods (run in executor) ---

    def _init_capture(self) -> dict:
        """Initialize the appropriate backend. Runs in thread.

        Linux: x11grab via av (ffmpeg). Handles scaling natively.
        macOS: mss (CGDisplay) with av reformat for color conversion.

        Raises NoDisplayError if no display is available.
        """
        if sys.platform.startswith("linux"):
            return self._init_x11grab()
        else:
            return self._init_mss()

    def _inhibit_dpms(self, display: str) -> None:
        """Disable screensaver and DPMS blanking, wake display. Runs in thread."""
        import subprocess
        env = {**os.environ, "DISPLAY": display}
        try:
            # Wake display if blanked, disable screensaver and DPMS
            subprocess.run(["xset", "dpms", "force", "on"], env=env, timeout=3, capture_output=True)
            subprocess.run(["xset", "s", "off", "-dpms"], env=env, timeout=3, capture_output=True)
            self._dpms_inhibited = True
            logger.info("[screen-capture] DPMS inhibited, display woken")
        except Exception as exc:
            logger.warning("[screen-capture] Could not inhibit DPMS: %s", exc)

    def _restore_dpms(self, display: str) -> None:
        """Restore screensaver and DPMS to defaults. Runs in thread."""
        import subprocess
        env = {**os.environ, "DISPLAY": display}
        try:
            subprocess.run(["xset", "s", "on", "+dpms"], env=env, timeout=3, capture_output=True)
            self._dpms_inhibited = False
            logger.info("[screen-capture] DPMS restored")
        except Exception as exc:
            logger.warning("[screen-capture] Could not restore DPMS: %s", exc)

    def _init_x11grab(self) -> dict:
        """Initialize x11grab capture via av (ffmpeg). Runs in thread."""
        display = os.environ.get("DISPLAY", "")
        if not display:
            raise NoDisplayError("$DISPLAY not set")

        # Wake display and disable blanking while streaming
        self._inhibit_dpms(display)

        # Get native display size
        self.native_width, self.native_height = _get_x11_display_size(display)

        # Compute capture dimensions (downscale if needed, keep even)
        self._compute_capture_dims()
        self._needs_scale = (self.capture_width != self.native_width
                             or self.capture_height != self.native_height)

        # x11grab video_size sets the capture region, so always capture at
        # native resolution to get the full screen.  Downscaling is done in
        # _capture_x11grab via frame.reformat().
        try:
            self._container = av.open(
                display, format="x11grab",
                options={
                    "video_size": f"{self.native_width}x{self.native_height}",
                    "framerate": str(self.target_fps),
                    "draw_mouse": "1",
                },
            )
        except av.error.FileNotFoundError as exc:
            raise NoDisplayError(f"x11grab failed: {exc}") from exc

        self._stream = self._container.streams.video[0]
        self._demux_iter = self._container.demux(self._stream)
        self._backend = "x11grab"
        self._frames_captured = 0
        self._frames_skipped = 0
        return self.get_display_info()

    def _init_mss(self) -> dict:
        """Initialize mss capture. Runs in thread."""
        import mss
        from mss.exception import ScreenShotError

        try:
            self._sct = mss.mss()
        except ScreenShotError as exc:
            raise NoDisplayError(str(exc)) from exc

        if len(self._sct.monitors) < 2:
            self._sct.close()
            self._sct = None
            raise NoDisplayError("No monitors detected")

        # Primary monitor (index 1; index 0 is the virtual "all monitors" rect)
        self._monitor = self._sct.monitors[1]
        self.native_width = self._monitor["width"]
        self.native_height = self._monitor["height"]

        self._compute_capture_dims()
        self._backend = "mss"
        self._frames_captured = 0
        self._frames_skipped = 0
        return self.get_display_info()

    def _compute_capture_dims(self) -> None:
        """Set capture_width/capture_height from native dims and max_height."""
        if self.native_height > self.max_height:
            scale = self.max_height / self.native_height
            self.capture_width = int(self.native_width * scale) & ~1
            self.capture_height = self.max_height & ~1
        else:
            self.capture_width = self.native_width & ~1
            self.capture_height = self.native_height & ~1

    def _cleanup_capture(self) -> None:
        """Release resources. Runs in thread."""
        self._demux_iter = None
        self._stream = None
        if self._container is not None:
            self._container.close()
            self._container = None
        if self._sct is not None:
            self._sct.close()
            self._sct = None
        self._monitor = None
        if self._dpms_inhibited:
            display = os.environ.get("DISPLAY", ":0")
            self._restore_dpms(display)

    def _capture_frame(self) -> Optional[av.VideoFrame]:
        """Capture one frame. Dispatches to the active backend. Runs in thread."""
        if self._backend == "x11grab":
            return self._capture_x11grab()
        elif self._backend == "mss":
            return self._capture_mss()
        return None

    def _capture_x11grab(self) -> Optional[av.VideoFrame]:
        """Grab a frame via x11grab. Returns yuv420p VideoFrame."""
        if self._demux_iter is None:
            return None

        try:
            for packet in self._demux_iter:
                for frame in packet.decode():
                    if self._needs_scale:
                        out = frame.reformat(
                            width=self.capture_width,
                            height=self.capture_height,
                            format="yuv420p",
                        )
                    else:
                        out = frame.reformat(format="yuv420p")
                    self._frames_captured += 1
                    self._last_capture_time = time.monotonic()
                    return out
        except (av.error.EOFError, StopIteration):
            logger.warning("[screen-capture] x11grab stream ended (EOF/StopIteration)")
            return None
        except Exception as exc:
            if self._frames_captured == 0:
                logger.error("[screen-capture] x11grab first frame failed: %s", exc)
            else:
                logger.warning("[screen-capture] x11grab error after %d frames: %s", self._frames_captured, exc)
            return None
        return None

    def _capture_mss(self) -> Optional[av.VideoFrame]:
        """Grab a frame via mss. Returns yuv420p VideoFrame or None if unchanged."""
        if self._sct is None or self._monitor is None:
            return None

        shot = self._sct.grab(self._monitor)

        # Create BGRA frame directly from raw pixel data
        frame = av.VideoFrame(shot.width, shot.height, "bgra")
        frame.planes[0].update(shot.raw)

        # Single reformat: color conversion (bgra → yuv420p) + optional downscale
        if shot.height > self.max_height:
            out = frame.reformat(
                width=self.capture_width,
                height=self.capture_height,
                format="yuv420p",
            )
        else:
            out = frame.reformat(format="yuv420p")

        self._frames_captured += 1
        self._last_capture_time = time.monotonic()
        return out
