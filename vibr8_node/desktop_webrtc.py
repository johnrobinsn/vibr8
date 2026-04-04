"""Lightweight desktop-only WebRTC handler for Docker node agents.

Handles screen capture + input injection without importing the full
WebRTCManager (which pulls in STT/TTS/torch dependencies not available
in the Docker container).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from typing import Dict

logger = logging.getLogger("vibr8-node")

# Lazy-loaded — only available when aiortc+av are installed (gui/gpu images)
_aiortc_available: bool | None = None


def _check_aiortc() -> bool:
    global _aiortc_available
    if _aiortc_available is None:
        try:
            import aiortc  # noqa: F401
            import av  # noqa: F401
            _aiortc_available = True
        except ImportError:
            _aiortc_available = False
    return _aiortc_available


# ── Clipboard helpers ─────────────────────────────────────────────────────

_IS_LINUX = sys.platform.startswith("linux")
_IS_MACOS = sys.platform == "darwin"


def _clipboard_read() -> str:
    try:
        if _IS_MACOS:
            return subprocess.check_output(
                ["pbpaste"], timeout=3,
            ).decode("utf-8", errors="replace")
        elif _IS_LINUX and shutil.which("xclip"):
            return subprocess.check_output(
                ["xclip", "-o", "-selection", "clipboard"],
                timeout=3,
                env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":99")},
            ).decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("[desktop-webrtc] clipboard read failed: %s", exc)
    return ""


def _clipboard_write(text: str) -> None:
    try:
        if _IS_MACOS:
            subprocess.run(
                ["pbcopy"], input=text.encode(),
                timeout=3, check=True,
            )
        elif _IS_LINUX and shutil.which("xclip"):
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=text.encode(),
                timeout=3,
                check=True,
                env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":99")},
            )
    except Exception as exc:
        logger.warning("[desktop-webrtc] clipboard write failed: %s", exc)


async def _handle_clipboard_get(channel) -> None:
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, _clipboard_read)
    if channel.readyState == "open":
        channel.send(json.dumps({"type": "clipboard", "text": text}))


async def _handle_clipboard_set(text: str) -> None:
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _clipboard_write, text)


# ── Desktop WebRTC Manager ───────────────────────────────────────────────


class DesktopWebRTCManager:
    """Minimal WebRTC manager for desktop streaming only.

    Handles screen capture, video track, and input injection data channel.
    Does NOT handle audio STT/TTS (not needed on remote nodes).
    """

    def __init__(self) -> None:
        self._connections: Dict[str, object] = {}     # client_id → RTCPeerConnection
        self._captures: Dict[str, object] = {}        # client_id → ScreenCapture
        self._injectors: Dict[str, object] = {}       # client_id → InputInjector
        self._shared_capture: object | None = None     # single shared ScreenCapture
        self._shared_capture_refs: set[str] = set()

    async def handle_offer(
        self,
        client_id: str,
        sdp: str,
        sdp_type: str = "offer",
        desktop_role: str = "controller",
        ice_servers: list[dict] | None = None,
    ) -> dict[str, str]:
        """Process an SDP offer for desktop streaming and return an SDP answer."""
        if not _check_aiortc():
            raise RuntimeError("aiortc/av not installed — desktop streaming unavailable")

        from aiortc import (
            RTCConfiguration,
            RTCIceServer,
            RTCPeerConnection,
            RTCSessionDescription,
        )
        from aiortc.codecs import h264 as _h264_codec
        from server.screen_capture import ScreenCapture, NoDisplayError
        from server.video_track import ScreenShareTrack
        from server.input_injector import InputInjector

        # Raise H.264 bitrate limits for screen content (sharp text/UI)
        _h264_codec.DEFAULT_BITRATE = 4_000_000  # 4 Mbps (was 1 Mbps)
        _h264_codec.MAX_BITRATE = 8_000_000      # 8 Mbps (was 3 Mbps)

        # Tear down any pre-existing connection for this client
        if client_id in self._connections:
            await self.close_connection(client_id)

        # Build ICE server list
        ice_server_objs = []
        for srv in (ice_servers or []):
            ice_server_objs.append(RTCIceServer(
                urls=srv["urls"],
                username=srv.get("username"),
                credential=srv.get("credential"),
            ))
        pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_server_objs))
        self._connections[client_id] = pc

        @pc.on("connectionstatechange")
        async def _on_state_change() -> None:
            logger.info("[desktop-webrtc] client %s state: %s", client_id, pc.connectionState)
            if pc.connectionState == "connected":
                # Force a keyframe after bitrate ramp-up for a sharp first image
                async def _delayed_keyframe() -> None:
                    await asyncio.sleep(2)
                    for t in pc.getTransceivers():
                        if t.kind == "video" and hasattr(t.sender, "_send_keyframe"):
                            t.sender._send_keyframe()
                            logger.info("[desktop-webrtc] client %s: forced keyframe", client_id)
                            break
                asyncio.ensure_future(_delayed_keyframe())
            elif pc.connectionState in ("failed", "closed"):
                await self.close_connection(client_id)

        # Screen capture
        display = os.environ.get("DISPLAY", ":99")
        try:
            if self._shared_capture is not None:
                capture = self._shared_capture
                self._shared_capture_refs.add(client_id)
            else:
                capture = ScreenCapture(target_fps=30, max_height=1080)
                await capture.start()
                self._shared_capture = capture
                self._shared_capture_refs = {client_id}

            video_track = ScreenShareTrack(capture)
            pc.addTrack(video_track)

            # Prefer H.264 for compatibility
            for t in pc.getTransceivers():
                if t.kind == "video":
                    from aiortc.rtcrtpsender import RTCRtpSender
                    caps = RTCRtpSender.getCapabilities("video")
                    h264 = [c for c in caps.codecs if "H264" in c.mimeType]
                    if h264:
                        t.setCodecPreferences(h264)
                    break

            self._captures[client_id] = capture
            logger.info(
                "[desktop-webrtc] client %s: desktop track added (%s, %dx%d)",
                client_id, desktop_role, capture.capture_width, capture.capture_height,
            )

            if desktop_role != "viewer":
                # Controller: input data channel
                @pc.on("datachannel")
                def on_datachannel(channel):
                    injector = InputInjector(
                        display, capture.native_width, capture.native_height,
                        screen_size_fn=lambda: (capture.native_width, capture.native_height),
                    )
                    self._injectors[client_id] = injector
                    logger.info("[desktop-webrtc] client %s: input channel opened", client_id)

                    @channel.on("message")
                    def on_message(msg):
                        try:
                            event = json.loads(msg)
                            etype = event.get("type")
                            if etype == "clipboard_get":
                                asyncio.ensure_future(_handle_clipboard_get(channel))
                            elif etype == "clipboard_set":
                                asyncio.ensure_future(_handle_clipboard_set(event.get("text", "")))
                            else:
                                asyncio.ensure_future(injector.inject(event))
                        except Exception:
                            pass

        except NoDisplayError as exc:
            logger.warning("[desktop-webrtc] client %s: no display: %s", client_id, exc)
            raise

        # SDP exchange
        offer = RTCSessionDescription(sdp=sdp, type=sdp_type)
        await pc.setRemoteDescription(offer)
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        # Wait for ICE gathering
        if pc.iceGatheringState != "complete":
            gathering_done = asyncio.Event()

            @pc.on("icegatheringstatechange")
            def _on_ice() -> None:
                if pc.iceGatheringState == "complete":
                    gathering_done.set()

            try:
                await asyncio.wait_for(gathering_done.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("[desktop-webrtc] client %s: ICE gathering timed out", client_id)

        return {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
        }

    async def close_connection(self, client_id: str) -> None:
        """Close and clean up a desktop peer connection."""
        self._injectors.pop(client_id, None)
        capture = self._captures.pop(client_id, None)
        if capture and capture is self._shared_capture:
            self._shared_capture_refs.discard(client_id)
            if not self._shared_capture_refs:
                self._shared_capture = None
                try:
                    await capture.stop()
                except Exception:
                    pass
        elif capture:
            try:
                await capture.stop()
            except Exception:
                pass

        pc = self._connections.pop(client_id, None)
        if pc is not None:
            await pc.close()
            logger.info("[desktop-webrtc] closed connection for client %s", client_id)

    async def close_all(self) -> None:
        """Close all desktop connections."""
        for client_id in list(self._connections):
            await self.close_connection(client_id)
