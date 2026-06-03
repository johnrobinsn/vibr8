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


def test_unconfirmed_interim_can_be_force_confirmed_when_frames_stop(monkeypatch) -> None:
    events: list[tuple[str, dict | None]] = []
    frame = np.zeros(512, dtype=np.int16)
    stt = STT(16000, 1, params=STTParams(prompt_timeout_ms=320))

    monkeypatch.setattr(STT, "_transcribe", staticmethod(lambda audio: "are you able to hear me"))
    monkeypatch.setitem(STT.shared_resources, "eou", lambda text: 0.1)
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

    sm = stt._state_machine
    assert sm.state == STT.State.SEGMENT_N
    assert sm.prompt_segments == []
    assert sm.pending_interim is not None
    last_interim_at = sm.last_interim_at
    assert last_interim_at is not None
    assert any(
        event_type == "interim_transcript" and data and data.get("retry") == 0
        for event_type, data in events
    )

    assert sm.force_confirm_pending_interim() is True

    assert sm.state == STT.State.PROMPT_WAIT
    assert sm.prompt_segments == ["are you able to hear me"]
    assert sm.last_segment_appended_at == last_interim_at
    assert sm.pending_interim is None
    assert any(event_type == "segment_confirmed" for event_type, _data in events)
    assert events[-1] == (
        "interim_transcript",
        {"transcript": "are you able to hear me", "eouProb": 0.1, "retry": -1},
    )


def test_rejected_retry_recovers_pending_interim() -> None:
    events: list[tuple[str, dict | None]] = []
    stt = STT(16000, 1, params=STTParams())
    stt.add_listener(lambda _stt, event_type, data: events.append((event_type, data)))

    sm = stt._state_machine
    last_interim_at = time.monotonic() - 5
    sm.state = STT.State.IDLE
    sm.pending_interim = {
        "transcript": "are you able to hear me",
        "eouProb": 0.8321,
        "audio": np.zeros(512, dtype=np.int16),
        "params": {},
        "lastInterimAt": last_interim_at,
        "segmentFrameCount": 1,
    }
    sm.last_interim_at = last_interim_at

    assert sm.recover_pending_interim_on_reject() is True

    assert sm.state == STT.State.PROMPT_WAIT
    assert sm.prompt_segments == ["are you able to hear me"]
    assert sm.last_segment_appended_at == last_interim_at
    assert sm.pending_interim is None
    assert any(event_type == "segment_confirmed" for event_type, _data in events)


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


@pytest.mark.parametrize(
    "state",
    [
        STT.State.SEGMENT_N,
        STT.State.SILENCE_0,
        STT.State.SILENCE_1,
        STT.State.SILENCE_2,
    ],
)
@pytest.mark.asyncio
async def test_watchdog_detects_stalled_unconfirmed_interim(state: STT.State) -> None:
    stt = AsyncSTT(16000, 1, params=STTParams(prompt_timeout_ms=1500))
    try:
        sm = stt._state_machine
        sm.pending_interim = {
            "transcript": "partial transcript",
            "eouProb": 0.1,
            "audio": np.zeros(512, dtype=np.int16),
            "params": {},
            "lastInterimAt": time.monotonic() - 10,
            "segmentFrameCount": 1,
        }
        sm.last_interim_at = sm.pending_interim["lastInterimAt"]
        sm.state = state

        assert stt._watchdog_interim_due(time.monotonic(), grace_seconds=0.2) is not None
    finally:
        stt.stop()


