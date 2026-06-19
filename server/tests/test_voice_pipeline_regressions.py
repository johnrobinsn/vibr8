import pytest
import numpy as np
import time

from server.stt import AsyncSTT, STT, STTParams
from server.webrtc import WebRTCManager
from vibr8_core.ws_bridge import WsBridge


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


@pytest.mark.asyncio
async def test_ws_bridge_uses_voice_service_tts_adapter() -> None:
    calls: list[tuple[str, str, bool]] = []

    class FakeVoiceServiceManager:
        def set_thinking_any(self, thinking):
            calls.append(("thinking", str(thinking), False))

        async def say_for_session(self, session_id, text, interrupt=True):
            calls.append((session_id, text, interrupt))
            return True

    class FakeTrack:
        def push_opus_frame(self, _frame):
            raise AssertionError("local TTS should not receive frames")

    bridge = WsBridge()
    bridge.set_webrtc_manager(FakeVoiceServiceManager())

    await bridge._speak_text("session-1", "**vibr8** ready", FakeTrack())

    assert ("session-1", "vibrate ready", True) in calls
    assert calls.count(("thinking", "False", False)) >= 1


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


# ── D1 + D2: polite-assistant TTS gating ────────────────────────────────────


class _StubWebRTCManager:
    """Minimal stand-in that exposes the surface WsBridge actually calls."""

    def __init__(self) -> None:
        self._last_voice_at_by_client: dict[str, float] = {}
        self.tts_muted_clients: set[str] = set()

    def last_voice_at_for_client(self, client_id: str) -> float:
        return self._last_voice_at_by_client.get(client_id, 0.0)

    def mark_voice(self, client_id: str, at: float | None = None) -> None:
        self._last_voice_at_by_client[client_id] = at if at is not None else time.time()

    def is_tts_muted(self, client_id: str) -> bool:
        return client_id in self.tts_muted_clients

    def set_thinking_any(self, *_: object) -> None:
        pass

    def get_any_outgoing_track(self) -> None:
        return None


class _StubTrack:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def push_opus_frame(self, frame: bytes) -> None:
        self.frames.append(frame)


def _make_bridge() -> "tuple[WsBridge, _StubWebRTCManager]":
    bridge = WsBridge()
    mgr = _StubWebRTCManager()
    bridge._webrtc_manager = mgr
    return bridge, mgr


# ─── D1: queue-before-start ──────────────────────────────────────────────


def test_enqueue_speak_proceeds_immediately_when_voice_is_quiet(monkeypatch) -> None:
    """No recent voice activity → TTS request bypasses the queue."""
    bridge, mgr = _make_bridge()
    track = _StubTrack()
    started: list[tuple[str, str]] = []

    async def fake_speak(session_id, text, t, client_id=""):
        started.append((session_id, client_id))
    monkeypatch.setattr(bridge, "_speak_text", fake_speak)
    monkeypatch.setattr("asyncio.ensure_future", lambda coro: coro.close() or None)

    bridge._enqueue_speak("s1", "hello", track, "client-1")
    assert bridge._deferred_tts == {}


def test_enqueue_speak_defers_when_voice_recently_active() -> None:
    """Voice within DEFERRED_TTS_VOICE_WINDOW_MS → request lands in queue."""
    bridge, mgr = _make_bridge()
    mgr.mark_voice("client-1")  # just now
    track = _StubTrack()

    bridge._enqueue_speak("s1", "hello", track, "client-1")
    queue = bridge._deferred_tts.get("client-1")
    assert queue and len(queue) == 1
    assert queue[0][0] == "s1" and queue[0][1] == "hello"


def test_enqueue_speak_skips_gate_when_client_id_missing(monkeypatch) -> None:
    """No client_id → can't check voice activity, so proceed immediately."""
    bridge, mgr = _make_bridge()
    mgr.mark_voice("client-1")  # active for client-1, but…
    track = _StubTrack()

    async def fake_speak(*_a, **_kw):
        return None
    monkeypatch.setattr(bridge, "_speak_text", fake_speak)
    monkeypatch.setattr("asyncio.ensure_future", lambda coro: coro.close() or None)

    # …enqueue with no client_id should NOT defer.
    bridge._enqueue_speak("s1", "hello", track, "")
    assert bridge._deferred_tts == {}


