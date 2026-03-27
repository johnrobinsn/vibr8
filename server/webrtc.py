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
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from av import AudioFrame

from server.audio_track import QueuedAudioTrack
from server.stt import AsyncSTT, STTParams
from server.voice_logger import VoiceLogger
from server import voice_profiles

logger = logging.getLogger(__name__)


# ── Voice Modes ──────────────────────────────────────────────────────────────

class VoiceMode:
    """Base class for voice modes that intercept transcripts."""

    name: str = "unknown"

    def on_transcript(self, text: str) -> str | None:
        """Process a transcript. Return text to submit, or None to suppress."""
        return text

    def on_done(self) -> str | None:
        """Called when the user says 'done'. Return accumulated text or None."""
        return None

    def on_disconnect(self) -> str | None:
        """Called when audio disconnects. Return accumulated text or None."""
        return None


class NoteMode(VoiceMode):
    """Accumulates transcript fragments into a voice note."""

    name = "note"

    def __init__(self) -> None:
        self._fragments: list[str] = []

    def on_transcript(self, text: str) -> str | None:
        self._fragments.append(text)
        logger.info("[note-mode] fragment %d: %s", len(self._fragments), text)
        return None  # suppress submission

    def on_done(self) -> str | None:
        if not self._fragments:
            return None
        return "[voice note]\n" + "\n".join(self._fragments)

    def on_disconnect(self) -> str | None:
        if not self._fragments:
            return None
        return "[voice note interrupted]\n" + "\n".join(self._fragments)


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

    def __init__(self, ice_servers: list[dict] | None = None) -> None:
        self._ice_servers = ice_servers or []
        self._connections: Dict[str, RTCPeerConnection] = {}
        self._stats: Dict[str, AudioStatsLogger] = {}
        self._outgoing_tracks: Dict[str, QueuedAudioTrack] = {}
        self._stt_instances: Dict[str, AsyncSTT] = {}
        self._stt_muted: set[str] = set()
        self._guard_enabled: Dict[str, bool] = {}
        self._tts_muted: Dict[str, bool] = {}
        self._client_ids: Dict[str, str] = {}  # session_id → client_id
        self._voice_loggers: Dict[str, VoiceLogger] = {}
        self._voice_modes: Dict[str, VoiceMode] = {}
        self._playground_ws: Dict[str, object] = {}  # client_id → WS
        self._playground_sessions: set[str] = set()  # session_ids that are playground
        self._ws_bridge = None
        self._ring0_manager = None
        self._launcher = None  # Set by main.py for Ring0 lazy-create
        self._node_registry = None  # Set by main.py for distributed nodes

    def get_client_ice_servers(self) -> list[dict]:
        """Return ICE servers in the format the browser RTCPeerConnection expects."""
        return self._ice_servers

    def set_ws_bridge(self, bridge) -> None:
        """Set a reference to WsBridge for submitting STT transcripts."""
        self._ws_bridge = bridge

    def set_ring0_manager(self, manager) -> None:
        """Set a reference to Ring0Manager for voice routing."""
        self._ring0_manager = manager

    def set_launcher(self, launcher) -> None:
        """Set a reference to CliLauncher for Ring0 lazy session creation."""
        self._launcher = launcher

    def set_node_registry(self, registry) -> None:
        """Set a reference to NodeRegistry for distributed node routing."""
        self._node_registry = registry

    def has_active_connections(self) -> bool:
        """Return True if any WebRTC connections are active."""
        return bool(self._connections)

    def get_outgoing_track(self, session_id: str) -> QueuedAudioTrack | None:
        """Return the outgoing audio track for *session_id*, or None."""
        return self._outgoing_tracks.get(session_id)

    def get_any_outgoing_track(self) -> tuple[str, QueuedAudioTrack] | None:
        """Return (session_id, track) for any active outgoing track, or None.

        Only one WebRTC session is active at a time, so this is used as a
        fallback when the responding session (e.g. Ring0) doesn't own the
        audio connection directly.
        """
        for sid, track in self._outgoing_tracks.items():
            return (sid, track)
        return None

    def _resolve_track(self, session_id: str) -> QueuedAudioTrack | None:
        """Look up the outgoing track for *session_id*, falling back to any active track.

        Only one WebRTC audio session is active at a time. When Ring0
        responds, the track may be registered under a different session.
        """
        track = self._outgoing_tracks.get(session_id)
        if not track:
            fallback = self.get_any_outgoing_track()
            if fallback:
                track = fallback[1]
        return track

    def barge_in(self, session_id: str) -> None:
        """Handle barge-in: clear queued TTS audio and cancel the TTS stream."""
        track = self._resolve_track(session_id)
        if track:
            track.clear_audio()
            track.set_thinking(False)
        if self._ws_bridge:
            self._ws_bridge.cancel_tts(session_id)
        logger.info("[webrtc] barge-in for session %s: audio cleared", session_id)

    def set_thinking(self, session_id: str, thinking: bool) -> None:
        """Enable or disable the thinking-tone for *session_id*."""
        track = self._resolve_track(session_id)
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

    def get_voice_mode(self, session_id: str) -> VoiceMode | None:
        """Return the active voice mode for *session_id*, or None."""
        return self._voice_modes.get(session_id)

    def set_voice_mode(self, session_id: str, mode: VoiceMode | None) -> None:
        """Set or clear the voice mode for *session_id*."""
        if mode is None:
            self._voice_modes.pop(session_id, None)
            logger.info("[voice-mode] session %s: mode cleared", session_id)
        else:
            self._voice_modes[session_id] = mode
            logger.info("[voice-mode] session %s: entered %s mode", session_id, mode.name)
        # Broadcast to browser — send to both the WebRTC session and the
        # ring0 session (if active), since the browser may only have a WS
        # connected to whichever session it's currently viewing.
        if self._ws_bridge:
            mode_name = mode.name if mode else None
            asyncio.ensure_future(
                self._ws_bridge.broadcast_voice_mode(session_id, mode_name)
            )
            if self._ring0_manager and self._ring0_manager.is_enabled:
                ring0_sid = self._ring0_manager.session_id
                if ring0_sid and ring0_sid != session_id:
                    asyncio.ensure_future(
                        self._ws_bridge.broadcast_voice_mode(ring0_sid, mode_name)
                    )

        # Mute/unmute Ring0 TTS during note mode so it doesn't talk over dictation
        if self._ring0_manager and self._ring0_manager.is_enabled:
            is_note = isinstance(mode, NoteMode)
            pair = self.get_any_outgoing_track()
            if pair:
                audio_sid, _ = pair
                self.set_tts_muted(audio_sid, is_note)

    async def handle_offer(
        self, session_id: str, sdp: str, sdp_type: str, client_id: str = "",
        playground: bool = False, profile_id: str | None = None,
        username: str = "default",
    ) -> dict[str, str]:
        """Process an SDP offer and return an SDP answer.

        Creates a :class:`QueuedAudioTrack` for outgoing TTS audio and
        an :class:`AsyncSTT` instance for incoming speech recognition.

        If *playground* is True, STT events go to the playground WS
        instead of the agent session.
        """
        # Tear down any pre-existing connection for this session.
        if session_id in self._connections:
            await self.close_connection(session_id)

        if client_id:
            self._client_ids[session_id] = client_id

        if playground:
            self._playground_sessions.add(session_id)

        # Build ICE server list from config (empty = local network only).
        ice_server_objs = []
        for srv in self._ice_servers:
            ice_server_objs.append(RTCIceServer(
                urls=srv["urls"],
                username=srv.get("username"),
                credential=srv.get("credential"),
            ))
        pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_server_objs))
        self._connections[session_id] = pc

        # Resolve voice profile → STT params
        stt_params = voice_profiles.get_stt_params(username, profile_id)

        # Create STT instance for this session.
        # aiortc typically delivers stereo (2-channel) audio even if source is mono.
        stt = AsyncSTT(sample_rate=48000, num_channels=2, params=stt_params)

        if playground:
            stt.add_listener(self._make_playground_listener(session_id, client_id))
        else:
            stt.add_listener(self._make_stt_listener(session_id, client_id))

        self._stt_instances[session_id] = stt

        # Create voice logger for audio persistence
        vl = VoiceLogger(username, session_id)
        self._voice_loggers[session_id] = vl
        await vl.start_recording()

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

        # Wait for ICE gathering to complete so relay candidates (TURN) are
        # included in the answer SDP.  Without this the answer may only
        # contain host candidates (private IPs) that are unreachable from
        # mobile networks.
        if pc.iceGatheringState != "complete":
            gathering_done = asyncio.Event()

            @pc.on("icegatheringstatechange")
            def _on_ice_gathering_state_change() -> None:
                if pc.iceGatheringState == "complete":
                    gathering_done.set()

            try:
                await asyncio.wait_for(gathering_done.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "[webrtc] session %s: ICE gathering timed out (state=%s)",
                    session_id,
                    pc.iceGatheringState,
                )

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

    def _make_stt_listener(self, session_id: str, client_id: str = ""):
        """Create an STT event listener that submits transcripts to the agent."""

        async def _on_stt_event(stt, event_type: str, data) -> None:
            if event_type == "final_transcript":
                transcript = data["transcript"].strip()
                logger.info("[stt] session %s transcript: %s", session_id, transcript)

                # Log segment audio if available
                audio = data.get("audio")
                voice_log = self._voice_loggers.get(session_id)
                if voice_log and audio is not None:
                    try:
                        await voice_log.log_segment(audio, data)
                    except Exception:
                        logger.exception("[voice-log] Failed to log segment")

                if not transcript or not self._ws_bridge:
                    return

                # Helper to submit text through Ring0 or active session.
                # When a remote node is active and Ring0 is enabled, forward
                # the transcript to the remote node's Ring0 via the WS tunnel.
                async def _submit_text(text: str) -> None:
                    if self._ring0_manager and self._ring0_manager.is_enabled:
                        # Check if a remote node is active — route to its Ring0
                        if self._node_registry:
                            active_nid = self._node_registry.active_node_id
                            node = self._node_registry.get_node(active_nid)
                            if node and node.tunnel and node.status == "online":
                                    node.tunnel.send_fire_and_forget({
                                        "type": "ring0_input",
                                        "text": text,
                                        "sourceClientId": client_id,
                                    })
                                    return
                        # Local Ring0
                        r0sid = self._ring0_manager.session_id
                        if not r0sid and self._launcher and self._ws_bridge:
                            r0sid = await self._ring0_manager.ensure_session(self._launcher, self._ws_bridge)
                        if r0sid:
                            await self._ws_bridge.submit_user_message(r0sid, text, source_client_id=client_id)
                            return
                    await self._ws_bridge.submit_user_message(session_id, text, source_client_id=client_id)

                transcript_lower = transcript.lower()

                # Check for "vibr8/vibrate <command>" regardless of guard state.
                # Only strip the guard word if a known command follows it.
                # If no command matches, pass the entire transcript through unmodified.
                guard_word_found = False
                match = self._find_guard_word(transcript_lower)
                if match:
                    guard_word_found = True
                    after_word = transcript_lower[match[1]:].strip().lstrip(".,;:!?- ").strip()
                    pre_text = transcript[:match[0]].strip()

                    # "done" is always honored — even inside voice modes (e.g. note mode).
                    if after_word.startswith("done"):
                        mode = self.get_voice_mode(session_id)
                        if not mode:
                            asyncio.ensure_future(self._speak_short(session_id, "No active mode"))
                            return
                        # Add pre-text as a final note fragment (it was part of dictation)
                        if pre_text:
                            mode.on_transcript(pre_text)
                        result = mode.on_done()
                        self.set_voice_mode(session_id, None)
                        if not result:
                            asyncio.ensure_future(self._speak_short(session_id, "Empty note"))
                            return
                        asyncio.ensure_future(self._speak_short(session_id, "Done"))
                        # Deliver via ring0 if enabled, otherwise to active session
                        if self._ring0_manager and self._ring0_manager.is_enabled:
                            ring0_sid = self._ring0_manager.session_id
                            if ring0_sid:
                                await self._ws_bridge.submit_user_message(ring0_sid, result, source_client_id=client_id)
                                if isinstance(mode, NoteMode):
                                    from server.ring0_events import Ring0Event
                                    await self._ws_bridge.emit_ring0_event(Ring0Event(fields={"type": "note_mode_ended"}))
                                return
                        await self._ws_bridge.submit_user_message(session_id, result, source_client_id=client_id)
                        return

                    # In voice mode (e.g. note mode), only "done" is recognized.
                    # All other guard-prefixed speech falls through to fragment capture.
                    if not self.get_voice_mode(session_id):
                        if after_word.startswith("off"):
                            if pre_text:
                                await _submit_text(pre_text)
                            if self._ws_bridge:
                                asyncio.ensure_future(
                                    self._ws_bridge.broadcast_audio_off(session_id)
                                )
                            return
                        if after_word.startswith("guard"):
                            if pre_text:
                                await _submit_text(pre_text)
                            self.set_guard_enabled(session_id, True)
                            asyncio.ensure_future(self._speak_short(session_id, "Guard on"))
                            return
                        if after_word.startswith("listen"):
                            if pre_text:
                                await _submit_text(pre_text)
                            self.set_guard_enabled(session_id, False)
                            asyncio.ensure_future(self._speak_short(session_id, "Listening"))
                            return
                        if after_word.startswith("quiet"):
                            if pre_text:
                                await _submit_text(pre_text)
                            self.set_tts_muted(session_id, True)
                            asyncio.ensure_future(self._speak_short(session_id, "Quiet mode"))
                            return
                        if after_word.startswith("speak"):
                            if pre_text:
                                await _submit_text(pre_text)
                            self.set_tts_muted(session_id, False)
                            asyncio.ensure_future(self._speak_short(session_id, "Speaking"))
                            return
                        if after_word.startswith("ring zero on") or after_word.startswith("ring 0 on"):
                            if pre_text:
                                await _submit_text(pre_text)
                            if self._ring0_manager:
                                self._ring0_manager.enable()
                                asyncio.ensure_future(self._speak_short(session_id, "Ring zero on"))
                            return
                        if after_word.startswith("ring zero off") or after_word.startswith("ring 0 off"):
                            if pre_text:
                                await _submit_text(pre_text)
                            if self._ring0_manager:
                                self._ring0_manager.disable()
                                asyncio.ensure_future(self._speak_short(session_id, "Ring zero off"))
                            return
                        if after_word.startswith("note"):
                            if pre_text:
                                await _submit_text(pre_text)
                            existing = self.get_voice_mode(session_id)
                            if existing and existing.name == "note":
                                asyncio.ensure_future(self._speak_short(session_id, "Already in note mode"))
                            else:
                                self.set_voice_mode(session_id, NoteMode())
                                asyncio.ensure_future(self._speak_short(session_id, "Note mode"))
                            return
                        if after_word.startswith("node"):
                            if pre_text:
                                await _submit_text(pre_text)
                            await self._handle_node_switch_command(session_id, after_word[4:].strip())
                            return
                        # Escape sequences: submit pre-text, then transform and
                        # fall through to submit the transformed text.
                        # "vibr8 vibrate ..." → "vibrate ..."
                        if after_word.startswith("vibrate"):
                            if pre_text:
                                await _submit_text(pre_text)
                            remaining = transcript[match[1]:].strip().lstrip(".,;:!?- ")
                            remaining = remaining[7:].strip() if remaining.lower().startswith("vibrate") else remaining
                            transcript = "vibrate" + (" " + remaining if remaining else "")
                        # "vibr8 app ..." → "vibr8 ..."
                        elif after_word.startswith("app"):
                            if pre_text:
                                await _submit_text(pre_text)
                            remaining = transcript[match[1]:].strip()
                            remaining = remaining[3:].strip() if remaining.lower().startswith("app") else remaining
                            transcript = "vibr8" + (" " + remaining if remaining else "")

                    # No command matched — transcript passes through unmodified
                    # (guard_word_found is True so guard mode still allows it)

                # Voice mode interception: route to active mode instead of submitting.
                mode = self.get_voice_mode(session_id)
                if mode:
                    mode.on_transcript(transcript)
                    return

                # If guard is enabled, require guard word
                if self.is_guard_enabled(session_id):
                    if not guard_word_found:
                        logger.info("[guard] session %s: no guard word, discarding", session_id)
                        return

                await _submit_text(transcript)
            elif event_type == "voice_was_detected":
                logger.info("[stt] session %s: voice detected — barge-in", session_id)
                self.barge_in(session_id)
            elif event_type == "voice_not_detected":
                logger.debug("[stt] session %s: voice ended", session_id)

        return _on_stt_event

    def _make_playground_listener(self, session_id: str, client_id: str = ""):
        """Create an STT event listener that sends events to the playground WS."""

        async def _on_playground_event(stt, event_type: str, data) -> None:
            ws = self._playground_ws.get(client_id)
            if not ws:
                return

            import json as _json

            if event_type == "voice_level":
                try:
                    await ws.send_str(_json.dumps({
                        "type": "voice_level",
                        "rmsDb": data["rmsDb"],
                    }))
                except Exception:
                    pass
            elif event_type == "voice_was_detected":
                try:
                    await ws.send_str(_json.dumps({"type": "voice_activity", "active": True}))
                except Exception:
                    pass
            elif event_type == "voice_not_detected":
                try:
                    await ws.send_str(_json.dumps({"type": "voice_activity", "active": False}))
                except Exception:
                    pass
            elif event_type == "final_transcript":
                transcript = data["transcript"].strip()
                if not transcript:
                    return
                # Save segment audio and get segment ID for playback
                segment_id = None
                audio = data.get("audio")
                if audio is not None:
                    vl = self._voice_loggers.get(session_id)
                    if vl:
                        segment_id = await vl.log_segment(audio, data)
                try:
                    await ws.send_str(_json.dumps({
                        "type": "segment",
                        "transcript": transcript,
                        "timeBegin": data.get("timeBegin", 0),
                        "timeEnd": data.get("timeEnd", 0),
                        "segmentId": segment_id,
                    }))
                except Exception:
                    pass

        return _on_playground_event

    def register_playground_ws(self, client_id: str, ws) -> None:
        """Register a playground WebSocket for a client."""
        self._playground_ws[client_id] = ws

    def unregister_playground_ws(self, client_id: str) -> None:
        """Unregister a playground WebSocket."""
        self._playground_ws.pop(client_id, None)

    def update_stt_params(self, session_id: str, params: STTParams) -> None:
        """Update STT params for a live session (e.g. from playground slider)."""
        stt = self._stt_instances.get(session_id)
        if stt:
            stt.update_params(params)

    async def _speak_short(self, session_id: str, phrase: str) -> None:
        """Speak a short acknowledgment phrase via TTS (e.g. 'Guard on').

        Respects the TTS mute setting — if the user has switched to
        mic-only mode, acknowledgments are also silenced.
        """
        track = self.get_outgoing_track(session_id)
        if not track or self.is_tts_muted(session_id):
            return
        try:
            from server.tts import TTS_OpenAI
            tts = TTS_OpenAI(opus_frame_handler=track.push_opus_frame)
            await tts.say(phrase)
        except Exception:
            logger.exception("[guard] TTS failed for session %s phrase=%r", session_id, phrase)

    async def _handle_node_switch_command(self, session_id: str, node_name: str) -> None:
        """Handle 'vibr8 node <name>' voice command to switch active node."""
        if not self._node_registry:
            asyncio.ensure_future(self._speak_short(session_id, "No nodes available"))
            return

        node_name = node_name.strip()
        if not node_name:
            asyncio.ensure_future(self._speak_short(session_id, "Which node?"))
            return

        # "local", "hub", or the hub's configured name switches back to hub
        name_lower = node_name.lower()
        local_node = self._node_registry.local_node
        hub_name = local_node.name
        is_hub = name_lower in ("local", "hub") or name_lower in hub_name.lower()
        if is_hub:
            if self._node_registry.active_node_id == local_node.id:
                asyncio.ensure_future(self._speak_short(session_id, f"Already on {hub_name}"))
                return
            self._node_registry.active_node_id = local_node.id
            logger.info("[voice] Switched to local node (%s) via voice command", hub_name)
            asyncio.ensure_future(self._speak_short(session_id, f"Switched to {hub_name}"))
            if self._ws_bridge:
                asyncio.ensure_future(
                    self._ws_bridge.broadcast_node_switch(local_node.id, hub_name)
                )
            return

        matches = self._node_registry.find_by_name(node_name)
        if not matches:
            asyncio.ensure_future(self._speak_short(session_id, f"No node named {node_name} found"))
            return
        if len(matches) > 1:
            names = ", ".join(m.name for m in matches[:3])
            asyncio.ensure_future(self._speak_short(session_id, f"Multiple nodes match: {names}"))
            return

        target = matches[0]
        if self._node_registry.active_node_id == target.id:
            asyncio.ensure_future(self._speak_short(session_id, f"Already on {target.name}"))
            return
        if target.status != "online":
            asyncio.ensure_future(self._speak_short(session_id, f"{target.name} is offline"))
            return

        self._node_registry.active_node_id = target.id
        logger.info("[voice] Switched to node %r (id=%s) via voice command", target.name, target.id[:8])
        asyncio.ensure_future(self._speak_short(session_id, f"Switched to node {target.name}"))
        if self._ws_bridge:
            asyncio.ensure_future(
                self._ws_bridge.broadcast_node_switch(target.id, target.name)
            )

    async def _consume_audio(
        self,
        session_id: str,
        track: MediaStreamTrack,
        stats: AudioStatsLogger,
    ) -> None:
        """Read frames from *track*, log stats, and feed to STT."""
        stt = self._stt_instances.get(session_id)
        audio_buffer: list[np.ndarray] = []

        voice_log = self._voice_loggers.get(session_id)

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

                        # Log raw stereo 48kHz audio to voice logger
                        if voice_log:
                            try:
                                # batch shape: (channels, N) — interleave for WAV
                                interleaved = batch.T.flatten().astype(np.int16)
                                await voice_log.log_chunk(interleaved)
                            except Exception:
                                pass

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
            # Stop voice logger recording
            if voice_log:
                try:
                    await voice_log.stop_recording()
                except Exception:
                    pass
            logger.info(
                "[webrtc] incoming audio track ended for session %s", session_id
            )

    async def close_connection(self, session_id: str) -> None:
        """Close and remove the peer connection for *session_id*."""
        # Flush active voice mode (deliver interrupted note)
        mode = self._voice_modes.pop(session_id, None)
        if mode and self._ws_bridge:
            result = mode.on_disconnect()
            if result:
                client_id_for_mode = self._client_ids.get(session_id, "")
                logger.info("[voice-mode] session %s: flushing on disconnect", session_id)
                if self._ring0_manager and self._ring0_manager.is_enabled and self._ring0_manager.session_id:
                    await self._ws_bridge.submit_user_message(self._ring0_manager.session_id, result, source_client_id=client_id_for_mode)
                else:
                    await self._ws_bridge.submit_user_message(session_id, result, source_client_id=client_id_for_mode)

        pc = self._connections.pop(session_id, None)
        self._stats.pop(session_id, None)
        self._outgoing_tracks.pop(session_id, None)
        self._guard_enabled.pop(session_id, None)
        self._tts_muted.pop(session_id, None)
        client_id = self._client_ids.pop(session_id, None)
        self._playground_sessions.discard(session_id)

        # Clean up playground WS reference
        if client_id:
            self._playground_ws.pop(client_id, None)

        # Clean up STT.
        stt = self._stt_instances.pop(session_id, None)
        if stt:
            stt.flush()
            stt.stop()

        # Clean up voice logger.
        voice_log = self._voice_loggers.pop(session_id, None)
        if voice_log:
            try:
                await voice_log.stop_recording()
            except Exception:
                pass

        if pc is not None:
            await pc.close()
            logger.info("[webrtc] closed connection for session %s", session_id)

    async def close_all(self) -> None:
        """Close every active peer connection."""
        session_ids = list(self._connections.keys())
        for session_id in session_ids:
            await self.close_connection(session_id)
