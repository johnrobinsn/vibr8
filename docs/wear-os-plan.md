# vibr8 Wear OS Client — Plan

## Overview

Minimal vibr8 client for Pixel Watch 4 (Wear OS 5). Voice-first, focused on permission handling, session/node status, and direct WebRTC audio to the hub — independent of the phone.

## Architecture

```
Pixel Watch 4
├── WebSocket → wss://vibr8.ringzero.ai/ws/native/{clientId}
│   ├── Permission requests/responses
│   ├── Session status updates
│   ├── Node switching commands
│   └── RPC (bring_to_foreground, etc.)
├── WebRTC → wss://vibr8.ringzero.ai (signaling via REST)
│   ├── Outgoing: mic → AEC → opus → hub
│   └── Incoming: hub → opus → speaker
└── REST → https://vibr8.ringzero.ai/api/
    ├── Auth (login, token refresh)
    ├── Session list
    ├── Node list/switch
    └── WebRTC offer/answer
```

### Existing Infrastructure to Reuse

The hub already has native client support:

- **`/ws/native/{clientId}`** — Persistent WebSocket with 30s heartbeat, survives backgrounding. JSON command/response protocol (not NDJSON).
- **`_native_ws_by_client`** — Server-side registry with device fingerprinting and metadata.
- **RPC mechanism** — Server sends `{command, id, params}`, client responds `{id, result}`. Supports `bring_to_foreground`, `launch_app`, notifications, etc.
- **Client metadata** — Persisted to `~/.vibr8/clients.json` with role, device info, fingerprint.

The watch client would connect as a native client, reusing this entire infrastructure. No new hub endpoints needed for the basic flow.

### What's Missing Hub-Side

1. **Permission forwarding to native clients** — Currently permission requests only broadcast to browser WebSockets (`session.browser_sockets`). The native WS needs to receive them too, or the watch needs its own browser-style WS.
2. **Session status push** — Native clients don't currently receive `session_init`, `cli_connected`, `cli_disconnected`, or status changes. The watch needs a subscription mechanism.
3. **WebRTC signaling for native clients** — The existing `/api/webrtc/offer` endpoint works, but the watch needs to present the SDP offer/answer as a native client, not a browser session.

**Decision point:** Should the watch use `/ws/native/` (simpler, but missing session state push) or `/ws/browser/` (full session state, but designed for browser tabs)? A third option: extend `/ws/native/` with an opt-in subscription to session events.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Kotlin |
| UI | Jetpack Compose for Wear OS |
| WebSocket | OkHttp WebSocket client |
| WebRTC | Google's `libwebrtc` via `org.webrtc:google-webrtc` AAR |
| Audio | WebRTC's built-in AEC (uses Android's `AcousticEchoCanceler` + its own processing) |
| Auth | Wear OS Data Layer API (shared from phone app) or standalone token |
| Build | Gradle with Kotlin DSL |

## UI Design (Round Display)

### Home Screen (Idle)
```
    ┌─────────────┐
   │  ● Neo       │    ← Active node name + status dot
   │              │
   │   vibr8  🎤  │    ← Tap to talk / always-listening indicator
   │              │
   │  3 sessions  │    ← Session count
   │  all idle    │    ← Aggregate status
    └─────────────┘
```

### Permission Notification
```
    ┌─────────────┐
   │  ⚠ Permission│
   │              │
   │  Execute:    │
   │  npm test    │    ← Truncated command
   │              │
   │  [✓]   [✗]  │    ← Allow / Deny buttons
    └─────────────┘
```

### Session List (Swipe up)
```
    ┌─────────────┐
   │  Sessions    │
   │              │
   │  ● ring0     │    ← Green = running
   │  ○ main-feat │    ← Gray = idle
   │  ○ bugfix    │
   │              │
    └─────────────┘
```

### Node Switcher (Swipe right)
```
    ┌─────────────┐
   │  Nodes       │
   │              │
   │  ● Neo       │    ← Online
   │  ● docker1   │    ← Online
   │  ○ cloud-dev │    ← Offline
   │              │
    └─────────────┘
```

## Echo Cancellation

WebRTC's native Android integration handles this. The pipeline:

1. **`org.webrtc.AudioTrack`** plays incoming TTS audio through the speaker
2. **`org.webrtc.AudioRecord`** captures mic input
3. **WebRTC's APM (Audio Processing Module)** applies:
   - AEC (Acoustic Echo Cancellation) — removes speaker bleed from mic
   - NS (Noise Suppression) — ambient noise reduction
   - AGC (Automatic Gain Control) — volume normalization