def test_drain_releases_after_quiet_window(monkeypatch) -> None:
    """After voice goes quiet for long enough, queued entry is released."""
    bridge, mgr = _make_bridge()
    track = _StubTrack()
    released: list[str] = []

    async def fake_speak(session_id, *_a, **_kw):
        released.append(session_id)
    monkeypatch.setattr(bridge, "_speak_text", fake_speak)

    # Run the coroutine that ensure_future would schedule, synchronously.
    def run_now(coro):
        try:
            coro.send(None)
        except StopIteration:
            pass
    monkeypatch.setattr("asyncio.ensure_future", run_now)

    # Queue it
    mgr.mark_voice("client-1")
    bridge._enqueue_speak("s1", "hello", track, "client-1")
    assert bridge._deferred_tts["client-1"]

    # Mark voice as old (older than quiet window)
    mgr.mark_voice("client-1", at=time.time() - 10)
    bridge._drain_deferred_tts_once()

    assert released == ["s1"]
    assert "client-1" not in bridge._deferred_tts


def test_drain_holds_entry_while_voice_remains_active(monkeypatch) -> None:
    """Voice still within quiet window → entry stays queued."""
    bridge, mgr = _make_bridge()
    track = _StubTrack()
    released: list[str] = []
    monkeypatch.setattr(bridge, "_speak_text", lambda *a, **kw: released.append("speak"))

    mgr.mark_voice("client-1")
    bridge._enqueue_speak("s1", "hello", track, "client-1")
    mgr.mark_voice("client-1")  # still active
    bridge._drain_deferred_tts_once()

    assert released == []
    assert len(bridge._deferred_tts["client-1"]) == 1


def test_drain_drops_stale_entries() -> None:
    """Entries older than DEFERRED_TTS_MAX_AGE_S are dropped without release."""
    bridge, mgr = _make_bridge()
    track = _StubTrack()
    very_old = time.time() - (bridge.DEFERRED_TTS_MAX_AGE_S + 5)
    bridge._deferred_tts["client-1"] = [("s1", "hello", track, very_old)]
    mgr.mark_voice("client-1", at=time.time() - 10)  # voice quiet

    bridge._drain_deferred_tts_once()
    assert "client-1" not in bridge._deferred_tts


def test_enqueue_speak_caps_queue_per_client() -> None:
    """Exceeding DEFERRED_TTS_MAX_PER_CLIENT evicts the oldest entry."""
    bridge, mgr = _make_bridge()
    mgr.mark_voice("client-1")
    track = _StubTrack()
    cap = bridge.DEFERRED_TTS_MAX_PER_CLIENT

    for i in range(cap + 2):
        bridge._enqueue_speak(f"s{i}", f"msg{i}", track, "client-1")

    queue = bridge._deferred_tts["client-1"]
    assert len(queue) == cap
    # Oldest evicted: queue starts at s2 (s0 and s1 dropped)
    assert queue[0][0] == f"s{cap + 2 - cap}"   # i.e. "s2"
    assert queue[-1][0] == f"s{cap + 1}"


# ─── D2: cancel-on-barge-in ────────────────────────────────────────────────


def test_cancel_tts_marks_session_so_in_flight_frames_drop() -> None:
    """on_frame must drop frames after cancel — `_tts_cancelled` is the gate."""
    bridge, _ = _make_bridge()
    bridge.cancel_tts("s1")
    assert "s1" in bridge._tts_cancelled


def test_cancel_tts_invokes_tts_engine_cancel() -> None:
    bridge, _ = _make_bridge()

    class FakeTTS:
        def __init__(self):
            self.cancelled = False
        def cancel(self):
            self.cancelled = True

    fake = FakeTTS()
    bridge._active_tts["s1"] = fake
    bridge.cancel_tts("s1")
    assert fake.cancelled is True
    assert "s1" not in bridge._active_tts


