# Voice Pipeline

Technical reference for the vibr8 voice system. Covers the full path from browser microphone capture through speech recognition to transcript delivery, plus TTS audio output back to the client.

## Architecture Overview

```
Browser                          Server                              Claude CLI
  |                                |                                    |
  |  WebRTC (Opus, 48kHz, 20ms)   |                                    |
  |------------------------------>|                                    |
  |                                |  _consume_audio()                  |
  |                                |    batch 8 frames (160ms)          |
  |                                |    stt.process_buffer()            |
  |                                |      mono -> gain -> resample      |
  |                                |      RMS gate -> Silero VAD        |
  |                                |      state machine                 |
  |                                |        -> process_segment()        |
  |                                |          Whisper -> filters -> EOU |
  |                                |                                    |
  |                                |  _on_stt_event("final_transcript") |
  |                                |    guard word + command check      |
  |                                |    voice mode interception         |
  |                                |    _submit_text()                  |
  |                                |      ws_bridge.submit_user_message |
  |                                |----------------------------------->|
  |                                |                                    |
  |  WebRTC (Opus TTS playback)    |  QueuedAudioTrack                  |
  |<------------------------------|    TTS_OpenAI -> Ogg/Opus parse     |
```

## 1. Client-Side Audio Capture

**File:** `web/src/webrtc.ts`

The browser captures audio via `getUserMedia` with processing constraints enabled:

```
echoCancellation: true
noiseSuppression: true
autoGainControl: true
```

The raw `MediaStreamTrack` is added directly to the `RTCPeerConnection` -- there is no client-side AudioWorklet, ScriptProcessor, or manual chunking. The browser's WebRTC stack handles Opus encoding transparently.

**Codec:** Opus (auto-negotiated by WebRTC)
**Sample rate:** 48 kHz (browser default for WebRTC)
**Frame duration:** 20ms (960 samples)

### Signaling

1. Browser creates an SDP offer via `pc.createOffer()`
2. Waits up to 10 seconds for ICE gathering to complete (to include relay candidates)
3. Sends the offer to `POST /api/webrtc/offer` with the client ID
4. Server creates a peer connection, generates an SDP answer
5. Browser sets the answer as the remote description

ICE servers (STUN/TURN) are fetched from the backend at connection time.

## 2. Server-Side Peer Connection

**File:** `server/webrtc.py`, `handle_offer()` (line 377)

On receiving the browser's SDP offer, the server:

1. Configures ICE servers from `~/.vibr8/ice-servers.json`
2. Creates an `RTCPeerConnection` via aiortc
3. Sets up an `AsyncSTT` instance with the user's voice profile parameters
4. Creates a `QueuedAudioTrack` for outgoing TTS audio and adds it to the peer connection
5. Listens for the incoming audio track via the `@pc.on("track")` event

```python
stt = AsyncSTT(sample_rate=48000, num_channels=2, params=stt_params)
outgoing = QueuedAudioTrack(client_id)
pc.addTrack(outgoing)
```

## 3. Audio Reception and Batching

**File:** `server/webrtc.py`, `_consume_audio()` (line 946)

The server receives audio frames from the WebRTC track in a loop:

```python
while True:
    frame = await track.recv()       # 20ms frame from aiortc
    pcm = frame.to_ndarray()         # shape (2, 960), dtype int16
    audio_buffer.append(pcm)
    if len(audio_buffer) >= 8:       # batch threshold
        batch = np.concatenate(audio_buffer, axis=1)  # shape (2, 7680)
        stt.process_buffer(batch)
        audio_buffer = []
```

| Parameter | Value |
|-----------|-------|
| Input sample rate | 48,000 Hz |
| Channels | 2 (stereo -- aiortc always delivers stereo) |
| Frame size | 960 samples (20ms) |
| Batch size | 8 frames |
| **Batch duration** | **160ms** |
| Batch shape | `(2, 7680)` int16 |

On track end (disconnect), any remaining frames in the buffer are flushed to the STT, and `stt.flush()` is called to reset the state machine.

## 4. STT Processing Pipeline

**File:** `server/stt.py`

### Threading Model

