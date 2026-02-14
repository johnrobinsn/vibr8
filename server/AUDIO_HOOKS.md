# Audio Integration Points

All audio handling lives in `server/webrtc.py`. There are two hooks:
one for receiving audio from the browser, one for sending audio back.

## Receiving Audio (browser → server)

**File:** `server/webrtc.py` — `WebRTCManager._consume_audio()` (line ~179)

```python
async def _consume_audio(self, session_id, track, stats):
    while True:
        frame = await track.recv()  # <-- each frame is ~20ms of audio
        stats.log_frame(frame)
```

Each `frame` is an `av.AudioFrame`:
- `frame.to_ndarray()` → numpy array of PCM samples (shape depends on channels)
- `frame.sample_rate` → typically 48000
- `frame.samples` → number of samples per channel (typically 960 = 20ms)
- Format is Opus-decoded signed 16-bit

**To process incoming audio**, add your code after `stats.log_frame(frame)`:

```python
async def _consume_audio(self, session_id, track, stats):
    while True:
        frame = await track.recv()
        stats.log_frame(frame)

        # --- YOUR CODE HERE ---
        # Example: accumulate frames for speech-to-text
        pcm = frame.to_ndarray()  # numpy int16 array
        await self._on_audio_frame(session_id, pcm, frame.sample_rate)
```

## Sending Audio (server → browser)

**File:** `server/webrtc.py` — `TestToneTrack.recv()` (line ~85)

Currently sends a 440Hz sine wave. To send real audio (TTS, etc.),
replace `TestToneTrack` with your own `MediaStreamTrack` subclass:

```python
from aiortc import MediaStreamTrack
from av import AudioFrame
import numpy as np
import fractions
import asyncio

class MyAudioTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self):
        super().__init__()
        self._sample_rate = 48000
        self._samples_per_frame = 960  # 20ms
        self._pts = 0
        self._queue = asyncio.Queue()  # feed audio here

    async def push_audio(self, pcm: np.ndarray):
        """Push PCM samples (int16, mono) to be sent to the browser."""
        await self._queue.put(pcm)

    async def recv(self) -> AudioFrame:
        # Get next chunk from queue (or generate silence)
        try:
            pcm = await asyncio.wait_for(self._queue.get(), timeout=0.02)
        except asyncio.TimeoutError:
            pcm = np.zeros(self._samples_per_frame, dtype=np.int16)

        frame = AudioFrame.from_ndarray(
            pcm.reshape(1, -1), format="s16", layout="mono"
        )
        frame.pts = self._pts
        frame.sample_rate = self._sample_rate
        frame.time_base = fractions.Fraction(1, self._sample_rate)
        self._pts += len(pcm)

        await asyncio.sleep(0.02)  # pace to real-time
        return frame
```

Then in `handle_offer()`, swap `TestToneTrack` for your track:

```python
# line ~163 in handle_offer()
track = MyAudioTrack(session_id)
pc.addTrack(track)
self._outgoing_tracks[session_id] = track  # keep a reference to push audio
```

## Audio Frame Quick Reference

| Property | Value | Notes |
|----------|-------|-------|
| `frame.sample_rate` | 48000 | Opus default |
| `frame.samples` | 960 | 20ms at 48kHz |
| `frame.format.name` | `"s16"` | signed 16-bit int |
| `frame.layout.name` | `"mono"` | single channel |
| `frame.to_ndarray()` | `np.int16` array | shape `(1, 960)` for mono |
| `frame.pts` | int | presentation timestamp in sample units |
