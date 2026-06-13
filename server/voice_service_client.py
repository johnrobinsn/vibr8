"""Opt-in adapter that proxies vibr8 audio WebRTC to voice-service.

Desktop WebRTC and vibr8-specific voice state stay on the local
``WebRTCManager``.  Audio offers are forwarded to the standalone service
when ``VIBR8_VOICE_SERVICE_URL`` is configured.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class _RemoteSession:
    service_session_id: str
    connection_token: str
    client_id: str
    tab_id: str
    vibr8_session_id: str
    events_task: asyncio.Task | None = None


class _RemoteAudioTrack:
    """Marker object used by WsBridge to detect active remote audio."""

    def __init__(self, client_id: str) -> None:
        self.client_id = client_id

    def push_opus_frame(self, _frame: bytes) -> None:
        """Local TTS frames are not used for remote voice-service sessions."""

    def clear_audio(self) -> None:
        """Remote queues are cleared via the service TTS cancel endpoint."""

    def set_thinking(self, _thinking: bool) -> None:
        """Thinking tones remain a vibr8-local feature."""


class VoiceServiceClient:
    """WebRTCManager-compatible adapter for the standalone voice service."""

    def __init__(
        self,
        local_manager: Any,
        service_url: str,
        *,
        api_token: str | None = None,
        tenant_id: str = "default",
    ) -> None:
        self._local = local_manager
        self._base_url = service_url.rstrip("/")
        self._api_token = api_token
        self._tenant_id = tenant_id
        self._session: aiohttp.ClientSession | None = None
        self._remote_sessions: dict[str, _RemoteSession] = {}
        self._client_sessions: dict[str, set[str]] = {}
        self._tracks: dict[str, _RemoteAudioTrack] = {}
        self._ws_bridge = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._local, name)

    @staticmethod
    def _peer_key(client_id: str, tab_id: str = "") -> str:
        return f"{client_id}#{tab_id}" if tab_id else client_id

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _api_headers(self) -> dict[str, str]:
        if not self._api_token:
            return {}
        return {"Authorization": f"Bearer {self._api_token}"}

    @staticmethod
    def _session_headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def set_ws_bridge(self, bridge) -> None:
        self._ws_bridge = bridge
        self._local.set_ws_bridge(bridge)

    async def handle_offer(
        self,
        client_id: str,
        sdp: str,
        sdp_type: str,
        session_id: str = "",
        playground: bool = False,
        profile_id: str | None = None,
        username: str = "default",
        desktop: bool = False,
        desktop_role: str = "controller",
        speaker_gate_name: str | None = None,
        speaker_gate_threshold: float = 0.45,
        speaker_gate_tse_enabled: bool = False,
        speaker_gate_tse_threshold: float = 0.35,
        tab_id: str = "",
    ) -> dict[str, str]:
        if desktop:
            return await self._local.handle_offer(
                client_id,
                sdp,
                sdp_type,
                session_id=session_id,
                playground=playground,
                profile_id=profile_id,
                username=username,
                desktop=desktop,
                desktop_role=desktop_role,
                speaker_gate_name=speaker_gate_name,
                speaker_gate_threshold=speaker_gate_threshold,
                speaker_gate_tse_enabled=speaker_gate_tse_enabled,
                speaker_gate_tse_threshold=speaker_gate_tse_threshold,
                tab_id=tab_id,
            )

        peer_key = self._peer_key(client_id, tab_id)
        await self.close_peer(peer_key)

        http = await self._http()
        body: dict[str, Any] = {
            "tenant_id": self._tenant_id,
            "subject_user_id": username,
            "client_ref": peer_key,
            "transport": {"preferred": "webrtc", "allow_websocket_fallback": True},
            "metadata": {
                "vibr8_client_id": client_id,
                "vibr8_session_id": session_id,
                "vibr8_tab_id": tab_id,
                "playground": playground,
            },
        }
        if profile_id:
            body["profile_id"] = profile_id
        if speaker_gate_name:
            body["features"] = {
                "speaker_gate": {
                    "provider": "embedding",
                    "speaker_ids": [speaker_gate_name],
                    "threshold": speaker_gate_threshold,
                    "tse_enabled": speaker_gate_tse_enabled,
                    "tse_threshold": speaker_gate_tse_threshold,
                }
            }

        async with http.post(f"{self._base_url}/v1/sessions", json=body, headers=self._api_headers()) as response:
            created = await self._json_or_error(response)

        service_session_id = str(created["id"])
        token = str(created["connection_token"])
        offer_body = {"sdp": sdp, "type": sdp_type}
        async with http.post(
            f"{self._base_url}/v1/sessions/{service_session_id}/webrtc/offer",
            json=offer_body,
            headers=self._session_headers(token),
        ) as response:
            answer = await self._json_or_error(response)

        remote = _RemoteSession(
            service_session_id=service_session_id,
            connection_token=token,
            client_id=client_id,
            tab_id=tab_id,
            vibr8_session_id=session_id,
        )
        remote.events_task = asyncio.create_task(self._forward_events(peer_key, remote, playground=playground))
        self._remote_sessions[peer_key] = remote
        self._client_sessions.setdefault(client_id, set()).add(peer_key)
        self._tracks[peer_key] = _RemoteAudioTrack(client_id)
        logger.info("[voice-service] client %s proxied to service session %s", peer_key, service_session_id)
        return {"sdp": str(answer["sdp"]), "type": str(answer.get("type", "answer"))}

    async def say_for_session(self, _vibr8_session_id: str, text: str, *, interrupt: bool = True) -> bool:
        remote = self._first_remote_session()
        if remote is None:
            return False
        http = await self._http()
        async with http.post(
            f"{self._base_url}/v1/sessions/{remote.service_session_id}/tts",
            json={"text": text, "interrupt": interrupt},
            headers=self._session_headers(remote.connection_token),
        ) as response:
            await self._json_or_error(response)
        return True

    async def cancel_tts_for_session(self, _vibr8_session_id: str) -> bool:
        remote = self._first_remote_session()
        if remote is None:
            return False
        http = await self._http()
        async with http.post(
            f"{self._base_url}/v1/sessions/{remote.service_session_id}/tts/cancel",
            headers=self._session_headers(remote.connection_token),
        ) as response:
            await self._json_or_error(response)
        return True

    def get_any_outgoing_track(self):
        for peer_key, track in self._tracks.items():
            remote = self._remote_sessions.get(peer_key)
            if remote:
                return remote.client_id, track
        return self._local.get_any_outgoing_track()

    def get_outgoing_track(self, client_id: str):
        for peer_key in self._client_sessions.get(client_id, ()):
            track = self._tracks.get(peer_key)
            if track:
                return track
        return self._local.get_outgoing_track(client_id)

    def barge_in(self, client_id: str) -> None:
        for peer_key in self._client_sessions.get(client_id, ()):
            remote = self._remote_sessions.get(peer_key)
            if remote:
                asyncio.create_task(self.cancel_tts_for_session(remote.vibr8_session_id))
        self._local.barge_in(client_id)

    async def close_peer(self, peer_key: str) -> None:
        remote = self._remote_sessions.pop(peer_key, None)
        if remote:
            if remote.events_task:
                remote.events_task.cancel()
            self._tracks.pop(peer_key, None)
            peers = self._client_sessions.get(remote.client_id)
            if peers:
                peers.discard(peer_key)
                if not peers:
                    self._client_sessions.pop(remote.client_id, None)
            try:
                http = await self._http()
                async with http.delete(
                    f"{self._base_url}/v1/sessions/{remote.service_session_id}",
                    json={"reason": "vibr8_disconnect"},
                    headers=self._session_headers(remote.connection_token),
                ) as response:
                    await self._json_or_error(response)
            except Exception:
                logger.exception("[voice-service] failed to close service session %s", remote.service_session_id)
            return
        await self._local.close_peer(peer_key)

    async def close_all(self) -> None:
        for peer_key in list(self._remote_sessions):
            await self.close_peer(peer_key)
        if self._session is not None:
            await self._session.close()
            self._session = None
        await self._local.close_all()

    async def _forward_events(self, peer_key: str, remote: _RemoteSession, *, playground: bool) -> None:
        if self._ws_bridge is None:
            return
        http = await self._http()
        url = f"{self._base_url}/v1/sessions/{remote.service_session_id}/events/ws?compat=vibr8"
        try:
            async with http.ws_connect(url, headers=self._session_headers(remote.connection_token)) as ws:
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    event = json.loads(msg.data)
                    await self._dispatch_compat_event(remote, event, playground=playground)
        except asyncio.CancelledError:
            raise
        except Exception:
            if peer_key in self._remote_sessions:
                logger.exception("[voice-service] event stream failed for %s", peer_key)

    async def _dispatch_compat_event(self, remote: _RemoteSession, event: dict[str, Any], *, playground: bool) -> None:
        event_type = str(event.get("type", ""))
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if playground:
            ws = getattr(self._local, "_playground_ws", {}).get(remote.client_id)
            if ws is not None:
                await ws.send_str(json.dumps({"type": event_type, **data}))
            return
        if event_type == "voice_transcript_preview":
            # Preview display routes to the active node's Ring0 UI (the node
            # broadcasts it over the vended session WS); audio stays here.
            await self._send_voice_preview(remote.client_id, data.get("transcript", ""))
            return
        if event_type == "final_transcript":
            text = str(data.get("transcript", "")).strip()
            session_id = self._current_vibr8_session(remote)
            if text and session_id:
                await self._send_voice_preview(remote.client_id, "")
                await self._ws_bridge.submit_user_message(session_id, text, source_client_id=remote.client_id)

    def _current_vibr8_session(self, remote: _RemoteSession) -> str:
        if self._ws_bridge is not None:
            return self._ws_bridge._client_sessions.get(remote.client_id, remote.vibr8_session_id)
        return remote.vibr8_session_id

    def _first_remote_session(self) -> _RemoteSession | None:
        for remote in self._remote_sessions.values():
            return remote
        return None

    @staticmethod
    async def _json_or_error(response: aiohttp.ClientResponse) -> dict[str, Any]:
        try:
            payload = await response.json()
        except Exception:
            payload = {"error": await response.text()}
        if response.status >= 400:
            raise RuntimeError(str(payload.get("error", f"HTTP {response.status}")))
        return dict(payload)