`AsyncSTT` wraps the synchronous `STT` class. All heavy processing (VAD, Whisper inference, EOU evaluation) runs on a dedicated `ThreadWorker` thread, protected by a `threading.Lock`. Event notifications are dispatched back to the asyncio event loop via `asyncio.run_coroutine_threadsafe()`.

### Shared Resources

Three ML models are loaded once and shared across all STT instances:

| Resource | Model | Purpose |
|----------|-------|---------|
| `vad` | Silero VAD (`snakers4/silero-vad`) | Voice activity detection |
| `asr` | `openai/whisper-large-v3` | Speech-to-text transcription |
| `eou` | End-of-utterance model | Utterance boundary detection |

Whisper runs on CUDA if available, otherwise CPU. Language is hardcoded to English, task to transcribe.

### `process_buffer()` (line 170)

Each 160ms batch goes through these stages in order:

#### Step 1: Mono Conversion

```python
mono = buffer.mean(axis=0)            # average channels
mono = ((mono[::2] + mono[1::2]) / 2) # stereo deinterleave + average
```

#### Step 2: Mic Gain

```python
mono = clip(mono * mic_gain, int16_min, int16_max)
```

Default gain: `1.0` (unity). Applied before resampling.

#### Step 3: Resampling

```python
resampled = resample_poly(mono, up=16000, down=48000)  # 48kHz -> 16kHz
```

Uses `scipy.signal.resample_poly` (polyphase filter). Both Whisper and Silero VAD require 16 kHz input.

#### Step 4: RMS Silence Gate

```python
rms = sqrt(mean(float_buf^2))
rms_db = 20 * log10(max(rms, 1e-10))
is_silent = rms_db < vad_threshold_db     # default: -30.0 dB
```

If the signal is below the RMS threshold, Silero VAD is skipped entirely. The `voice_level` event is emitted with `rmsDb` for the UI mic meter regardless.

#### Step 5: Silero VAD

Only runs if the RMS gate passes:

```python
float_buf_2d = float_buf.reshape(-1, 512)           # 512-sample chunks (32ms each)
probabilities = vad(torch.from_numpy(float_buf_2d), 16000)
silero_prob = float(torch.max(probabilities))
is_silent = bool(torch.all(probabilities < silero_vad_threshold))  # default: 0.4
```

| Parameter | Value |
|-----------|-------|
| VAD chunk size | 512 samples (32ms at 16kHz) |
| Threshold | 0.4 (probability) |
| Reset interval | 30 seconds of continuous silence in IDLE |

The Silero VAD hidden state is reset after 30 seconds of sustained silence to prevent RNN drift from causing false voice detections on ambient noise.

#### Step 6: State Machine Dispatch

```python
event = VOICE_NOT_DETECTED if is_silent else VOICE_WAS_DETECTED
state_machine.handle_event(event, segment_buffer, resampled_audio)
capture_time += 0.160   # advance by 160ms
```

## 5. State Machine

The state machine tracks voice activity and determines when to trigger transcription.

### States

| State | Description |
|-------|-------------|
| `IDLE` | No voice activity. Waiting for speech. |
| `SEGMENT_0` | First voice frame detected. May be a noise burst -- not yet confirmed. |
| `SEGMENT_N` | Confirmed voice. Actively accumulating speech audio. |
| `SILENCE_0` | 1st silence frame after voice. Speaker may be pausing. |
| `SILENCE_1` | 2nd consecutive silence frame. |
| `SILENCE_2` | 3rd consecutive silence frame. Next silence triggers transcription. |

### Transition Table

Each transition receives the segment buffer and the current resampled audio chunk as arguments.

