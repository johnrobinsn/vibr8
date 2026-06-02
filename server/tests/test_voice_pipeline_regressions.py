import pytest
import numpy as np
import time

from server.stt import AsyncSTT, STT, STTParams
from server.webrtc import WebRTCManager


def test_default_prompt_timeout_matches_profile_defaults() -> None:
    assert STTParams().prompt_timeout_ms == 1500


def test_confirmed_segment_finalizes_after_prompt_wait(monkeypatch) -> None:
    """Happy path: a confirmed segment exits PROMPT_WAIT after silence frames.

    This does not cover the separate first-segment EOU-retry stall, which
    happens in SEGMENT_N before any segment has been confirmed.
    """
    events: list[tuple[str, dict | None]] = []
    frame = np.zeros(512, dtype=np.int16)
    stt = STT(16000, 1, params=STTParams(prompt_timeout_ms=320))

    monkeypatch.setattr(STT, "_transcribe", staticmethod(lambda audio: "hello there"))
    monkeypatch.setitem(STT.shared_resources, "eou", lambda text: 0.9)
    stt.add_listener(lambda _stt, event_type, data: events.append((event_type, data)))

    def tick(event: STT.Event) -> None:
        stt._state_machine.handle_event(event, stt._segment, frame)
        stt._capture_time += 0.16

    tick(STT.Event.VOICE_WAS_DETECTED)
    tick(STT.Event.VOICE_WAS_DETECTED)
    tick(STT.Event.VOICE_NOT_DETECTED)
    tick(STT.Event.VOICE_NOT_DETECTED)
    tick(STT.Event.VOICE_NOT_DETECTED)
    tick(STT.Event.VOICE_NOT_DETECTED)

    assert stt._state_machine.state == STT.State.PROMPT_WAIT
    assert any(event_type == "segment_confirmed" for event_type, _data in events)
    assert not any(event_type == "final_transcript" for event_type, _data in events)

    tick(STT.Event.VOICE_NOT_DETECTED)
    tick(STT.Event.VOICE_NOT_DETECTED)

    assert events[-1] == ("final_transcript", {"transcript": "hello there"})
    assert stt._state_machine.state == STT.State.IDLE


@pytest.mark.parametrize(
    "state",
    [
        STT.State.IDLE,
        STT.State.SEGMENT_0,
        STT.State.SEGMENT_N,
        STT.State.SILENCE_0,
        STT.State.SILENCE_1,
        STT.State.SILENCE_2,
    ],
)
@pytest.mark.asyncio
async def test_watchdog_only_flushes_while_prompt_waiting(state: STT.State) -> None:
    stt = AsyncSTT(16000, 1, params=STTParams(prompt_timeout_ms=1500))
    try:
        sm = stt._state_machine
        sm.prompt_segments = ["partial transcript"]
        sm.last_segment_appended_at = time.monotonic() - 10
        sm.state = state

        assert stt._watchdog_flush_due(time.monotonic(), grace_seconds=0.2) is None
    finally:
        stt.stop()


@pytest.mark.asyncio
async def test_final_transcript_clears_voice_preview_before_submit() -> None:
    calls: list[tuple[str, str, object]] = []

    class FakeBridge:
        _client_sessions = {"client-1": "session-1"}

        async def send_to_browsers(self, session_id, msg):
            calls.append(("preview", session_id, msg))

        async def submit_user_message(self, session_id, text, source_client_id=None):
            calls.append(("submit", session_id, (text, source_client_id)))

    manager = WebRTCManager()
    manager.set_ws_bridge(FakeBridge())
    listener = manager._make_stt_listener("client-1")

    await listener(None, "final_transcript", {"transcript": "vibr8 status"})

    assert calls == [
        ("preview", "session-1", {"type": "voice_transcript_preview", "transcript": ""}),
        ("submit", "session-1", ("vibr8 status", "client-1")),
    ]
