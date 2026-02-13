"""WebRTC module — manages per-session RTCPeerConnection instances using aiortc.

Provides audio streaming capabilities for vibr8 sessions, including
a test tone generator and incoming audio stats logging.
"""

from __future__ import annotations

import asyncio
import fractions
import logging
import time
from typing import Dict

import numpy as np
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from av import AudioFrame

logger = logging.getLogger(__name__)


class AudioStatsLogger:
    """Periodically logs frame count and peak RMS level for an audio stream."""

    def __init__(
        self, session_id: str, direction: str, interval: float = 5.0
    ) -> None:
        self.session_id = session_id
        self.direction = direction
        self.interval = interval

        self._frame_count: int = 0
        self._peak_rms: float = 0.0
        self._sample_rate: int = 0
        self._channels: int = 0
        self._last_log_time: float = time.monotonic()

    def log_frame(self, frame: AudioFrame) -> None:
        """Accumulate stats for *frame* and emit a log line every *interval* seconds."""
        self._frame_count += 1
        self._sample_rate = frame.sample_rate
        self._channels = len(frame.layout.channels)

        samples = frame.to_ndarray().astype(np.float64)
        rms = float(np.sqrt(np.mean(samples**2)))
        if rms > self._peak_rms:
            self._peak_rms = rms

        now = time.monotonic()
        if now - self._last_log_time >= self.interval:
            logger.info(
                "[webrtc] %s audio for session %s: %d frames, "
                "peak_level=%.1f, sample_rate=%d, channels=%d",
                self.direction,
                self.session_id,
                self._frame_count,
                self._peak_rms,
                self._sample_rate,
                self._channels,
            )
            # Reset counters for the next interval.
            self._frame_count = 0
            self._peak_rms = 0.0
            self._last_log_time = now


class TestToneTrack(MediaStreamTrack):
    """Generates a 440 Hz sine wave as an audio MediaStreamTrack.

    Output format: 48 kHz, mono, 20 ms frames (960 samples), signed 16-bit.
    """

    kind = "audio"

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self._session_id = session_id
        self._sample_rate = 48000
        self._samples_per_frame = 960  # 20 ms at 48 kHz
        self._frequency = 440.0
        self._phase = 0.0
        self._pts = 0
        self._stats = AudioStatsLogger(session_id, "outgoing")

    async def recv(self) -> AudioFrame:
        """Return the next 20 ms audio frame containing a 440 Hz tone."""
        # Generate sine wave samples.
        t = (
            np.arange(self._samples_per_frame) / self._sample_rate
            + self._phase / (2.0 * np.pi * self._frequency)
        )
        samples = (np.sin(2.0 * np.pi * self._frequency * t) * 32767 * 0.5).astype(
            np.int16
        )
        self._phase = (
            2.0
            * np.pi
            * self._frequency
            * (self._samples_per_frame / self._sample_rate)
            + self._phase
        ) % (2.0 * np.pi)

        # Build the AudioFrame.  Shape must be (1, samples) for mono s16.
        frame = AudioFrame.from_ndarray(
            samples.reshape(1, -1), format="s16", layout="mono"
        )
        frame.pts = self._pts
        frame.sample_rate = self._sample_rate
        frame.time_base = fractions.Fraction(1, self._sample_rate)
        self._pts += self._samples_per_frame

        self._stats.log_frame(frame)

        # Pace output to real-time.
        await asyncio.sleep(0.02)
        return frame


class WebRTCManager:
    """Manages per-session RTCPeerConnection instances."""

    def __init__(self) -> None:
        self._connections: Dict[str, RTCPeerConnection] = {}
        self._stats: Dict[str, AudioStatsLogger] = {}

    async def handle_offer(
        self, session_id: str, sdp: str, sdp_type: str
    ) -> dict[str, str]:
        """Process an SDP offer and return an SDP answer.

        If a connection already exists for *session_id* it is closed first.
        A :class:`TestToneTrack` is added as the outgoing audio track so the
        remote peer receives a 440 Hz test tone.
        """
        # Tear down any pre-existing connection for this session.
        if session_id in self._connections:
            await self.close_connection(session_id)

        # No STUN/TURN servers — local network only, avoids aioice retry errors.
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self._connections[session_id] = pc

        @pc.on("connectionstatechange")
        async def _on_connection_state_change() -> None:
            logger.info(
                "[webrtc] session %s connection state: %s",
                session_id,
                pc.connectionState,
            )
            if pc.connectionState in ("failed", "closed"):
                await self.close_connection(session_id)

        @pc.on("track")
        def _on_track(track: MediaStreamTrack) -> None:
            logger.info(
                "[webrtc] session %s received %s track", session_id, track.kind
            )
            if track.kind == "audio":
                stats = AudioStatsLogger(session_id, "incoming")
                self._stats[session_id] = stats
                asyncio.ensure_future(self._consume_audio(session_id, track, stats))

        # Add outgoing test tone.
        tone = TestToneTrack(session_id)
        pc.addTrack(tone)

        # SDP exchange.
        offer = RTCSessionDescription(sdp=sdp, type=sdp_type)
        await pc.setRemoteDescription(offer)

        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        return {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
        }

    async def _consume_audio(
        self,
        session_id: str,
        track: MediaStreamTrack,
        stats: AudioStatsLogger,
    ) -> None:
        """Read frames from *track* until it ends, logging stats along the way."""
        try:
            while True:
                frame = await track.recv()
                stats.log_frame(frame)
        except Exception:
            logger.info(
                "[webrtc] incoming audio track ended for session %s", session_id
            )

    async def close_connection(self, session_id: str) -> None:
        """Close and remove the peer connection for *session_id*."""
        pc = self._connections.pop(session_id, None)
        self._stats.pop(session_id, None)
        if pc is not None:
            await pc.close()
            logger.info("[webrtc] closed connection for session %s", session_id)

    async def close_all(self) -> None:
        """Close every active peer connection."""
        session_ids = list(self._connections.keys())
        for session_id in session_ids:
            await self.close_connection(session_id)
