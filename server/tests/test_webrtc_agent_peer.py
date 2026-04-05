"""Verify that an aiortc peer can receive video frames and send data channel messages.

This is a loopback test: one "server" peer (like WebRTCManager) adds a video track
and handles the data channel, one "agent" peer (like DesktopTarget) receives frames
and sends input events. Same process, no network.
"""

import asyncio
import json
import pytest
from fractions import Fraction

import av
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamTrack

# ── Synthetic video track (stands in for ScreenShareTrack) ──────────────

_VIDEO_CLOCK_RATE = 90000


class SyntheticVideoTrack(MediaStreamTrack):
    """Generates solid-color frames at a given fps for testing."""

    kind = "video"

    def __init__(self, width: int = 1280, height: int = 720, fps: int = 10):
        super().__init__()
        self._width = width
        self._height = height
        self._fps = fps
        self._pts = 0
        self._frame_count = 0

    async def recv(self) -> av.VideoFrame:
        await asyncio.sleep(1.0 / self._fps)
        r = self._frame_count % 256
        g = (self._frame_count * 7) % 256
        b = (self._frame_count * 13) % 256
        arr = np.full((self._height, self._width, 3), [r, g, b], dtype=np.uint8)
        frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
        frame.pts = self._pts
        frame.time_base = Fraction(1, _VIDEO_CLOCK_RATE)
        self._pts += _VIDEO_CLOCK_RATE // self._fps
        self._frame_count += 1
        return frame


# ── Helpers ──────────────────────────────────────────────────────────────


async def _wait_ice_gathering(pc: RTCPeerConnection, timeout: float = 5.0) -> None:
    """Wait for ICE gathering to complete (same as webrtc.py:586-597)."""
    if pc.iceGatheringState == "complete":
        return
    done = asyncio.Event()

    @pc.on("icegatheringstatechange")
    def _on_change():
        if pc.iceGatheringState == "complete":
            done.set()

    await asyncio.wait_for(done.wait(), timeout=timeout)


async def _do_signaling(
    agent_pc: RTCPeerConnection,
    server_pc: RTCPeerConnection,
    video_track: MediaStreamTrack,
) -> None:
    """Perform full offer/answer exchange with ICE gathering waits.

    Mirrors the real flow:
    - Agent creates offer (like browser in webrtc.ts)
    - Server adds video track and creates answer (like WebRTCManager.handle_offer)
    - Both sides wait for ICE gathering before sharing SDP
    """
    # Agent creates offer
    offer = await agent_pc.createOffer()
    await agent_pc.setLocalDescription(offer)
    await _wait_ice_gathering(agent_pc)

    # Server receives offer, adds video track, creates answer
    server_pc.addTrack(video_track)
    await server_pc.setRemoteDescription(
        RTCSessionDescription(
            sdp=agent_pc.localDescription.sdp,
            type=agent_pc.localDescription.type,
        )
    )

    answer = await server_pc.createAnswer()
    await server_pc.setLocalDescription(answer)
    await _wait_ice_gathering(server_pc)

    # Agent receives answer
    await agent_pc.setRemoteDescription(
        RTCSessionDescription(
            sdp=server_pc.localDescription.sdp,
            type=server_pc.localDescription.type,
        )
    )


# ── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_receives_video_frames():
    """Agent peer receives decoded video frames and can send input via data channel."""

    server_pc = RTCPeerConnection()
    agent_pc = RTCPeerConnection()

    # Agent declares recvonly video (same as browser: webrtc.ts:255)
    agent_pc.addTransceiver("video", direction="recvonly")

    # Agent creates data channel (same as browser: webrtc.ts:241)
    input_channel = agent_pc.createDataChannel("input")

    # Events
    video_track_ready = asyncio.Event()
    channel_ready = asyncio.Event()
    agent_video_track = None
    received_inputs: list[dict] = []

    @agent_pc.on("track")
    def on_track(track):
        nonlocal agent_video_track
        if track.kind == "video":
            agent_video_track = track
            video_track_ready.set()

    @server_pc.on("datachannel")
    def on_datachannel(channel):
        @channel.on("message")
        def on_message(msg):
            received_inputs.append(json.loads(msg))

    @input_channel.on("open")
    def on_open():
        channel_ready.set()

    # ── Signaling ──
    video_track = SyntheticVideoTrack(width=1280, height=720, fps=10)
    await _do_signaling(agent_pc, server_pc, video_track)

    # ── Wait for track + channel ──
    await asyncio.wait_for(video_track_ready.wait(), timeout=10.0)
    assert agent_video_track is not None

    await asyncio.wait_for(channel_ready.wait(), timeout=10.0)
    assert input_channel.readyState == "open"

    # ── Receive video frames ──
    frames = []
    for _ in range(3):
        frame = await asyncio.wait_for(agent_video_track.recv(), timeout=5.0)
        frames.append(frame)

    assert len(frames) == 3
    for frame in frames:
        assert isinstance(frame, av.VideoFrame)
        assert frame.width > 0
        assert frame.height > 0

    # ── Send input events on data channel ──
    test_events = [
        {"type": "mousemove", "x": 0.5, "y": 0.5},
        {"type": "mousedown", "button": 0},
        {"type": "mouseup", "button": 0},
        {"type": "keydown", "key": "a"},
    ]
    for event in test_events:
        input_channel.send(json.dumps(event))

    await asyncio.sleep(0.5)

    assert len(received_inputs) == len(test_events)
    assert received_inputs[0]["type"] == "mousemove"
    assert received_inputs[0]["x"] == 0.5
    assert received_inputs[3]["key"] == "a"

    # ── Cleanup ──
    video_track.stop()
    await agent_pc.close()
    await server_pc.close()


@pytest.mark.asyncio
async def test_agent_frame_dimensions():
    """Verify received frames preserve the source dimensions."""

    server_pc = RTCPeerConnection()
    agent_pc = RTCPeerConnection()

    agent_pc.addTransceiver("video", direction="recvonly")

    video_ready = asyncio.Event()
    agent_track = None

    @agent_pc.on("track")
    def on_track(track):
        nonlocal agent_track
        if track.kind == "video":
            agent_track = track
            video_ready.set()

    video_track = SyntheticVideoTrack(width=1920, height=1080, fps=10)
    await _do_signaling(agent_pc, server_pc, video_track)

    await asyncio.wait_for(video_ready.wait(), timeout=10.0)

    frame = await asyncio.wait_for(agent_track.recv(), timeout=5.0)
    assert isinstance(frame, av.VideoFrame)
    # H.264 may round to nearest macroblock (16px), but should be close
    assert abs(frame.width - 1920) <= 16
    assert abs(frame.height - 1080) <= 16

    video_track.stop()
    await agent_pc.close()
    await server_pc.close()