| Current State | Event | Next State | Action |
|---------------|-------|------------|--------|
| `IDLE` | `VOICE_NOT_DETECTED` | `IDLE` | Clear segment buffer |
| `IDLE` | `VOICE_WAS_DETECTED` | `SEGMENT_0` | `capture_segment()` -- record start time, append audio |
| `SEGMENT_0` | `VOICE_NOT_DETECTED` | `IDLE` | Clear segment buffer (discard noise burst) |
| `SEGMENT_0` | `VOICE_WAS_DETECTED` | `SEGMENT_N` | `voice_was_detected()` -- append audio, emit event, reset EOU counters |
| `SEGMENT_N` | `VOICE_NOT_DETECTED` | `SILENCE_0` | Append audio to segment |
| `SEGMENT_N` | `VOICE_WAS_DETECTED` | `SEGMENT_N` | Append audio to segment |
| `SILENCE_0` | `VOICE_NOT_DETECTED` | `SILENCE_1` | Append audio to segment |
| `SILENCE_0` | `VOICE_WAS_DETECTED` | `SEGMENT_N` | Append audio to segment (resume speaking) |
| `SILENCE_1` | `VOICE_NOT_DETECTED` | `SILENCE_2` | Append audio to segment |
| `SILENCE_1` | `VOICE_WAS_DETECTED` | `SEGMENT_N` | Append audio to segment (resume speaking) |
| `SILENCE_2` | `VOICE_NOT_DETECTED` | `IDLE` | **`process_segment()`** -- run Whisper + EOU |
| `SILENCE_2` | `VOICE_WAS_DETECTED` | `SEGMENT_N` | Append audio to segment (resume speaking) |

### Key Behaviors

- **Noise rejection:** A single voice frame (`SEGMENT_0`) followed by silence returns to `IDLE` and discards the audio.
- **Silence tolerance:** Up to 3 consecutive silence frames (~480ms at 160ms per buffer) are tolerated before triggering transcription. If voice resumes during `SILENCE_0/1/2`, the state returns to `SEGMENT_N` and the audio is preserved.
- **EOU retry re-entry:** When EOU evaluation fails (score below threshold), `process_segment()` overrides the state back to `SEGMENT_N`, allowing the silence chain to cycle again and accumulate more speech.

## 6. Segment Processing and EOU

**File:** `server/stt.py`, `process_segment()` (line 265)

Called when the state machine transitions from `SILENCE_2` to `IDLE`. This is where transcription and utterance boundary detection happen.

### Step 1: Minimum Duration Check

```python
duration = segment_time_end - segment_time_begin
if duration < min_segment_duration:   # default: 0.4s
    discard segment
    return
```

Segments shorter than 400ms are discarded as likely noise.

### Step 2: Whisper Inference

```python
combined = np.concatenate(segment_frames, axis=0)
float_buf = combined.astype(np.float32) / 32767
text = asr(float_buf, return_timestamps=True)["text"]
```

The entire accumulated segment (all frames from `SEGMENT_0` through the current silence) is concatenated and fed to Whisper as a single inference pass.

### Step 3: Hallucination Filters

Five cascading filters reject common Whisper false positives. If any filter triggers, the segment is discarded and the state machine returns to `IDLE`.

**a) Known Pattern Filter**
Rejects exact matches against a set of ~40 common hallucinations:
- YouTube-style: "thank you for watching", "subscribe", "like and subscribe"
- Short utterances: "oh", "hmm", "um", "yeah", "okay", "hey"
- Service text: "transcription by castingwords", "amara org"

**b) Non-Latin Script Filter**
Rejects text where less than 50% of characters are ASCII/Latin. Catches Korean, Chinese, Japanese, and other non-English hallucinations.

**c) Repetition Loop Filter**
Rejects text where 4+ sentences are present and the most common sentence accounts for more than 60% of all sentences (e.g., "Oh my God." repeated 89 times).

On any rejection, a `voice_not_detected` event is emitted.

### Step 4: EOU Evaluation