def test_cancel_tts_hard_cancels_speak_task() -> None:
    """Stored task handle must receive Task.cancel()."""
    bridge, _ = _make_bridge()

    class FakeTask:
        def __init__(self):
            self.cancelled = False
            self._done = False
        def cancel(self):
            self.cancelled = True
        def done(self):
            return self._done

    task = FakeTask()
    bridge._tts_tasks["s1"] = task  # type: ignore[assignment]
    bridge.cancel_tts("s1")
    assert task.cancelled is True
    assert "s1" not in bridge._tts_tasks


def test_cancel_tts_drops_deferred_entries_for_same_session() -> None:
    """Queued entries for the same session must be dropped on barge-in."""
    bridge, mgr = _make_bridge()
    track = _StubTrack()
    mgr.mark_voice("client-1")
    bridge._enqueue_speak("s1", "hello", track, "client-1")
    bridge._enqueue_speak("s2", "world", track, "client-1")
    assert len(bridge._deferred_tts["client-1"]) == 2

    bridge.cancel_tts("s1")

    # s1 dropped, s2 (different session) preserved.
    remaining = bridge._deferred_tts["client-1"]
    assert len(remaining) == 1
    assert remaining[0][0] == "s2"


def test_cancel_tts_clears_deferred_dict_when_only_entry_for_session() -> None:
    """If the only queued entry was for the cancelled session, the client
    key is removed entirely (no empty list left behind)."""
    bridge, mgr = _make_bridge()
    track = _StubTrack()
    mgr.mark_voice("client-1")
    bridge._enqueue_speak("s1", "hello", track, "client-1")

    bridge.cancel_tts("s1")

    assert "client-1" not in bridge._deferred_tts


# ─── WebRTCManager voice-activity tracking ────────────────────────────────


def test_webrtc_manager_mark_voice_records_timestamp_per_peer() -> None:
    mgr = WebRTCManager()
    mgr.mark_voice_active("clientA#tab1")
    assert mgr._last_voice_at["clientA#tab1"] > 0


def test_webrtc_manager_last_voice_for_client_maxes_across_peers() -> None:
    mgr = WebRTCManager()
    # Stub peer-list lookup so we don't need a full connection setup.
    mgr._peers_for_client = lambda cid: ["clientA#tab1", "clientA#tab2"]  # type: ignore[method-assign]
    older = time.time() - 5
    mgr._last_voice_at["clientA#tab1"] = older
    mgr._last_voice_at["clientA#tab2"] = older + 3
    assert mgr.last_voice_at_for_client("clientA") == older + 3


def test_webrtc_manager_last_voice_falls_back_to_client_id_when_no_peers() -> None:
    mgr = WebRTCManager()
    mgr._peers_for_client = lambda cid: []  # type: ignore[method-assign]
    mgr._last_voice_at["client-native"] = 123.456
    assert mgr.last_voice_at_for_client("client-native") == 123.456


# ─── _speak_text on_frame gate (integration: D2's race-window protection) ───


import asyncio as _asyncio_alias


class _FakeTTSEngine:
    """A TTS engine that produces frames synchronously via the handler.

    Mimics OpenAI's pattern: chunks arrive between cooperative cancel
    checks, so once a chunk has started processing, all its frames flow
    into ``on_frame`` even if ``cancel()`` is called partway through.
    The D2 protection is the ``on_frame`` gate inside ``_speak_text`` —
    this fake exercises that gate.
    """

    def __init__(self, frames_before_cancel: int, frames_after_cancel: int):
        self._opus_frame_handler = None
        self._cancelled = False
        self.frames_before = frames_before_cancel
        self.frames_after = frames_after_cancel

    def cancel(self):
        self._cancelled = True

    async def say(self, text):  # noqa: ARG002
        # Emit pre-cancel frames
        for _ in range(self.frames_before):
            if self._opus_frame_handler:
                self._opus_frame_handler(b"pre")
            await _asyncio_alias.sleep(0)
        # Simulate barge-in landing here — cancel was called externally,
        # but the in-flight HTTP chunk still produces these frames.
        for _ in range(self.frames_after):
            if self._opus_frame_handler:
                self._opus_frame_handler(b"post")
            await _asyncio_alias.sleep(0)


