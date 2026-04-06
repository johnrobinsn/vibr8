"""DesktopTarget — WebRTC peer that receives desktop video and sends input.

Acts as an internal WebRTC client (using aiortc), connecting through the
exact same signaling and media path as a browser viewer.  This ensures
the agent sees the same video stream and uses the same input injection
pipeline — one code path, local or remote.

Supports hybrid reconnect: on connection loss, tries a few quick retries
with exponential backoff, then waits for a tunnel reconnect signal before
retrying once more.

Usage:
    async def signal(sdp, sdp_type):
        return await webrtc_manager.handle_offer(client_id, sdp, sdp_type, desktop=True)

    target = DesktopTarget(signaling_fn=signal, ice_servers=[...])
    await target.start()
    frame = await target.get_frame()       # av.VideoFrame
    await target.inject({"type": "mousemove", "x": 0.5, "y": 0.5})
    await target.stop()
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
from typing import Any, Awaitable, Callable

import av
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

logger = logging.getLogger(__name__)

# Type for the signaling callback injected by main.py
SignalingFn = Callable[[str, str], Awaitable[dict[str, str]]]

# Reconnect config
_QUICK_RETRY_COUNT = 3
_QUICK_RETRY_BACKOFF = [2, 4, 8]  # seconds


class DesktopTarget:
    """WebRTC peer that receives desktop video and sends input events.

    Mirrors the browser's WebRTC flow (webrtc.ts):
      - addTransceiver("video", "recvonly")
      - createDataChannel("input")
      - createOffer → signal → setRemoteDescription(answer)
      - recv() decoded frames from the video track
      - send() input events as JSON on the data channel
    """

    def __init__(
        self,
        signaling_fn: SignalingFn,
        ice_servers: list[dict[str, Any]] | None = None,
        reconnect_signal: asyncio.Event | None = None,
    ) -> None:
        self._signal = signaling_fn
        self._ice_servers = ice_servers or []
        self._reconnect_signal = reconnect_signal

        self._pc: RTCPeerConnection | None = None
        self._video_track: Any | None = None  # RemoteStreamTrack
        self._input_channel: Any | None = None  # RTCDataChannel
        self._started = False
        self._connected = False

        # Frame drain loop: continuously reads from the video track
        # and caches the latest frame so the agent always gets a fresh one.
        self._latest_frame: av.VideoFrame | None = None
        self._drain_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None

        self._native_width: int = 0
        self._native_height: int = 0

        # Callbacks for agent to hook into
        self.on_disconnect: Callable[[], Awaitable[None]] | None = None
        self.on_reconnect: Callable[[], Awaitable[None]] | None = None

    @property
    def native_width(self) -> int:
        return self._native_width

    @property
    def native_height(self) -> int:
        return self._native_height

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        """Create peer connection, do signaling, wait for video track."""
        if self._started:
            return
        await self._establish_connection()
        self._started = True
        logger.info(
            "[desktop-target] Started (WebRTC peer) %dx%d",
            self._native_width, self._native_height,
        )

    async def stop(self) -> None:
        """Tear down the peer connection."""
        if not self._started:
            return
        self._started = False
        self._connected = False

        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass

        await self._teardown()
        logger.info("[desktop-target] Stopped")

    async def get_frame(self) -> av.VideoFrame | None:
        """Return the most recently received video frame."""
        return self._latest_frame

    async def inject(self, event: dict) -> None:
        """Send an input event on the data channel (same as browser)."""
        ch = self._input_channel
        if ch and ch.readyState == "open":
            ch.send(_json.dumps(event))

    # ── Connection establishment ─────────────────────────────────────────

    async def _establish_connection(self) -> None:
        """Create WebRTC peer connection and wait for video track."""
        # Tear down any previous connection
        await self._teardown()

        # Build ICE config
        ice_objs = []
        for srv in self._ice_servers:
            ice_objs.append(RTCIceServer(
                urls=srv["urls"],
                username=srv.get("username"),
                credential=srv.get("credential"),
            ))
        self._pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_objs))

        # Declare recvonly video (same as browser: webrtc.ts:255)
        self._pc.addTransceiver("video", direction="recvonly")

        # Create input data channel (same as browser: webrtc.ts:241)
        self._input_channel = self._pc.createDataChannel("input")

        # Listen for incoming video track
        track_ready = asyncio.Event()

        @self._pc.on("track")
        def on_track(track):
            if track.kind == "video":
                self._video_track = track
                track_ready.set()

        @self._pc.on("connectionstatechange")
        async def on_state_change():
            state = self._pc.connectionState if self._pc else "unknown"
            logger.info("[desktop-target] connection state: %s", state)
            if state in ("failed", "closed"):
                logger.warning("[desktop-target] Terminal state '%s' — triggering reconnect", state)
                self._handle_disconnect()

        # Create offer
        offer = await self._pc.createOffer()
        await self._pc.setLocalDescription(offer)

        # Wait for ICE gathering (same as webrtc.py:586-597)
        if self._pc.iceGatheringState != "complete":
            gathering_done = asyncio.Event()

            @self._pc.on("icegatheringstatechange")
            def _on_ice():
                if self._pc and self._pc.iceGatheringState == "complete":
                    gathering_done.set()

            try:
                await asyncio.wait_for(gathering_done.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("[desktop-target] ICE gathering timed out")

        # Signal: send offer, receive answer
        logger.info("[desktop-target] Sending signaling offer...")
        answer = await self._signal(
            self._pc.localDescription.sdp,
            self._pc.localDescription.type,
        )
        await self._pc.setRemoteDescription(
            RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]),
        )
        logger.info("[desktop-target] Remote description set, ICE connecting...")

        # Wait for video track
        logger.info("[desktop-target] Waiting for video track...")
        await asyncio.wait_for(track_ready.wait(), timeout=15.0)
        logger.info("[desktop-target] Video track received, waiting for first frame...")

        # Read first frame to get native resolution
        first_frame = await asyncio.wait_for(self._video_track.recv(), timeout=10.0)
        self._native_width = first_frame.width
        self._native_height = first_frame.height
        self._latest_frame = first_frame

        # Start draining frames in background
        self._drain_task = asyncio.create_task(self._drain_loop())

        self._connected = True

    async def _teardown(self) -> None:
        """Clean up current connection without changing started state."""
        if self._drain_task and not self._drain_task.done():
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass

        if self._pc:
            await self._pc.close()
            self._pc = None

        self._video_track = None
        self._input_channel = None
        self._latest_frame = None
        self._connected = False

    # ── Frame drain ──────────────────────────────────────────────────────

    async def _drain_loop(self) -> None:
        """Continuously drain the video track, caching the latest frame.

        ScreenShareTrack delivers at 30fps but the agent only needs ~1fps.
        This loop keeps the track flowing and always has a fresh frame ready.
        """
        track = self._video_track
        while track:
            try:
                frame = await track.recv()
                self._latest_frame = frame
                # Update resolution if it changes (e.g. display resize)
                if frame.width != self._native_width or frame.height != self._native_height:
                    self._native_width = frame.width
                    self._native_height = frame.height
            except asyncio.CancelledError:
                raise  # Don't treat cancellation as disconnect
            except Exception as e:
                if self._started and self._connected:
                    logger.warning("[desktop-target] Drain loop error (%s): %r", type(e).__name__, e)
                    self._handle_disconnect()
                else:
                    logger.debug("[desktop-target] Drain loop ended (started=%s connected=%s): %s", self._started, self._connected, e)
                break

    # ── Reconnect logic ──────────────────────────────────────────────────

    def _handle_disconnect(self) -> None:
        """Called when connection drops. Fires callback and starts reconnect."""
        if not self._connected and not self._started:
            return
        if self._reconnect_task and not self._reconnect_task.done():
            return  # already reconnecting

        self._connected = False
        self._reconnect_count = getattr(self, "_reconnect_count", 0) + 1
        logger.warning("[desktop-target] Connection lost (reconnect count: %d)", self._reconnect_count)

        if self._reconnect_count > 10:
            logger.error("[desktop-target] Too many reconnects, giving up")
            return

        if self._started:
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """Hybrid reconnect: quick retries then wait for tunnel signal."""
        # Notify agent of disconnect
        if self.on_disconnect:
            try:
                await self.on_disconnect()
            except Exception:
                logger.exception("[desktop-target] on_disconnect callback error")

        # Quick retries with exponential backoff
        for attempt in range(_QUICK_RETRY_COUNT):
            delay = _QUICK_RETRY_BACKOFF[attempt]
            logger.info("[desktop-target] Quick retry %d/%d in %ds...", attempt + 1, _QUICK_RETRY_COUNT, delay)
            await asyncio.sleep(delay)

            if not self._started:
                return  # stopped while waiting

            try:
                await self._establish_connection()
                self._reconnect_count = 0  # reset on success
                logger.info("[desktop-target] Reconnected on quick retry %d", attempt + 1)
                if self.on_reconnect:
                    await self.on_reconnect()
                return
            except Exception as e:
                logger.warning("[desktop-target] Quick retry %d failed: %s", attempt + 1, e)

        # Quick retries exhausted — wait for tunnel reconnect signal
        if self._reconnect_signal:
            logger.info("[desktop-target] Waiting for tunnel reconnect signal...")
            self._reconnect_signal.clear()
            await self._reconnect_signal.wait()

            if not self._started:
                return

            # One more attempt after tunnel signal
            try:
                await self._establish_connection()
                logger.info("[desktop-target] Reconnected after tunnel signal")
                if self.on_reconnect:
                    await self.on_reconnect()
                return
            except Exception as e:
                logger.error("[desktop-target] Reconnect after tunnel signal failed: %s", e)
        else:
            logger.warning("[desktop-target] No reconnect signal available, giving up")
