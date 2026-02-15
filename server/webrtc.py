"""WebRTC module — manages per-session RTCPeerConnection instances using aiortc.

Provides audio streaming capabilities for vibr8 sessions, including
STT (speech-to-text) for incoming audio and a queued audio track for
outgoing TTS audio.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict

import numpy as np
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from av import AudioFrame

from server.audio_track import QueuedAudioTrack
from server.stt import AsyncSTT

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


class WebRTCManager:
    """Manages per-session RTCPeerConnection instances with STT and TTS audio."""

    def __init__(self) -> None:
        self._connections: Dict[str, RTCPeerConnection] = {}
        self._stats: Dict[str, AudioStatsLogger] = {}
        self._outgoing_tracks: Dict[str, QueuedAudioTrack] = {}
        self._stt_instances: Dict[str, AsyncSTT] = {}
        self._stt_muted: set[str] = set()
        self._guard_enabled: Dict[str, bool] = {}
        self._tts_muted: Dict[str, bool] = {}
        self._ws_bridge = None

    def set_ws_bridge(self, bridge) -> None:
        """Set a reference to WsBridge for submitting STT transcripts."""
        self._ws_bridge = bridge

    def get_outgoing_track(self, session_id: str) -> QueuedAudioTrack | None:
        """Return the outgoing audio track for *session_id*, or None."""
        return self._outgoing_tracks.get(session_id)

    def barge_in(self, session_id: str) -> None:
        """Handle barge-in: clear queued TTS audio and cancel the TTS stream."""
        track = self._outgoing_tracks.get(session_id)
        if track:
            track.clear_audio()
            track.set_thinking(False)
        if self._ws_bridge:
            self._ws_bridge.cancel_tts(session_id)
        logger.info("[webrtc] barge-in for session %s: audio cleared", session_id)

    def set_thinking(self, session_id: str, thinking: bool) -> None:
        """Enable or disable the thinking-tone for *session_id*."""
        track = self._outgoing_tracks.get(session_id)
        if track:
            track.set_thinking(thinking)

    def mute_stt(self, session_id: str) -> None:
        """Suppress STT processing (prevents echo from triggering barge-in)."""
        self._stt_muted.add(session_id)

    def unmute_stt(self, session_id: str) -> None:
        """Re-enable STT processing."""
        self._stt_muted.discard(session_id)

    def is_guard_enabled(self, session_id: str) -> bool:
        """Return whether guard mode is active for *session_id*."""
        return self._guard_enabled.get(session_id, True)

    def set_guard_enabled(self, session_id: str, enabled: bool) -> None:
        """Enable or disable guard mode for *session_id*."""
        self._guard_enabled[session_id] = enabled
        logger.info("[guard] session %s: guard mode %s", session_id, "ON" if enabled else "OFF")
        # Broadcast to browser so UI stays in sync
        if self._ws_bridge:
            asyncio.ensure_future(
                self._ws_bridge.broadcast_guard_state(session_id, enabled)
            )

    def is_tts_muted(self, session_id: str) -> bool:
        """Return whether TTS is muted for *session_id*."""
        return self._tts_muted.get(session_id, False)

    def set_tts_muted(self, session_id: str, muted: bool) -> None:
        """Mute or unmute TTS for *session_id*."""
        self._tts_muted[session_id] = muted
        logger.info("[webrtc] session %s: TTS %s", session_id, "muted" if muted else "unmuted")
        if self._ws_bridge:
            asyncio.ensure_future(
                self._ws_bridge.broadcast_tts_muted(session_id, muted)
            )

    async def handle_offer(
        self, session_id: str, sdp: str, sdp_type: str
    ) -> dict[str, str]:
        """Process an SDP offer and return an SDP answer.

        Creates a :class:`QueuedAudioTrack` for outgoing TTS audio and
        an :class:`AsyncSTT` instance for incoming speech recognition.
        """
        # Tear down any pre-existing connection for this session.
        if session_id in self._connections:
            await self.close_connection(session_id)

        # No STUN/TURN servers — local network only, avoids aioice retry errors.
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self._connections[session_id] = pc

        # Create STT instance for this session.
        # aiortc typically delivers stereo (2-channel) audio even if source is mono.
        stt = AsyncSTT(sample_rate=48000, num_channels=2)
        stt.add_listener(self._make_stt_listener(session_id))
        self._stt_instances[session_id] = stt

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

        # Add outgoing audio track (receives TTS Opus frames via queue).
        outgoing = QueuedAudioTrack(session_id)
        pc.addTrack(outgoing)
        self._outgoing_tracks[session_id] = outgoing

        # SDP exchange.
        offer = RTCSessionDescription(sdp=sdp, type=sdp_type)
        await pc.setRemoteDescription(offer)

        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        return {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
        }

    @staticmethod
    def _find_guard_word(text_lower: str) -> tuple[int, int] | None:
        """Find the first occurrence of a guard word in lowered text.

        Returns (start_index, end_index) or None.
        """
        for word in ("vibr8", "vibrate"):
            idx = text_lower.find(word)
            if idx != -1:
                return (idx, idx + len(word))
        return None

    def _make_stt_listener(self, session_id: str):
        """Create an STT event listener that submits transcripts to the agent."""

        async def _on_stt_event(stt, event_type: str, data) -> None:
            if event_type == "final_transcript":
                transcript = data["transcript"].strip()
                logger.info("[stt] session %s transcript: %s", session_id, transcript)
                if not transcript or not self._ws_bridge:
                    return

                transcript_lower = transcript.lower()

                # Check for "vibr8/vibrate <command>" regardless of guard state
                match = self._find_guard_word(transcript_lower)
                if match:
                    after_word = transcript_lower[match[1]:].strip()
                    if after_word.startswith("off"):
                        # Disconnect audio entirely
                        if self._ws_bridge:
                            asyncio.ensure_future(
                                self._ws_bridge.broadcast_audio_off(session_id)
                            )
                        return
                    if after_word.startswith("guard"):
                        self.set_guard_enabled(session_id, True)
                        asyncio.ensure_future(self._speak_short(session_id, "Guard on"))
                        return
                    if after_word.startswith("listen"):
                        self.set_guard_enabled(session_id, False)
                        asyncio.ensure_future(self._speak_short(session_id, "Listening"))
                        return
                    if after_word.startswith("quiet"):
                        self.set_tts_muted(session_id, True)
                        asyncio.ensure_future(self._speak_short(session_id, "Quiet mode"))
                        return
                    if after_word.startswith("speak"):
                        self.set_tts_muted(session_id, False)
                        asyncio.ensure_future(self._speak_short(session_id, "Speaking"))
                        return

                # If guard is enabled, require guard word
                if self.is_guard_enabled(session_id):
                    if not match:
                        logger.info("[guard] session %s: no guard word, discarding", session_id)
                        return
                    # Strip guard word + everything before it
                    after = transcript[match[1]:].strip()
                    if not after:
                        return  # guard word alone, nothing to submit
                    transcript = after

                await self._ws_bridge.submit_user_message(session_id, transcript)
            elif event_type == "voice_was_detected":
                logger.info("[stt] session %s: voice detected — barge-in", session_id)
                self.barge_in(session_id)
            elif event_type == "voice_not_detected":
                logger.debug("[stt] session %s: voice ended", session_id)

        return _on_stt_event

    async def _speak_short(self, session_id: str, phrase: str) -> None:
        """Speak a short acknowledgment phrase via TTS (e.g. 'Guard on')."""
        track = self.get_outgoing_track(session_id)
        if not track:
            return
        try:
            self.mute_stt(session_id)
            from server.tts import TTS_OpenAI
            tts = TTS_OpenAI(opus_frame_handler=track.push_opus_frame)
            await tts.say(phrase)
        except Exception:
            logger.exception("[guard] TTS failed for session %s phrase=%r", session_id, phrase)
        finally:
            self.unmute_stt(session_id)

    async def _consume_audio(
        self,
        session_id: str,
        track: MediaStreamTrack,
        stats: AudioStatsLogger,
    ) -> None:
        """Read frames from *track*, log stats, and feed to STT."""
        stt = self._stt_instances.get(session_id)
        audio_buffer: list[np.ndarray] = []

        try:
            while True:
                frame = await track.recv()
                stats.log_frame(frame)

                if stt and session_id not in self._stt_muted:
                    pcm = frame.to_ndarray()  # shape (channels, 960) s16
                    audio_buffer.append(pcm)
                    if len(audio_buffer) >= 8:  # ~160ms batch
                        batch = np.concatenate(audio_buffer, axis=1)
                        # Log first batch to confirm STT is receiving audio.
                        if not hasattr(self, '_stt_logged'):
                            self._stt_logged = set()
                        if session_id not in self._stt_logged:
                            self._stt_logged.add(session_id)
                            rms = float(np.sqrt(np.mean(batch.astype(np.float64)**2)))
                            logger.info(
                                "[webrtc] First STT batch for session %s: shape=%s, rms=%.1f",
                                session_id, batch.shape, rms,
                            )
                        stt.process_buffer(batch)
                        audio_buffer = []
        except Exception:
            # Flush any remaining audio to STT on track end.
            if audio_buffer and stt:
                try:
                    batch = np.concatenate(audio_buffer, axis=1)
                    stt.process_buffer(batch)
                    stt.flush()
                except Exception:
                    pass
            logger.info(
                "[webrtc] incoming audio track ended for session %s", session_id
            )

    async def close_connection(self, session_id: str) -> None:
        """Close and remove the peer connection for *session_id*."""
        pc = self._connections.pop(session_id, None)
        self._stats.pop(session_id, None)
        self._outgoing_tracks.pop(session_id, None)
        self._guard_enabled.pop(session_id, None)
        self._tts_muted.pop(session_id, None)

        # Clean up STT.
        stt = self._stt_instances.pop(session_id, None)
        if stt:
            stt.flush()
            stt.stop()

        if pc is not None:
            await pc.close()
            logger.info("[webrtc] closed connection for session %s", session_id)

    async def close_all(self) -> None:
        """Close every active peer connection."""
        session_ids = list(self._connections.keys())
        for session_id in session_ids:
            await self.close_connection(session_id)