```python
eou_prob = eou_model(text)

if eou_prob < eou_threshold and eou_counter < eou_max_retries:
    # Utterance not complete -- keep listening
    emit("interim_transcript", {transcript, eouProb, retry})
    state = SEGMENT_N
    eou_counter += 1
    return
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `eou_threshold` | 0.15 | Minimum EOU probability to accept as a complete utterance |
| `eou_max_retries` | 3 | Maximum times to retry before dispatching anyway |

**Retry flow:**

1. EOU score is below threshold (e.g., 0.08 < 0.15)
2. `interim_transcript` event is emitted (for UI preview)
3. State is forced back to `SEGMENT_N`, retry counter incremented
4. The silence chain cycles again (SEGMENT_N -> SILENCE_0 -> SILENCE_1 -> SILENCE_2 -> process_segment), accumulating ~480ms more audio
5. Whisper re-evaluates the now-larger segment with updated EOU score
6. After `eou_max_retries` retries, the transcript is dispatched regardless of EOU score

**Reset points:** `eou_counter` is reset to zero when:
- New voice is confirmed (`voice_was_detected()` action in `SEGMENT_0` -> `SEGMENT_N` transition)

### Step 5: Final Dispatch

When EOU passes (or retries are exhausted):

```python
emit("voice_not_detected")
emit("final_transcript", {
    timeBegin,
    timeEnd,
    transcript: text,
    audio: combined,          # raw PCM for logging
    params: stt_params_dict,  # snapshot of active params
    eouProb: float,
})
segment.clear()
```

## 7. Events

The STT pipeline emits these events to registered listeners:

| Event | Data | When |
|-------|------|------|
| `voice_level` | `{rmsDb: float}` | Every `process_buffer()` call (160ms) |
| `voice_was_detected` | `None` | `SEGMENT_0` -> `SEGMENT_N` transition |
| `voice_not_detected` | `None` | Segment rejected or accepted |
| `interim_transcript` | `{transcript, eouProb, retry}` | EOU retry (below threshold, retries remain) |
| `final_transcript` | `{timeBegin, timeEnd, transcript, audio, params, eouProb}` | Utterance accepted |
| `flushed` | `None` | State machine manually reset |

## 8. Transcript Routing

**File:** `server/webrtc.py`, `_on_stt_event()` (line 594)

When `final_transcript` fires, the handler runs in sequence:

### 8.1 Guard Word Detection

Scans the lowered transcript for "vibr8" or "vibrate". If found, splits the transcript into `pre_text` (before the guard word) and `after_word` (after it).

### 8.2 Command Matching

If a guard word is found, `after_word` is checked against known commands:

| Command | Action |
|---------|--------|
| `done` | Finishes active voice mode, submits accumulated text |
| `off` | Disables audio (broadcasts `audio_off`) |
| `guard` | Enables guard mode |
| `listen` | Disables guard mode |
| `quiet` | Mutes TTS output |
| `speak` | Unmutes TTS output |
| `ring zero on` | Enables Ring0 |
| `ring zero off` | Disables Ring0 |
| `note` | Enters NoteMode (accumulates speech fragments) |
| `node <name>` | Switches active remote node |

**Escape sequences:**
- `"vibr8 vibrate ..."` -> submits `"vibrate ..."` (strips outer guard word)
- `"vibr8 app ..."` -> submits `"vibr8 ..."` (transforms "app" to "vibr8")

If a command matches, any `pre_text` before the guard word is submitted as input first.

If no command matches, the **entire transcript passes through unmodified** (guard word included).

### 8.3 Voice Mode Interception

If a voice mode is active (e.g., NoteMode), the transcript is routed to the mode's `on_transcript()` handler instead of being submitted. Only the `done` command can exit a voice mode.

**NoteMode** accumulates all transcripts as fragments. On `done`, it joins them with newlines and submits as `"[voice note]\n<text>"`.

### 8.4 Guard Enforcement

If guard mode is enabled and no guard word was found in the transcript, the entire transcript is silently discarded.

### 8.5 Submission

The transcript is submitted via `_submit_text()`:

1. **Ring0 enabled + remote node active:** Tunnels to the remote node as a `ring0_input` message
2. **Ring0 enabled (local):** Submits to the Ring0 session via `ws_bridge.submit_user_message(ring0_sid, text)`
3. **Ring0 disabled:** Submits to the client's current session via `ws_bridge.submit_user_message(session_id, text)`

`submit_user_message()` both delivers the text to the CLI process and broadcasts a `user_message` (with `source: "voice"`) to all connected browsers so the transcript appears in the chat UI.

### 8.6 Interim Transcript Preview

When `interim_transcript` fires (during EOU retries), the handler broadcasts a `voice_transcript_preview` message to browsers for the target session (Ring0 or current). This is suppressed during voice modes (e.g., NoteMode). The preview is cleared when the final `user_message` with `source: "voice"` arrives.

### 8.7 Barge-In

When `voice_was_detected` fires, `barge_in(client_id)` is called, which cancels any in-progress TTS playback for that client.

## 9. TTS Output Pipeline

### TTS Synthesis

**File:** `server/tts.py`

| Parameter | Value |
|-----------|-------|
| API | OpenAI `/v1/audio/speech` |
| Model | `tts-1-hd` |
| Voice | `echo` |
| Format | `opus` (native Ogg/Opus container) |
| Speed | 1.0 |
| Stream chunk size | 16,384 bytes |

The response is streamed as an Ogg/Opus container. An `_OggProcessor` incrementally parses Ogg pages, skips `OpusHead` and `OpusTags` metadata pages, and extracts audio data segments. Each segment (a raw Opus frame) is delivered to the frame handler callback immediately.

TTS can be cancelled mid-stream via `tts.cancel()` (e.g., on barge-in).

### Outgoing Audio Track

**File:** `server/audio_track.py`

`QueuedAudioTrack` is an aiortc `MediaStreamTrack` that serves Opus frames to the WebRTC transport:

| Parameter | Value |
|-----------|-------|
| Sample rate | 48,000 Hz |
| Frame duration | 20ms |
| Samples per frame | 960 |
| Silence frame | `f8fffe` (3 bytes, Opus DTX) |

**`push_opus_frame(frame)`** enqueues a raw Opus frame for playback.

**`recv()`** is called by the WebRTC transport at ~20ms intervals:
1. Pops the next frame from the queue
2. If empty: returns a thinking tone frame (if thinking is enabled) or a silence frame
3. Sets PTS/DTS timestamps (incrementing by 960 samples per frame)
4. Paces output to real-time using wall-clock sleep

### Thinking Tone

A very quiet audio loop played when the agent is processing (between barge-in and response):

| Parameter | Value |
|-----------|-------|
| Frequency | 280 Hz (warm, between C4 and D4) |
| Modulation | 0.5 Hz amplitude envelope ("breathing") |
| Amplitude | 0.025 (2.5% of full scale) |
| Loop duration | 2 seconds |
| Encoding | Opus, 48kHz, mono, s16 |

The tone is pre-generated and cached. `set_thinking(True)` enables the loop; frames cycle when the queue is empty.

## 10. Voice Profiles

**File:** `server/voice_profiles.py`

Voice profiles store per-user STT tuning parameters as JSON files at `~/.vibr8/data/voice/profiles/{username}/{profile_id}.json`.

### Parameter Mapping

| Profile Field (camelCase) | STTParams Field (snake_case) | Default |
|---------------------------|------------------------------|---------|
| `micGain` | `mic_gain` | 1.0 |
| `vadThresholdDb` | `vad_threshold_db` | -30.0 |
| `sileroVadThreshold` | `silero_vad_threshold` | 0.4 |
| `eouThreshold` | `eou_threshold` | 0.15 |
| `eouMaxRetries` | `eou_max_retries` | 3 |
| `minSegmentDuration` | `min_segment_duration` | 0.4 |

If no profile is active, defaults are used. The playground WebSocket handler allows live parameter updates during testing.

## 11. Timing Summary

End-to-end latency from end of speech to transcript delivery:

| Stage | Duration | Notes |
|-------|----------|-------|
| Silence detection | 3 x 160ms = **480ms** | Three silence buffers to reach `SILENCE_2` -> `IDLE` |
| Whisper inference | **200-800ms** | Depends on segment length and GPU |
| EOU evaluation | **~10ms** | Lightweight model |
| EOU retries (worst case) | 3 x (480ms + inference) | Up to 3 additional cycles |
| Network + CLI delivery | **<10ms** | Local WebSocket hop |

**Typical case (EOU passes first try):** ~700-1300ms from end of speech to transcript delivery.

**Worst case (3 EOU retries):** ~3-5 seconds, but the utterance is likely mid-sentence and the delay allows the speaker to finish naturally.
