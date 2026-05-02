"""WebRTC module — manages per-client RTCPeerConnection instances using aiortc.

Provides audio streaming capabilities for vibr8 clients, including
STT (speech-to-text) for incoming audio and a queued audio track for
outgoing TTS audio.  All audio state is keyed by client_id, not session_id,
so connections survive session switches.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import shutil
import subprocess
import sys
import time
from typing import Dict

import numpy as np
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from av import AudioFrame

from aiortc.codecs import h264 as _h264_codec

from server.audio_track import QueuedAudioTrack
from server.input_injector import InputInjector
from server.screen_capture import ScreenCapture, NoDisplayError
from server.stt import AsyncSTT, STTParams
from server.video_track import ScreenShareTrack
from server.voice_logger import VoiceLogger

# Raise H.264 bitrate limits for screen content (sharp text/UI edges)
_h264_codec.DEFAULT_BITRATE = 4_000_000  # 4 Mbps (was 1 Mbps)
_h264_codec.MAX_BITRATE = 8_000_000      # 8 Mbps (was 3 Mbps)
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
        self, label: str, direction: str, interval: float = 5.0
    ) -> None:
        self.label = label
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
                "[webrtc] %s audio for client %s: %d frames, "
                "peak_level=%.1f, sample_rate=%d, channels=%d",
                self.direction,
                self.label,
                self._frame_count,
                self._peak_rms,
                self._sample_rate,
                self._channels,
            )
            # Reset counters for the next interval.
            self._frame_count = 0
            self._peak_rms = 0.0
            self._last_log_time = now


# ── Clipboard helpers ────────────────────────────────────────────────────────

_IS_LINUX = sys.platform.startswith("linux")
_IS_MACOS = sys.platform == "darwin"


async def _handle_clipboard_get(channel) -> None:
    """Read the remote clipboard and send it back on the data channel."""
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, _clipboard_read)
    if channel.readyState == "open":
        channel.send(_json.dumps({"type": "clipboard", "text": text}))


async def _handle_clipboard_set(text: str) -> None:
    """Write text to the remote clipboard."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _clipboard_write, text)


def _clipboard_read() -> str:
    """Read clipboard contents (blocking)."""
    try:
        if _IS_MACOS:
            return subprocess.check_output(["pbpaste"], timeout=3).decode("utf-8", errors="replace")
        elif _IS_LINUX and shutil.which("xclip"):
            return subprocess.check_output(
                ["xclip", "-o", "-selection", "clipboard"],
                timeout=3,
                env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":99")},
            ).decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("[webrtc] clipboard read failed: %s", exc)
    return ""


def _clipboard_write(text: str) -> None:
    """Write text to clipboard (blocking)."""
    try:
        if _IS_MACOS:
            subprocess.run(["pbcopy"], input=text.encode(), timeout=3, check=True)
        elif _IS_LINUX and shutil.which("xclip"):
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=text.encode(),
                timeout=3,
                check=True,
                env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":99")},
            )
    except Exception as exc:
        logger.warning("[webrtc] clipboard write failed: %s", exc)