@pytest.mark.asyncio
async def test_watchdog_does_not_confirm_interim_after_segment_resumes() -> None:
    stt = AsyncSTT(16000, 1, params=STTParams(prompt_timeout_ms=1500))
    try:
        sm = stt._state_machine
        original_interim_at = time.monotonic() - 10
        sm.pending_interim = {
            "transcript": "partial transcript",
            "eouProb": 0.1,
            "audio": np.zeros(512, dtype=np.int16),
            "params": {},
            "lastInterimAt": original_interim_at,
            "segmentFrameCount": 1,
        }
        sm.last_interim_at = original_interim_at
        sm.state = STT.State.SEGMENT_N
        stt._segment = [
            np.zeros(512, dtype=np.int16),
            np.ones(512, dtype=np.int16),
        ]

        stt._locked_confirm_pending_interim(grace_seconds=0.2)

        assert sm.state == STT.State.SEGMENT_N
        assert sm.prompt_segments == []
        assert sm.pending_interim is not None
        assert sm.last_interim_at is not None
        assert sm.last_interim_at > original_interim_at
        assert len(stt._segment) == 2
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


# ── Voice-model preload race ─────────────────────────────────────────────────


def test_preload_shared_resources_serializes_concurrent_callers(monkeypatch) -> None:
    """`STT.preload_shared_resources` must serialize concurrent callers.

    Pre-fix, `_preload_stt`, `warmup_voice_models`, and `_ensure_pipeline`
    all kicked off as separate background tasks and all called
    `transformers.from_pretrained(low_cpu_mem_usage=True, ...)`. That path
    uses `accelerate.init_empty_weights()`, which monkey-patches
    `nn.Module.__init__` via a *global* (not thread-local) flag. Two
    concurrent loaders raced — one's exit re-enabled the meta-init for
    the other thread's modules — and the second loader's `.to(device)`
    blew up with `Cannot copy out of meta tensor; no data!`.

    The fix is a class-level `threading.Lock` held for the whole load.
    This test mocks the actual loading work and verifies that two
    concurrent calls don't overlap.
    """
    import threading
    import time
    import server.stt as stt_module
    from server.stt import STT

    # Reset class-level state so the lock is freshly created.
    STT.shared_resources = {}
    STT._load_lock = None

    concurrent_inside = 0
    max_concurrent = 0
    body_lock = threading.Lock()

    # Stub the *first* heavy load (Silero VAD via torch.hub.load) with a
    # slow body. If the lock is missing or scoped incorrectly, a second
    # thread will enter while the first is asleep here and `max_concurrent`
    # will read 2. The fake also marks the rest of the resources as
    # already-loaded so the asr/eou blocks short-circuit and the test
    # stays CI-cheap.
    def fake_silero_load(*_a, **_kw):
        nonlocal concurrent_inside, max_concurrent
        with body_lock:
            concurrent_inside += 1
            max_concurrent = max(max_concurrent, concurrent_inside)
        time.sleep(0.1)
        with body_lock:
            concurrent_inside -= 1
        STT.shared_resources["asr_model"] = object()
        STT.shared_resources["asr_processor"] = object()
        STT.shared_resources["assistant_model"] = object()
        STT.shared_resources["eou"] = object()
        return (object(), None)

    monkeypatch.setattr(stt_module.torch.hub, "load", fake_silero_load)

    threads = [
        threading.Thread(target=STT.preload_shared_resources)
        for _ in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert all(not t.is_alive() for t in threads), "preload threads deadlocked"
    assert max_concurrent == 1, (
        f"preload_shared_resources ran concurrently with itself "
        f"(max_concurrent={max_concurrent}) — STT._load_lock is missing or "
        f"isn't held over the whole guarded block. In production this "
        f"surfaces as 'Cannot copy out of meta tensor; no data!'."
    )


def test_kokoro_pipeline_shares_stt_load_lock() -> None:
    """`_ensure_pipeline` must acquire `STT._load_lock` so concurrent
    Kokoro/STT preloads serialize against each other.

    Both loaders ultimately call into transformers under
    accelerate.init_empty_weights; running them in parallel produces the
    same meta-tensor error as two concurrent STT loaders.
    """
    import inspect
    from server import tts_kokoro
    source = inspect.getsource(tts_kokoro._ensure_pipeline)
    assert "STT._load_lock" in source, (
        "_ensure_pipeline must hold STT._load_lock for the duration of "
        "the KPipeline() load — otherwise it races with concurrent STT "
        "preloads and one side ends up with meta-tensor parameters."
    )