async def test_speak_text_on_frame_gate_drops_post_cancel_frames(monkeypatch) -> None:
    """Frames produced by an in-flight TTS chunk *after* cancel must not
    repopulate the audio buffer that barge-in just cleared.

    The gate is best-effort with respect to the in-flight frame at the
    exact moment the flag flips — at most one frame can slip through
    because the engine may have already emitted to `on_frame` before
    the next cooperative yield. What matters is that the gate then
    catches the remaining frames, not the precise one-frame boundary.
    """
    bridge, _ = _make_bridge()
    track = _StubTrack()

    pre_count, post_count = 3, 5
    fake = _FakeTTSEngine(frames_before_cancel=pre_count, frames_after_cancel=post_count)
    monkeypatch.setattr("server.tts.create_tts", lambda opus_frame_handler=None: fake)

    task = _asyncio_alias.create_task(
        bridge._speak_text("s1", "hello", track, "client-1"),
    )
    # Yield enough to flush the pre-cancel batch.
    for _ in range(pre_count + 1):
        await _asyncio_alias.sleep(0)
    # Barge-in: set the gate flag. Hard task-cancel as well so the
    # engine doesn't keep producing frames after we observe.
    bridge._tts_cancelled.add("s1")
    task.cancel()
    try:
        await _asyncio_alias.wait_for(task, timeout=1.0)
    except (_asyncio_alias.CancelledError, _asyncio_alias.TimeoutError):
        pass

    # All pre-cancel frames flowed through; post-cancel frames mostly
    # dropped. Allow at most one race-window leak.
    pre_received = track.frames.count(b"pre")
    post_received = track.frames.count(b"post")
    assert pre_received == pre_count, (
        f"pre-cancel frames lost: got {pre_received}, expected {pre_count}"
    )
    assert post_received <= 1, (
        f"too many post-cancel frames leaked through gate: {post_received} "
        f"(allowed: 1 in-flight, expected ≤ 1 of {post_count})"
    )


async def test_speak_text_registers_and_clears_task_handle(monkeypatch) -> None:
    """`_tts_tasks[session_id]` should hold the running task during
    `_speak_text` and be cleared in the finally block."""
    bridge, _ = _make_bridge()
    track = _StubTrack()

    # Use a hanging engine so we can observe the registration window
    # before the task completes.
    started = _asyncio_alias.Event()
    release = _asyncio_alias.Event()

    class HangingTTS:
        def __init__(self):
            self._opus_frame_handler = None
            self._cancelled = False
        def cancel(self):
            self._cancelled = True
        async def say(self, _text):
            started.set()
            await release.wait()

    monkeypatch.setattr("server.tts.create_tts", lambda opus_frame_handler=None: HangingTTS())

    task = _asyncio_alias.create_task(
        bridge._speak_text("s1", "hello", track, "client-1"),
    )
    await started.wait()
    # While the engine is hanging, the task handle is registered.
    assert bridge._tts_tasks.get("s1") is task

    release.set()
    await task
    # After completion, handle is cleared.
    assert "s1" not in bridge._tts_tasks


async def test_speak_text_clears_active_tts_on_normal_completion(monkeypatch) -> None:
    bridge, _ = _make_bridge()
    track = _StubTrack()
    fake = _FakeTTSEngine(frames_before_cancel=2, frames_after_cancel=0)
    monkeypatch.setattr("server.tts.create_tts", lambda opus_frame_handler=None: fake)

    await bridge._speak_text("s1", "hello", track, "client-1")
    assert "s1" not in bridge._active_tts
    assert "s1" not in bridge._tts_tasks


async def test_speak_text_cancellation_propagates_through_finally(monkeypatch) -> None:
    """A hard Task.cancel() must let the finally block clean state up,
    not leave _tts_tasks/_active_tts populated."""
    bridge, _ = _make_bridge()
    track = _StubTrack()

    class HangingTTS:
        def __init__(self):
            self._opus_frame_handler = None
            self._cancelled = False
        def cancel(self):
            self._cancelled = True
        async def say(self, text):  # noqa: ARG002
            await _asyncio_alias.sleep(10)  # would hang without external cancel

    fake = HangingTTS()
    monkeypatch.setattr("server.tts.create_tts", lambda opus_frame_handler=None: fake)

    task = _asyncio_alias.create_task(
        bridge._speak_text("s1", "hello", track, "client-1"),
    )
    for _ in range(3):
        await _asyncio_alias.sleep(0)
    assert "s1" in bridge._tts_tasks

    bridge.cancel_tts("s1")

    with pytest.raises(_asyncio_alias.CancelledError):
        await task

    # finally block must have cleared both maps even though the task
    # exited via CancelledError.
    assert "s1" not in bridge._tts_tasks
    assert "s1" not in bridge._active_tts