class WebRTCManager:
    """Manages per-client RTCPeerConnection instances with STT and TTS audio.

    All audio state is keyed by ``client_id`` so that connections survive
    session switches.  Transcript routing is resolved dynamically at
    delivery time via ``ws_bridge._client_sessions``.
    """

    def __init__(self, ice_servers: list[dict] | None = None) -> None:
        self._ice_servers = ice_servers or []
        # All dicts keyed by client_id
        self._connections: Dict[str, RTCPeerConnection] = {}
        self._stats: Dict[str, AudioStatsLogger] = {}
        self._outgoing_tracks: Dict[str, QueuedAudioTrack] = {}
        self._video_tracks: Dict[str, ScreenShareTrack] = {}
        self._screen_captures: Dict[str, ScreenCapture] = {}
        self._stt_instances: Dict[str, AsyncSTT] = {}
        self._stt_muted: set[str] = set()  # client_ids with STT muted
        self._guard_enabled: Dict[str, bool] = {}
        self._tts_muted: Dict[str, bool] = {}
        self._voice_loggers: Dict[str, VoiceLogger] = {}
        self._voice_modes: Dict[str, VoiceMode] = {}
        self._input_injectors: Dict[str, InputInjector] = {}
        self._playground_ws: Dict[str, object] = {}  # client_id → WS
        self._playground_clients: set[str] = set()  # client_ids that are playground
        self._enrollment_ws: Dict[str, object] = {}  # client_id → enrollment WS
        self._enrollment_listeners: Dict[str, object] = {}  # client_id → listener fn
        self._client_usernames: Dict[str, str] = {}  # client_id → username
        self._client_speaker_gates: Dict[str, tuple[str, float]] = {}  # client_id → (speaker_name, threshold)
        # Shared screen capture: one per display, ref-counted across viewers
        self._shared_captures: Dict[str, ScreenCapture] = {}  # display → capture
        self._shared_capture_refs: Dict[str, set[str]] = {}  # display → {client_ids}
        self._desktop_viewers: set[str] = set()  # client_ids that are view-only
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

    def get_outgoing_track(self, client_id: str) -> QueuedAudioTrack | None:
        """Return the outgoing audio track for *client_id*, or None."""
        return self._outgoing_tracks.get(client_id)

    def get_any_outgoing_track(self) -> tuple[str, QueuedAudioTrack] | None:
        """Return (client_id, track) for any active outgoing track, or None.

        Only one WebRTC connection is active at a time, so this is used as a
        fallback when the responding session (e.g. Ring0) doesn't own the
        audio connection directly.
        """
        for cid, track in self._outgoing_tracks.items():
            return (cid, track)
        return None

    def _resolve_track(self, client_id: str) -> QueuedAudioTrack | None:
        """Look up the outgoing track for *client_id*, falling back to any active track.

        Only one WebRTC audio connection is active at a time. When Ring0
        responds, the track may be registered under a different client.
        """
        track = self._outgoing_tracks.get(client_id)
        if not track:
            fallback = self.get_any_outgoing_track()
            if fallback:
                track = fallback[1]
        return track

    def barge_in(self, client_id: str) -> None:
        """Handle barge-in: clear queued TTS audio and cancel the TTS stream."""
        track = self._resolve_track(client_id)
        if track:
            track.clear_audio()
            track.set_thinking(False)
        if self._ws_bridge:
            # Cancel TTS for the client's current session
            session_id = self._current_session_for(client_id)
            if session_id:
                self._ws_bridge.cancel_tts(session_id)
        logger.info("[webrtc] barge-in for client %s: audio cleared", client_id)

    def barge_in_any(self) -> None:
        """Barge-in on whatever WebRTC connection is active (convenience for callers without client_id)."""
        for cid in self._connections:
            self.barge_in(cid)
            return

    def set_thinking(self, client_id: str, thinking: bool) -> None:
        """Enable or disable the thinking-tone for *client_id*."""
        track = self._resolve_track(client_id)
        if track:
            track.set_thinking(thinking)

    def set_thinking_any(self, thinking: bool) -> None:
        """Set thinking on whatever WebRTC connection is active (convenience for callers without client_id)."""
        for cid in self._connections:
            self.set_thinking(cid, thinking)
            return

    def mute_stt(self, client_id: str) -> None:
        """Suppress STT processing (prevents echo from triggering barge-in)."""
        self._stt_muted.add(client_id)

    def unmute_stt(self, client_id: str) -> None:
        """Re-enable STT processing."""
        self._stt_muted.discard(client_id)

    def is_guard_enabled(self, client_id: str) -> bool:
        """Return whether guard mode is active for *client_id*."""
        return self._guard_enabled.get(client_id, True)

    def set_guard_enabled(self, client_id: str, enabled: bool) -> None:
        """Enable or disable guard mode for *client_id*."""
        self._guard_enabled[client_id] = enabled
        logger.info("[guard] client %s: guard mode %s", client_id, "ON" if enabled else "OFF")
        # Broadcast to browser so UI stays in sync
        if self._ws_bridge:
            asyncio.ensure_future(
                self._ws_bridge.broadcast_guard_state("", enabled, client_id=client_id)
            )

    def is_tts_muted(self, client_id: str) -> bool:
        """Return whether TTS is muted for *client_id*."""
        return self._tts_muted.get(client_id, False)

    def set_tts_muted(self, client_id: str, muted: bool) -> None:
        """Mute or unmute TTS for *client_id*."""
        self._tts_muted[client_id] = muted
        logger.info("[webrtc] client %s: TTS %s", client_id, "muted" if muted else "unmuted")
        if self._ws_bridge:
            asyncio.ensure_future(
                self._ws_bridge.broadcast_tts_muted("", muted, client_id=client_id)
            )

    def refresh_speaker_gates(self, username: str) -> None:
        """Re-resolve per-client speaker gates after profile changes."""
        from server.speaker_fingerprints import get_embeddings_for_speaker
        for cid, uname in self._client_usernames.items():
            if uname != username:
                continue
            gate = self._client_speaker_gates.get(cid)
            if not gate:
                continue
            speaker_name, threshold = gate
            stt = self._stt_instances.get(cid)
            if not stt:
                continue
            embeddings = get_embeddings_for_speaker(username, speaker_name)
            if embeddings:
                stt.set_speaker_gate(embeddings, threshold)
                logger.info("[webrtc] client %s: speaker gate refreshed (threshold=%.3f, %d embeddings)", cid, threshold, len(embeddings))
            else:
                stt.clear_speaker_gate()
                self._client_speaker_gates.pop(cid, None)
                logger.info("[webrtc] client %s: speaker gate cleared (profile '%s' no longer exists)", cid, speaker_name)

    def set_speaker_gate_for_client(self, client_id: str, speaker_name: str | None,
                                    threshold: float, username: str) -> bool:
        """Set or clear speaker gate for a specific client. Returns True if applied."""
        stt = self._stt_instances.get(client_id)
        if not stt:
            return False
        if not speaker_name:
            stt.clear_speaker_gate()
            self._client_speaker_gates.pop(client_id, None)
            logger.info("[webrtc] client %s: speaker gate cleared", client_id)
            return True
        from server.speaker_fingerprints import get_embeddings_for_speaker
        embeddings = get_embeddings_for_speaker(username, speaker_name)
        if not embeddings:
            stt.clear_speaker_gate()
            self._client_speaker_gates.pop(client_id, None)
            logger.info("[webrtc] client %s: speaker '%s' not found, gate cleared", client_id, speaker_name)
            return False
        stt.set_speaker_gate(embeddings, threshold)
        self._client_speaker_gates[client_id] = (speaker_name, threshold)
        logger.info("[webrtc] client %s: speaker gate set (speaker='%s', threshold=%.3f, %d embeddings)",
                    client_id, speaker_name, threshold, len(embeddings))
        return True

    def get_voice_mode(self, client_id: str) -> VoiceMode | None:
        """Return the active voice mode for *client_id*, or None."""
        return self._voice_modes.get(client_id)

    def set_voice_mode(self, client_id: str, mode: VoiceMode | None) -> None:
        """Set or clear the voice mode for *client_id*."""
        if mode is None:
            self._voice_modes.pop(client_id, None)
            logger.info("[voice-mode] client %s: mode cleared", client_id)
        else:
            self._voice_modes[client_id] = mode
            logger.info("[voice-mode] client %s: entered %s mode", client_id, mode.name)
        # Broadcast to browser — send to both the WebRTC client and the
        # ring0 session (if active), since the browser may only have a WS
        # connected to whichever session it's currently viewing.
        if self._ws_bridge:
            mode_name = mode.name if mode else None
            asyncio.ensure_future(
                self._ws_bridge.broadcast_voice_mode("", mode_name, client_id=client_id)
            )
            if self._ring0_manager and self._ring0_manager.is_enabled:
                ring0_sid = self._ring0_manager.session_id
                if ring0_sid:
                    asyncio.ensure_future(
                        self._ws_bridge.broadcast_voice_mode(ring0_sid, mode_name)
                    )

        # Mute/unmute Ring0 TTS during note mode so it doesn't talk over dictation
        if self._ring0_manager and self._ring0_manager.is_enabled:
            is_note = isinstance(mode, NoteMode)
            pair = self.get_any_outgoing_track()
            if pair:
                audio_cid, _ = pair
                self.set_tts_muted(audio_cid, is_note)

    def _current_session_for(self, client_id: str) -> str | None:
        """Look up the session a client is currently viewing."""
        if self._ws_bridge:
            return self._ws_bridge._client_sessions.get(client_id)
        return None

    async def handle_offer(
        self, client_id: str, sdp: str, sdp_type: str, session_id: str = "",
        playground: bool = False, profile_id: str | None = None,
        username: str = "default", desktop: bool = False,
        desktop_role: str = "controller",
        speaker_gate_name: str | None = None,
        speaker_gate_threshold: float = 0.45,
    ) -> dict[str, str]:
        """Process an SDP offer and return an SDP answer.

        Creates a :class:`QueuedAudioTrack` for outgoing TTS audio and
        an :class:`AsyncSTT` instance for incoming speech recognition.

        Audio state is keyed by *client_id*.  The optional *session_id* is
        used only as initial context for transcript routing.

        If *playground* is True, STT events go to the playground WS
        instead of the agent session.

        If *desktop* is True, a :class:`ScreenShareTrack` is added to
        stream the server's screen capture as a WebRTC video track.

        *desktop_role* controls the connection type when *desktop* is True:
        ``"controller"`` (default) gets video + input channel,
        ``"viewer"`` gets video only (no input).
        """
        # Tear down any pre-existing connection for this client.
        if client_id in self._connections:
            await self.close_connection(client_id)

        if playground:
            self._playground_clients.add(client_id)

        # Build ICE server list from config (empty = local network only).
        ice_server_objs = []
        for srv in self._ice_servers:
            ice_server_objs.append(RTCIceServer(
                urls=srv["urls"],
                username=srv.get("username"),
                credential=srv.get("credential"),
            ))
        pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_server_objs))
        self._connections[client_id] = pc

        @pc.on("connectionstatechange")
        async def _on_connection_state_change() -> None:
            logger.info(
                "[webrtc] client %s connection state: %s",
                client_id,
                pc.connectionState,
            )
            if pc.connectionState == "connected" and desktop:
                # Force a keyframe after bitrate ramp-up for a sharp first image
                async def _delayed_keyframe() -> None:
                    await asyncio.sleep(2)
                    for t in pc.getTransceivers():
                        if t.kind == "video" and hasattr(t.sender, "_send_keyframe"):
                            t.sender._send_keyframe()
                            logger.info("[webrtc] client %s: forced desktop keyframe", client_id)
                            break
                asyncio.ensure_future(_delayed_keyframe())
            elif pc.connectionState in ("failed", "closed"):
                await self.close_connection(client_id)

        # Desktop connections are video-only (no incoming audio to process).
        if not desktop:
            # Resolve voice profile → STT params
            stt_params = voice_profiles.get_stt_params(username, profile_id)

            # Create STT instance for this client.
            # aiortc typically delivers stereo (2-channel) audio even if source is mono.
            stt = AsyncSTT(sample_rate=48000, num_channels=2, params=stt_params)

            if playground:
                stt.add_listener(self._make_playground_listener(client_id))
            else:
                stt.add_listener(self._make_stt_listener(client_id))

            self._stt_instances[client_id] = stt
            self._client_usernames[client_id] = username

            # Apply per-client speaker gate if provided in the offer
            if speaker_gate_name:
                from server.speaker_fingerprints import get_embeddings_for_speaker
                embeddings = get_embeddings_for_speaker(username, speaker_gate_name)
                if embeddings:
                    stt.set_speaker_gate(embeddings, speaker_gate_threshold)
                    self._client_speaker_gates[client_id] = (speaker_gate_name, speaker_gate_threshold)
                    logger.info("[webrtc] client %s: speaker gate applied on connect (speaker='%s', threshold=%.3f, %d embeddings)",
                                client_id, speaker_gate_name, speaker_gate_threshold, len(embeddings))
                else:
                    logger.info("[webrtc] client %s: speaker '%s' not found, no gate applied", client_id, speaker_gate_name)
            else:
                logger.info("[webrtc] client %s: no speaker gate requested", client_id)

            # Create voice logger for audio persistence
            vl = VoiceLogger(username, session_id or client_id)
            self._voice_loggers[client_id] = vl
            await vl.start_recording()

            @pc.on("track")
            def _on_track(track: MediaStreamTrack) -> None:
                logger.info(
                    "[webrtc] client %s received %s track", client_id, track.kind
                )
                if track.kind == "audio":
                    stats = AudioStatsLogger(client_id, "incoming")
                    self._stats[client_id] = stats
                    asyncio.ensure_future(self._consume_audio(client_id, track, stats))

            # Add outgoing audio track (receives TTS Opus frames via queue).
            outgoing = QueuedAudioTrack(client_id)
            pc.addTrack(outgoing)
            self._outgoing_tracks[client_id] = outgoing

        # Add outgoing video track for desktop screen sharing.
        if desktop:
            try:
                display = os.environ.get("DISPLAY", ":1")

                # Use shared capture: one ScreenCapture per display, ref-counted
                if display in self._shared_captures:
                    capture = self._shared_captures[display]
                    self._shared_capture_refs[display].add(client_id)
                    logger.info("[webrtc] client %s: reusing shared capture for %s", client_id, display)
                else:
                    capture = ScreenCapture(target_fps=30, max_height=1080)
                    await capture.start()
                    self._shared_captures[display] = capture
                    self._shared_capture_refs[display] = {client_id}
                    logger.info("[webrtc] client %s: created shared capture for %s", client_id, display)

                video_track = ScreenShareTrack(capture)
                pc.addTrack(video_track)
                # Prefer H.264 for WebView compatibility (VP8 decode is unreliable)
                for t in pc.getTransceivers():
                    if t.kind == "video":
                        from aiortc.rtcrtpsender import RTCRtpSender
                        caps = RTCRtpSender.getCapabilities("video")
                        h264 = [c for c in caps.codecs if "H264" in c.mimeType]
                        if h264:
                            t.setCodecPreferences(h264)
                            logger.info("[webrtc] client %s: H.264 preferred for desktop", client_id)
                        break
                self._video_tracks[client_id] = video_track
                self._screen_captures[client_id] = capture
                logger.info(
                    "[webrtc] client %s: desktop %s track added (%dx%d)",
                    client_id, desktop_role, capture.capture_width, capture.capture_height,
                )

                if desktop_role == "viewer":
                    # Viewer: video only, no input injection
                    self._desktop_viewers.add(client_id)
                else:
                    # Controller: full input channel for mouse/keyboard/clipboard
                    @pc.on("datachannel")
                    def on_datachannel(channel):
                        injector = InputInjector(
                            display, capture.native_width, capture.native_height,
                            screen_size_fn=lambda: (capture.native_width, capture.native_height),
                        )
                        self._input_injectors[client_id] = injector
                        logger.info("[webrtc] client %s: input channel '%s' opened", client_id, channel.label)

                        @channel.on("message")
                        def on_message(msg):
                            try:
                                event = _json.loads(msg)
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
                logger.warning("[webrtc] client %s: no display for desktop: %s", client_id, exc)

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
                    "[webrtc] client %s: ICE gathering timed out (state=%s)",
                    client_id,
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

    def _make_stt_listener(self, client_id: str):
        """Create an STT event listener that submits transcripts to the agent.

        Captures only *client_id*.  The target session is resolved dynamically
        at transcript time via ``ws_bridge._client_sessions``, so switching
        sessions doesn't break audio routing.
        """

        async def _on_stt_event(stt, event_type: str, data) -> None:
            # Suppress transcript routing while enrollment is active —
            # the enrollment listener handles its own events separately
            if client_id in self._enrollment_ws and event_type in (
                "final_transcript", "interim_transcript",
            ):
                return

            # Resolve the target session dynamically
            def _resolve_session() -> str | None:
                if self._ws_bridge:
                    return self._ws_bridge._client_sessions.get(client_id)
                return None

            async def _clear_voice_preview() -> None:
                """Send empty preview to clear stale interim text from the UI."""
                if not self._ws_bridge:
                    return
                target_sid = None
                if self._ring0_manager and self._ring0_manager.is_enabled:
                    if self._node_registry:
                        active_nid = self._node_registry.active_node_id
                        if active_nid != "local":
                            from server.ws_bridge import WsBridge
                            target_sid = WsBridge.qualify_session_id(active_nid, "ring0")
                    if not target_sid:
                        target_sid = self._ring0_manager.session_id
                if not target_sid:
                    target_sid = _resolve_session()
                if target_sid:
                    await self._ws_bridge.send_to_browsers(target_sid, {
                        "type": "voice_transcript_preview",
                        "transcript": "",
                    })

            if event_type == "segment_confirmed":
                # Log individual segment audio (moved from final_transcript)
                audio = data.get("audio")
                voice_log = self._voice_loggers.get(client_id)
                if voice_log and audio is not None:
                    try:
                        await voice_log.log_segment(audio, data)
                    except Exception:
                        logger.exception("[voice-log] Failed to log segment")

            elif event_type == "final_transcript":
                transcript = data["transcript"].strip()
                logger.info("[stt] client %s transcript: %s", client_id, transcript)

                if not transcript or not self._ws_bridge:
                    return

                # Helper to submit text through Ring0 or the client's current session.
                async def _submit_text(text: str) -> None:
                    logger.info("[stt] SUBMIT to model: client=%s text=%r eou=%.4f",
                                client_id, text, data.get("eouProb", -1))
                    if self._ring0_manager and self._ring0_manager.is_enabled:
                        # Check if a remote node is active — route to its Ring0
                        if self._node_registry:
                            active_nid = self._node_registry.active_node_id
                            node = self._node_registry.get_node(active_nid)
                            if node and node.id != "local":
                                if node.tunnel and node.status == "online":
                                    logger.info("[stt] Routing to remote node %r (id=%s)", node.name, node.id[:8])
                                    await node.tunnel.send_fire_and_forget({
                                        "type": "ring0_input",
                                        "text": text,
                                        "sourceClientId": client_id,
                                    })
                                    return
                                else:
                                    logger.warning("[stt] Active node %r not routable: tunnel=%s status=%s — falling through to local",
                                                   node.name, bool(node.tunnel), node.status)
                        # Local Ring0
                        r0sid = self._ring0_manager.session_id
                        if not r0sid and self._launcher and self._ws_bridge:
                            r0sid = await self._ring0_manager.ensure_session(self._launcher, self._ws_bridge)
                        if r0sid:
                            await self._ws_bridge.submit_user_message(r0sid, text, source_client_id=client_id)
                            return
                    target_session = _resolve_session()
                    if target_session:
                        await self._ws_bridge.submit_user_message(target_session, text, source_client_id=client_id)

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
                        mode = self.get_voice_mode(client_id)
                        if not mode:
                            asyncio.ensure_future(self._speak_short(client_id, "No active mode"))
                            return
                        # Add pre-text as a final note fragment (it was part of dictation)
                        if pre_text:
                            mode.on_transcript(pre_text)
                        result = mode.on_done()
                        self.set_voice_mode(client_id, None)
                        if not result:
                            asyncio.ensure_future(self._speak_short(client_id, "Empty note"))
                            return
                        asyncio.ensure_future(self._speak_short(client_id, "Done"))
                        # Deliver via ring0 if enabled, otherwise to current session
                        if self._ring0_manager and self._ring0_manager.is_enabled:
                            ring0_sid = self._ring0_manager.session_id
                            if ring0_sid:
                                await self._ws_bridge.submit_user_message(ring0_sid, result, source_client_id=client_id)
                                if isinstance(mode, NoteMode):
                                    from server.ring0_events import Ring0Event
                                    await self._ws_bridge.emit_ring0_event(Ring0Event(fields={"type": "note_mode_ended"}))
                                return
                        target_session = _resolve_session()
                        if target_session:
                            await self._ws_bridge.submit_user_message(target_session, result, source_client_id=client_id)
                        return

                    # In voice mode (e.g. note mode), only "done" is recognized.
                    # All other guard-prefixed speech falls through to fragment capture.
                    if not self.get_voice_mode(client_id):
                        if after_word.startswith("off"):
                            if pre_text:
                                await _submit_text(pre_text)
                            if self._ws_bridge:
                                target_session = _resolve_session()
                                if target_session:
                                    asyncio.ensure_future(
                                        self._ws_bridge.broadcast_audio_off(target_session)
                                    )
                            return
                        if after_word.startswith("guard"):
                            if pre_text:
                                await _submit_text(pre_text)
                            self.set_guard_enabled(client_id, True)
                            asyncio.ensure_future(self._speak_short(client_id, "Guard on"))
                            return
                        if after_word.startswith("listen"):
                            if pre_text:
                                await _submit_text(pre_text)
                            self.set_guard_enabled(client_id, False)
                            asyncio.ensure_future(self._speak_short(client_id, "Listening"))
                            return
                        if after_word.startswith("quiet"):
                            if pre_text:
                                await _submit_text(pre_text)
                            self.set_tts_muted(client_id, True)
                            asyncio.ensure_future(self._speak_short(client_id, "Quiet mode"))
                            return
                        if after_word.startswith("speak"):
                            if pre_text:
                                await _submit_text(pre_text)
                            self.set_tts_muted(client_id, False)
                            asyncio.ensure_future(self._speak_short(client_id, "Speaking"))
                            return
                        if after_word.startswith("ring zero on") or after_word.startswith("ring 0 on"):
                            if pre_text:
                                await _submit_text(pre_text)
                            if self._ring0_manager:
                                self._ring0_manager.enable()
                                asyncio.ensure_future(self._speak_short(client_id, "Ring zero on"))
                            return
                        if after_word.startswith("ring zero off") or after_word.startswith("ring 0 off"):
                            if pre_text:
                                await _submit_text(pre_text)
                            if self._ring0_manager:
                                self._ring0_manager.disable()
                                asyncio.ensure_future(self._speak_short(client_id, "Ring zero off"))
                            return
                        if after_word.startswith("note"):
                            if pre_text:
                                await _submit_text(pre_text)
                            existing = self.get_voice_mode(client_id)
                            if existing and existing.name == "note":
                                asyncio.ensure_future(self._speak_short(client_id, "Already in note mode"))
                            else:
                                self.set_voice_mode(client_id, NoteMode())
                                asyncio.ensure_future(self._speak_short(client_id, "Note mode"))
                            return
                        if after_word.startswith("node"):
                            if pre_text:
                                await _submit_text(pre_text)
                            await self._handle_node_switch_command(client_id, after_word[4:].strip())
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
                mode = self.get_voice_mode(client_id)
                if mode:
                    mode.on_transcript(transcript)
                    return

                # If guard is enabled, require guard word
                if self.is_guard_enabled(client_id):
                    if not guard_word_found:
                        logger.info("[guard] client %s: no guard word, discarding", client_id)
                        # Clear any stale interim preview from the UI
                        await _clear_voice_preview()
                        return

                await _submit_text(transcript)
            elif event_type == "voice_was_detected":
                logger.info("[stt] client %s: voice detected — barge-in", client_id)
                self.barge_in(client_id)
            elif event_type == "voice_not_detected":
                logger.debug("[stt] client %s: voice ended", client_id)
            elif event_type == "interim_transcript":
                # Suppress during voice modes (note mode, etc.)
                if self.get_voice_mode(client_id):
                    return
                # Suppress if guard is enabled and no guard word in interim text
                if self.is_guard_enabled(client_id):
                    text = data.get("transcript", "")
                    if not self._find_guard_word(text):
                        return
                if not self._ws_bridge:
                    return
                target_sid = None
                if self._ring0_manager and self._ring0_manager.is_enabled:
                    # Route preview to remote node's Ring0 if active
                    if self._node_registry:
                        active_nid = self._node_registry.active_node_id
                        if active_nid != "local":
                            from server.ws_bridge import WsBridge
                            target_sid = WsBridge.qualify_session_id(active_nid, "ring0")
                    if not target_sid:
                        target_sid = self._ring0_manager.session_id
                if not target_sid:
                    target_sid = _resolve_session()
                if target_sid:
                    await self._ws_bridge.send_to_browsers(target_sid, {
                        "type": "voice_transcript_preview",
                        "transcript": data["transcript"],
                    })

        return _on_stt_event

    def _make_playground_listener(self, client_id: str):
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
                    vl = self._voice_loggers.get(client_id)
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

    # ── Enrollment listener ──────────────────────────────────────────────

    def _make_enrollment_listener(self, client_id: str):
        """STT listener that extracts ECAPA embeddings and sends them to the enrollment WS."""

        async def _on_enrollment_event(stt, event_type: str, data) -> None:
            ws = self._enrollment_ws.get(client_id)
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
            elif event_type == "segment_confirmed":
                transcript = data.get("transcript", "").strip()
                audio = data.get("audio")
                if audio is None:
                    return
                import numpy as np
                from server.speaker_model import embed, cosine_sim
                loop = asyncio.get_event_loop()
                float_buf = audio.astype(np.float32) / np.iinfo(np.int16).max
                emb = await loop.run_in_executor(None, embed, float_buf)
                emb_list = emb.tolist()

                username = self._client_usernames.get(client_id, "default")
                from server import speaker_fingerprints
                fps = speaker_fingerprints.list_fingerprints(username)
                scores = []
                for fp_meta in fps:
                    fp = speaker_fingerprints.get_fingerprint(username, fp_meta["id"])
                    if fp and fp.get("embeddings"):
                        best_sim = -1.0
                        best_label = ""
                        for e in fp["embeddings"]:
                            if "embedding" not in e:
                                continue
                            sim = cosine_sim(emb, np.array(e["embedding"], dtype=np.float32))
                            if sim > best_sim:
                                best_sim = sim
                                best_label = e.get("label", "")
                        scores.append({"id": fp["id"], "name": fp["name"], "similarity": round(best_sim, 4), "bestVoiceprint": best_label})

                try:
                    await ws.send_str(_json.dumps({
                        "type": "enrollment_segment",
                        "transcript": transcript,
                        "embedding": emb_list,
                        "scores": scores,
                        "timeBegin": data.get("timeBegin", 0),
                        "timeEnd": data.get("timeEnd", 0),
                    }))
                except Exception:
                    pass

        return _on_enrollment_event

    def register_enrollment_ws(self, client_id: str, ws) -> None:
        """Register an enrollment WebSocket and add enrollment listener to STT."""
        self._enrollment_ws[client_id] = ws
        stt = self._stt_instances.get(client_id)
        if stt:
            listener = self._make_enrollment_listener(client_id)
            self._enrollment_listeners[client_id] = listener
            stt.add_listener(listener)

    def unregister_enrollment_ws(self, client_id: str) -> None:
        """Unregister enrollment WebSocket and remove enrollment listener."""
        self._enrollment_ws.pop(client_id, None)
        listener = self._enrollment_listeners.pop(client_id, None)
        if listener:
            stt = self._stt_instances.get(client_id)
            if stt:
                try:
                    stt.remove_listener(listener)
                except ValueError:
                    pass

    def update_stt_params(self, client_id: str, params: STTParams) -> None:
        """Update STT params for a live client (e.g. from playground slider)."""
        stt = self._stt_instances.get(client_id)
        if stt:
            stt.update_params(params)

    async def _speak_short(self, client_id: str, phrase: str) -> None:
        """Speak a short acknowledgment phrase via TTS (e.g. 'Guard on').

        Respects the TTS mute setting — if the user has switched to
        mic-only mode, acknowledgments are also silenced.
        """
        track = self.get_outgoing_track(client_id)
        if not track:
            fallback = self.get_any_outgoing_track()
            if fallback:
                track = fallback[1]
        if not track or self.is_tts_muted(client_id):
            return
        try:
            from server.tts import create_tts
            tts = create_tts(opus_frame_handler=track.push_opus_frame)
            await tts.say(phrase)
        except Exception:
            logger.exception("[guard] TTS failed for client %s phrase=%r", client_id, phrase)

    async def _handle_node_switch_command(self, client_id: str, node_name: str) -> None:
        """Handle 'vibr8 node <name>' voice command to switch active node."""
        if not self._node_registry:
            asyncio.ensure_future(self._speak_short(client_id, "No nodes available"))
            return

        node_name = node_name.strip()
        if not node_name:
            asyncio.ensure_future(self._speak_short(client_id, "Which node?"))
            return

        # "local", "hub", or the hub's configured name switches back to hub
        name_lower = node_name.lower()
        local_node = self._node_registry.local_node
        hub_name = local_node.name
        is_hub = name_lower in ("local", "hub") or name_lower in hub_name.lower()
        if is_hub:
            if self._node_registry.active_node_id == local_node.id:
                asyncio.ensure_future(self._speak_short(client_id, f"Already on {hub_name}"))
                return
            self._node_registry.active_node_id = local_node.id
            logger.info("[voice] Switched to local node (%s) via voice command", hub_name)
            asyncio.ensure_future(self._speak_short(client_id, f"Switched to {hub_name}"))
            if self._ws_bridge:
                asyncio.ensure_future(
                    self._ws_bridge.broadcast_node_switch(local_node.id, hub_name)
                )
            return

        matches = self._node_registry.find_by_name(node_name)
        if not matches:
            asyncio.ensure_future(self._speak_short(client_id, f"No node named {node_name} found"))
            return
        if len(matches) > 1:
            names = ", ".join(m.name for m in matches[:3])
            asyncio.ensure_future(self._speak_short(client_id, f"Multiple nodes match: {names}"))
            return

        target = matches[0]
        if self._node_registry.active_node_id == target.id:
            asyncio.ensure_future(self._speak_short(client_id, f"Already on {target.name}"))
            return
        if target.status != "online":
            asyncio.ensure_future(self._speak_short(client_id, f"{target.name} is offline"))
            return

        self._node_registry.active_node_id = target.id
        logger.info("[voice] Switched to node %r (id=%s) via voice command", target.name, target.id[:8])
        asyncio.ensure_future(self._speak_short(client_id, f"Switched to node {target.name}"))
        if self._ws_bridge:
            asyncio.ensure_future(
                self._ws_bridge.broadcast_node_switch(target.id, target.name)
            )

    async def _consume_audio(
        self,
        client_id: str,
        track: MediaStreamTrack,
        stats: AudioStatsLogger,
    ) -> None:
        """Read frames from *track*, log stats, and feed to STT."""
        stt = self._stt_instances.get(client_id)
        audio_buffer: list[np.ndarray] = []

        voice_log = self._voice_loggers.get(client_id)

        try:
            while True:
                frame = await track.recv()
                stats.log_frame(frame)

                if stt and client_id not in self._stt_muted:
                    pcm = frame.to_ndarray()  # shape (channels, 960) s16
                    audio_buffer.append(pcm)
                    if len(audio_buffer) >= 8:  # ~160ms batch
                        batch = np.concatenate(audio_buffer, axis=1)
                        # Log first batch to confirm STT is receiving audio.
                        if not hasattr(self, '_stt_logged'):
                            self._stt_logged = set()
                        if client_id not in self._stt_logged:
                            self._stt_logged.add(client_id)
                            rms = float(np.sqrt(np.mean(batch.astype(np.float64)**2)))
                            logger.info(
                                "[webrtc] First STT batch for client %s: shape=%s, rms=%.1f",
                                client_id, batch.shape, rms,
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
                "[webrtc] incoming audio track ended for client %s", client_id
            )

    async def close_connection(self, client_id: str) -> None:
        """Close and remove the peer connection for *client_id*."""
        # Flush active voice mode (deliver interrupted note)
        mode = self._voice_modes.pop(client_id, None)
        if mode and self._ws_bridge:
            result = mode.on_disconnect()
            if result:
                logger.info("[voice-mode] client %s: flushing on disconnect", client_id)
                if self._ring0_manager and self._ring0_manager.is_enabled and self._ring0_manager.session_id:
                    await self._ws_bridge.submit_user_message(self._ring0_manager.session_id, result, source_client_id=client_id)
                else:
                    target_session = self._current_session_for(client_id)
                    if target_session:
                        await self._ws_bridge.submit_user_message(target_session, result, source_client_id=client_id)

        pc = self._connections.pop(client_id, None)
        self._stats.pop(client_id, None)
        self._outgoing_tracks.pop(client_id, None)
        self._video_tracks.pop(client_id, None)
        self._input_injectors.pop(client_id, None)
        self._desktop_viewers.discard(client_id)
        capture = self._screen_captures.pop(client_id, None)
        if capture:
            # Shared capture ref-counting: only stop when last consumer disconnects
            stopped = False
            for display, cap in list(self._shared_captures.items()):
                if cap is capture:
                    refs = self._shared_capture_refs.get(display, set())
                    refs.discard(client_id)
                    if not refs:
                        self._shared_captures.pop(display, None)
                        self._shared_capture_refs.pop(display, None)
                        try:
                            await capture.stop()
                        except Exception:
                            pass
                    stopped = True
                    break
            if not stopped:
                try:
                    await capture.stop()
                except Exception:
                    pass
        self._guard_enabled.pop(client_id, None)
        self._tts_muted.pop(client_id, None)
        self._playground_clients.discard(client_id)
        self._enrollment_ws.pop(client_id, None)
        self._enrollment_listeners.pop(client_id, None)
        self._client_usernames.pop(client_id, None)
        self._client_speaker_gates.pop(client_id, None)

        # Clean up playground WS reference
        self._playground_ws.pop(client_id, None)

        # Clean up STT.
        stt = self._stt_instances.pop(client_id, None)
        if stt:
            stt.flush()
            stt.stop()

        # Clean up voice logger.
        voice_log = self._voice_loggers.pop(client_id, None)
        if voice_log:
            try:
                await voice_log.stop_recording()
            except Exception:
                pass

        if pc is not None:
            await pc.close()
            logger.info("[webrtc] closed connection for client %s", client_id)

    async def close_all(self) -> None:
        """Close every active peer connection."""
        client_ids = list(self._connections.keys())
        for client_id in client_ids:
            await self.close_connection(client_id)