This is the same pipeline Chrome uses on Android. The Pixel Watch 4's Qualcomm Snapdragon W5+ Gen 1 SoC supports hardware AEC, which WebRTC will use when available.

**Risk:** Watch speaker/mic proximity may challenge AEC. Mitigation: half-duplex mode as fallback (mute mic during TTS playback).

## Authentication

### Primary: Shared from Phone
- Wear OS Data Layer API syncs auth token from the vibr8 Android app
- Watch receives token via `DataClient` or `MessageClient`
- No login UI needed on watch

### Fallback: Standalone
- PIN-based login: hub generates a 6-digit code, user enters on watch
- Token stored in encrypted `SharedPreferences`
- Refresh via `/api/auth/refresh`

## Implementation Phases

### Phase 1: WebSocket + Permissions
- OkHttp WebSocket to `/ws/native/{clientId}`
- Receive and display permission requests
- Allow/deny response buttons
- Session list display (polling `/api/sessions`)
- Node list + switching
- Basic Compose UI with Horologist library

### Phase 2: WebRTC Voice
- `libwebrtc` integration
- SDP signaling via REST (`/api/webrtc/offer`)
- Push-to-talk (tap watch face to speak)
- TTS playback through watch speaker
- AEC validation on actual hardware

### Phase 3: Polish
- Shared auth from phone app via Data Layer
- Complications (Wear OS widgets) for session status
- Tile for quick permission approval
- Haptic feedback on permission arrival
- Battery optimization (ambient mode, connection management)

## Hub-Side Changes Needed

1. **Extend `/ws/native/` with session event subscriptions**
   - New command: `subscribe_sessions` with optional node filter
   - Push `permission_request`, `session_status`, `cli_connected/disconnected` to subscribed native clients
   - Or: let watch connect via `/ws/browser/` with a `role=watch` parameter

2. **WebRTC signaling for non-session-bound audio**
   - Current: `POST /api/webrtc/offer` requires `sessionId`
   - Watch: audio is global (same as browser), but the watch isn't "viewing" a session
   - Solution: allow `clientId`-based signaling (no session binding)

3. **Permission response from native clients**
   - Route: watch receives `permission_request` → user taps allow → send `permission_response` back through native WS or REST
   - Need: `POST /api/sessions/{id}/permissions/{reqId}/respond`

## Wire Protocol Versioning

With web, Android, and watch clients, the NDJSON/JSON WebSocket protocol should be formally specified:

- **Version header**: Clients send `{"type": "hello", "version": 1, "client": "wear-os/1.0"}` on connect
- **Spec location**: `docs/wire-protocol.md`
- **Scope**: Message types, required/optional fields, error codes
- **Backward compat**: Server supports N and N-1; clients declare version on connect

## Project Structure

```
wear/
├── app/
│   ├── src/main/
│   │   ├── java/ai/ringzero/vibr8/wear/
│   │   │   ├── MainActivity.kt
│   │   │   ├── data/
│   │   │   │   ├── HubClient.kt          # WebSocket + REST
│   │   │   │   ├── WebRTCClient.kt        # Voice pipeline
│   │   │   │   └── AuthRepository.kt      # Token management
│   │   │   ├── ui/
│   │   │   │   ├── HomeScreen.kt
│   │   │   │   ├── PermissionScreen.kt
│   │   │   │   ├── SessionListScreen.kt
│   │   │   │   └── NodeSwitcherScreen.kt
│   │   │   └── service/
│   │   │       └── HubConnectionService.kt  # Foreground service
│   │   ├── AndroidManifest.xml
│   │   └── res/
│   └── build.gradle.kts
├── build.gradle.kts
├── settings.gradle.kts
└── gradle/
```

## Session Strategy

**Recommendation: Spec first, then dedicated session.**

1. Create `wear/SPEC.md` from this plan — nail down the exact message formats, UI flows, and hub-side changes
2. Implement hub-side changes in the main vibr8 codebase (extend `/ws/native/`, add permission forwarding)
3. Spin up a dedicated coding session for the Wear OS app itself — give it the spec as context
4. The watch app is a separate Gradle project in `wear/` within the vibr8 repo (monorepo)

This keeps the watch implementation focused and prevents it from polluting the main vibr8 development flow.

## Open Questions

1. **Always-listening vs push-to-talk?** Battery vs convenience tradeoff. Start with push-to-talk; evaluate always-listening later with VAD duty cycling.
2. **Watch-only or phone companion required?** Target watch-only (WiFi/LTE direct to hub). Phone companion is a nice-to-have for auth sharing.
3. **Offline behavior?** Watch shows last-known state, queues permission responses. Reconnects automatically.
4. **Multiple watches?** Same `clientId` scheme as browser tabs — each watch is a unique client.