# ─── Deferred-TTS drain loop lifecycle ────────────────────────────────────


async def test_drain_loop_exits_when_queue_drains(monkeypatch) -> None:
    """Loop self-stops to avoid spinning forever after the queue empties."""
    bridge, mgr = _make_bridge()
    bridge.DEFERRED_TTS_TICK_S = 0.01  # speed up

    monkeypatch.setattr(bridge, "_speak_text", lambda *a, **kw: None)
    monkeypatch.setattr("asyncio.ensure_future", lambda coro: coro.close() or None)

    bridge._ensure_deferred_tts_drainer()
    assert bridge._deferred_tts_task is not None
    # No queue → first tick exits the loop.
    await _asyncio_alias.sleep(0.05)
    assert bridge._deferred_tts_task.done()


async def test_drain_loop_self_restarts_after_emptying(monkeypatch) -> None:
    """After the loop exits idle, the next enqueue must rearm it."""
    bridge, mgr = _make_bridge()
    bridge.DEFERRED_TTS_TICK_S = 0.01
    track = _StubTrack()
    released: list[str] = []
    monkeypatch.setattr(bridge, "_speak_text", lambda *a, **kw: released.append("speak"))
    monkeypatch.setattr("asyncio.ensure_future", lambda coro: coro.close() or None)

    # Round 1: queue, drain, idle exit.
    bridge._ensure_deferred_tts_drainer()
    await _asyncio_alias.sleep(0.05)
    first_task = bridge._deferred_tts_task
    assert first_task is not None and first_task.done()

    # Round 2: queue again, drainer must restart.
    mgr.mark_voice("client-1")
    bridge._enqueue_speak("s1", "hello", track, "client-1")
    assert bridge._deferred_tts_task is not first_task
    assert not bridge._deferred_tts_task.done()
    # Clean up so pytest doesn't leak the task.
    bridge._deferred_tts_task.cancel()
    try:
        await bridge._deferred_tts_task
    except _asyncio_alias.CancelledError:
        pass


async def test_close_all_cancels_speak_tasks_and_drainer(monkeypatch) -> None:
    """`close_all()` must hard-cancel TTS tasks, clear queues, and stop
    the drainer so shutdown isn't blocked on lingering coroutines."""
    bridge, mgr = _make_bridge()
    bridge.DEFERRED_TTS_TICK_S = 0.01
    track = _StubTrack()
    monkeypatch.setattr(bridge, "_speak_text", lambda *a, **kw: None)
    monkeypatch.setattr("asyncio.ensure_future", lambda coro: coro.close() or None)

    # Plant a fake task + queue entry
    class FakeTask:
        def __init__(self):
            self.cancelled = False
        def cancel(self):
            self.cancelled = True
        def done(self):
            return False

    fake_task = FakeTask()
    bridge._tts_tasks["s1"] = fake_task  # type: ignore[assignment]
    bridge._tts_cancelled.add("s1")
    mgr.mark_voice("client-1")
    bridge._enqueue_speak("s2", "hi", track, "client-1")
    bridge._ensure_deferred_tts_drainer()
    drainer = bridge._deferred_tts_task

    await bridge.close_all()

    assert fake_task.cancelled is True
    assert bridge._tts_tasks == {}
    assert bridge._tts_cancelled == set()
    assert bridge._deferred_tts == {}
    assert bridge._deferred_tts_task is None
    if drainer is not None:
        # Let the cancelled drainer task observe the cancel and exit.
        try:
            await _asyncio_alias.wait_for(drainer, timeout=1.0)
        except (_asyncio_alias.CancelledError, _asyncio_alias.TimeoutError):
            pass
        assert drainer.done() or drainer.cancelled()
