# ADB Android Node Support — Architecture Plan

> Status: Plan finalized, not yet implemented.
> Last updated: 2026-04-09

## Overview

Add Android device support to vibr8 via ADB. Android devices appear as "nodes" in the UI but differ from desktop nodes — they can't run Claude Code sessions locally. The hub manages ADB connections directly, streams the phone screen via scrcpy, and runs computer-use agents that control the phone via scrcpy's binary input protocol.

Four interconnected features:
1. **Node capability flags** — Distinguish desktop nodes (can run sessions) from Android nodes (can't)
2. **Host-node session proxying** — Sessions targeting Android nodes run on the hub, tagged with the node
3. **Phone screen viewing** — Live scrcpy video stream displayed in a "Phone" tab
4. **Computer-use via scrcpy** — VLM agent controls Android via scrcpy input injection

---

## 1. Android Node Model

### Design Decision: Virtual Nodes (No Separate Process)

Android nodes are **virtual nodes registered by the hub itself**. There is no `vibr8_node` process for Android — the hub owns the ADB/scrcpy connection directly. The "node" in the registry is a logical entity for UI/routing purposes.

Rationale: The phone is connected to the hub machine (USB or same LAN). Spinning up a fake node_agent process to talk to localhost ADB adds unnecessary complexity.

### Node Registration

Add a new node type to `NodeRegistry`:

```python
class AndroidNode:
    id: str                    # UUID
    name: str                  # User-given name (e.g., "Pixel 9")
    node_type: "android"       # Distinguishes from remote desktop nodes
    connection_mode: str       # "usb" | "ip" | "mdns"
    device_id: str             # ADB serial (e.g., "XXXXXXXX" or "192.168.1.50:5555")
    status: str                # "online" | "offline"
    capabilities: {
        "canRunSessions": False,
        "hasDisplay": True,
        "nodeType": "android",
        "model": str,          # e.g., "Pixel 9 Pro"
        "androidVersion": str, # e.g., "15"
        "screenWidth": int,
        "screenHeight": int,
    }
    # Connection config (persisted, editable)
    ip: str | None             # For IP/mDNS mode
    port: int | None           # For IP/mDNS mode
```

Persisted to `~/.vibr8/android-nodes.json` (separate from `nodes.json` which tracks remote desktop nodes).

### Capability Flags

Add `canRunSessions: bool` to all nodes' capabilities:

| Node Type | `canRunSessions` | `hasDisplay` | `nodeType` |
|---|---|---|---|
| Desktop (remote) | `true` | `true` | `"desktop"` |
| Android (ADB) | `false` | `true` | `"android"` |
| Headless (remote) | `true` | `false` | `"desktop"` |

**Where this flag is checked:**
- `routes.py` session creation — if target node has `canRunSessions: false`, create session on host with `associatedNodeId` (see section 2)
- `ring0_mcp.py:create_session` — same logic via the API
- Frontend — hide "create session on this node" for nodes without the capability

### Three Connection Modes

#### USB Auto-Detect
- Periodic poll of `adb devices` (every 5 seconds via background task)
- New USB devices appear in the "Add Android Device" dialog
- Device serial used as `device_id`
- Already-registered devices update status (online/offline)

#### IP/Port (Wireless ADB)
- User enters IP and port manually in the UI
- Hub runs `adb connect {ip}:{port}` to establish connection
- Connection settings are persisted and editable (IP/port can change between sessions)
- Reconnect button in the UI for when the device disconnects

#### mDNS/Bonjour Discovery
- Use the `zeroconf` Python library to discover `_adb-tls-connect._tcp` services on the LAN
- Discovered devices appear dynamically in the "Add Android Device" dialog
- Android 11+ devices with wireless debugging enabled advertise via mDNS
- After the user selects a discovered device, persist its connection info

### API Endpoints

```
GET  /api/android/devices              → List registered Android nodes (with online/offline status)
POST /api/android/devices              → Register a new Android node (name, connection_mode, ip, port, device_id)
PUT  /api/android/devices/{id}         → Update connection settings (ip, port, name)
DELETE /api/android/devices/{id}       → Remove an Android node

GET  /api/android/discover             → List discoverable devices (USB + mDNS), excluding already-registered ones
POST /api/android/connect              → Manually connect wireless ADB: { ip, port }
POST /api/android/disconnect/{id}      → Disconnect wireless ADB

GET  /api/android/devices/{id}/status  → Connection health check (adb shell echo ok)
```

### Frontend: Android Device Management

New section in Settings page (or a dedicated panel accessible from the sidebar node list):

- **"Add Android Device" button** → opens dialog with three tabs: USB, IP/Port, Discover
  - **USB tab**: Dropdown of detected USB devices (from `/api/android/discover`)
  - **IP/Port tab**: Text fields for IP address and port, "Connect" button
  - **Discover tab**: Auto-refreshing list of mDNS-discovered devices, "Add" button per device
- **Device list**: Each registered Android node shows:
  - Name (editable inline)
  - Device model + Android version
  - Online/offline indicator (green/red dot)
  - When offline: gear icon to edit connection settings (IP/port may have changed)
  - Delete button

### Files to Create / Modify

| File | Change |
|---|---|
| `server/android_registry.py` | **New.** `AndroidRegistry` class — registration, connection management, status tracking, persistence |
| `server/adb_utils.py` | **New.** Low-level ADB helpers: `list_devices()`, `connect()`, `disconnect()`, `device_info()`, `shell()` |
| `server/mdns_discovery.py` | **New.** mDNS/Bonjour discovery for ADB devices using `zeroconf` |
| `server/node_registry.py` | Add `canRunSessions` to capability checks; expose android nodes in combined node list |
| `server/routes.py` | Add `/api/android/*` endpoints; modify session creation to check `canRunSessions` |
| `web/src/components/AndroidDevices.tsx` | **New.** Android device management UI |
| `web/src/api.ts` | Add Android device API types and functions |

---

## 2. Host-Node Session Proxying

### Problem

When Ring0 creates a Claude session targeting an Android node (which can't run Claude), the session should run on the host but be visually associated with that node.

### Design: `associatedNodeId` Field

Add an `associatedNodeId` field to sessions. This is distinct from the qualified session ID (`node_id:raw_id`) which means "this session runs on that node."

```
Session runs on    | Session ID format     | associatedNodeId
host (local)       | raw UUID              | null
host, for android  | raw UUID              | "android-node-id"
remote node        | "node_id:raw_uuid"    | null
```

### Flow

1. Ring0 calls `create_session(name="...", node_id="android-node-123")`
2. Hub looks up node → `canRunSessions: false`
3. Hub creates session locally (on host) with `associatedNodeId = "android-node-123"`
4. Session ID is a plain UUID (no node prefix — it runs locally on the hub)
5. The session functions identically to a local session (CLI subprocess on hub)

### Frontend Visibility

- When viewing an Android node, show sessions where `associatedNodeId` matches
- These sessions appear in the sidebar alongside the phone screen view
- The session list endpoint (`GET /api/sessions`) includes `associatedNodeId` in the response
- Frontend filters sessions by `associatedNodeId` when a node is selected

### Explicit Targeting Only

Only sessions where Ring0 (or the user) explicitly targets the Android node get associated. Sessions created while an Android node happens to be selected in the UI do NOT auto-associate unless the user chooses to target that node.

### Files to Modify

| File | Change |
|---|---|
| `server/ws_bridge.py` | Add `associated_node_id: str` to `Session` dataclass |
| `server/session_store.py` | Persist `associatedNodeId` field |
| `server/routes.py` | Session creation: if `canRunSessions: false`, create locally with `associatedNodeId`; include field in session list responses |
| `server/ring0_mcp.py` | Add `node_id` param to `create_session` tool; pass to API |
| `web/src/store.ts` | Add `associatedNodeId` to session state; filter logic |
| `web/src/components/Sidebar.tsx` | Show associated sessions under android nodes |

---

## 3. Phone Screen Viewing (scrcpy Streaming)

### Design: Custom scrcpy Client

Roll our own minimal scrcpy client rather than using py-scrcpy-client (which targets server v1.20, 3 years behind). Use `adbutils` for ADB management and PyAV for H.264 decoding.

### scrcpy Architecture (Reference)

scrcpy pushes a server JAR to the device, starts it via `app_process`, and connects over ADB local abstract sockets. The server captures the screen via Android's `MediaProjection` API, encodes to H.264 via `MediaCodec`, and streams the encoded frames over TCP.

**Protocol (v2.x+):**
1. Push `scrcpy-server-v{VER}` to `/data/local/tmp/scrcpy-server.jar`
2. Set up ADB tunnel: `adb forward tcp:{port} localabstract:scrcpy_{scid}`
3. Start server: `adb shell CLASSPATH=... app_process / com.genymobile.scrcpy.Server {VER} key=value...`
4. Connect TCP socket(s): video, control (optional), audio (optional)

**Video socket wire format:**
- 1 byte: dummy (connection check)
- 64 bytes: device name
- 12 bytes: codec metadata (`codec_id:u32`, `width:u32`, `height:u32`)
- Per frame: 12-byte header (`pts_flags:u64`, `size:u32`) + H.264 NAL units (Annex B)

**Control socket wire format:**
- Binary big-endian messages, 1-byte type prefix
- Touch: type(1) + action(1) + touch_id(8) + x(4) + y(4) + w(2) + h(2) + pressure(2) + action_button(4) + buttons(4)
- Text: type(1) + length(4) + UTF-8 bytes
- Keycode: type(1) + action(1) + keycode(4) + repeat(4) + metaState(4)
- Scroll: type(1) + x(4) + y(4) + w(2) + h(2) + h_scroll(4) + v_scroll(4) + buttons(4)

### New File: `server/scrcpy_client.py`

```python
class ScrcpyClient:
    """Minimal scrcpy client for video streaming and input injection.

    Manages the scrcpy-server lifecycle on the Android device, connects
    video and control sockets, decodes H.264 frames via PyAV.
    """

    def __init__(self, device_id: str, max_size: int = 1080, max_fps: int = 30):
        self._device_id = device_id
        self._max_size = max_size
        self._max_fps = max_fps
        self._video_sock: socket.socket | None = None
        self._control_sock: socket.socket | None = None
        self._codec: av.CodecContext | None = None
        self._screen_width: int = 0
        self._screen_height: int = 0
        self._latest_frame: av.VideoFrame | None = None
        self._running: bool = False

    async def start(self) -> dict:
        """Push server, start it, connect sockets, begin frame decode loop.
        Returns device info (name, width, height).
        """

    async def stop(self) -> None:
        """Disconnect sockets, kill server process, clean up ADB tunnel."""

    async def get_frame(self) -> av.VideoFrame | None:
        """Return the most recent decoded video frame."""

    async def inject_touch(self, action: int, x: int, y: int, width: int, height: int) -> None:
        """Send touch event via control socket."""

    async def inject_key(self, action: int, keycode: int) -> None:
        """Send keycode event via control socket."""

    async def inject_text(self, text: str) -> None:
        """Send text input via control socket (handles Unicode)."""

    async def inject_scroll(self, x: int, y: int, h: int, v: int, width: int, height: int) -> None:
        """Send scroll event via control socket."""

    async def _decode_loop(self) -> None:
        """Background task: read H.264 packets from video socket, decode via PyAV,
        cache latest frame. Handles rotation (new SPS/PPS → resolution change).
        """

    async def _push_and_start_server(self) -> None:
        """Push scrcpy-server JAR, set up ADB forward, start server process."""
```

**Key design points:**
- `_decode_loop` runs as an asyncio task, continuously decoding H.264 → av.VideoFrame
- `get_frame()` returns the cached latest frame (non-blocking, like DesktopTarget)
- Control socket writes are synchronous (binary protocol, fast) but wrapped in executor
- Server version: bundle scrcpy-server v2.7 (stable, well-documented protocol)
- Handles rotation: detects new SPS/PPS config packets, updates resolution

### Streaming to Browser: WebRTC

Reuse the existing desktop streaming infrastructure. The scrcpy client provides `av.VideoFrame` objects — same format as `ScreenCapture`. Create a `ScrcpyVideoTrack(MediaStreamTrack)` that pulls frames from `ScrcpyClient.get_frame()` and delivers them to the WebRTC peer connection.

```
ScrcpyClient (H.264 decode → av.VideoFrame)
  → ScrcpyVideoTrack (MediaStreamTrack)
    → RTCPeerConnection
      → Browser (existing DesktopView.tsx component)
```

The browser's `DesktopView.tsx` already renders a WebRTC video stream. The only UI difference: portrait aspect ratio (9:16) instead of landscape (16:9). The component should handle both.

### WebRTC Signaling for Android Nodes

Desktop nodes handle WebRTC signaling via the node tunnel (`webrtc_offer` command → node's `DesktopWebRTC`). Android virtual nodes don't have a tunnel, so the hub handles signaling directly:

1. Browser requests WebRTC offer for an Android node
2. Hub creates `RTCPeerConnection` with `ScrcpyVideoTrack` as the video source
3. Hub returns SDP answer
4. Video streams from hub to browser (hub is the WebRTC peer, not the phone)

This is similar to how `DesktopTarget` works (hub-side WebRTC peer), except the video source is scrcpy instead of a remote node's screen capture.

### Frontend: Phone View

The existing `DesktopView.tsx` should work with minimal changes — it already handles WebRTC video rendering. Modifications needed:

- **Aspect ratio**: Detect portrait vs landscape from the video track dimensions and adjust the container accordingly
- **Node type awareness**: When the selected node is `nodeType: "android"`, the tab could be labeled "Phone" instead of "Desktop" (or just keep "Screen")
- **No input channel**: Since manual touch is not supported, don't create the input data channel. The view is display-only.

### Files to Create / Modify

| File | Change |
|---|---|
| `server/scrcpy_client.py` | **New.** ScrcpyClient: server lifecycle, H.264 decode, control socket |
| `server/scrcpy_track.py` | **New.** ScrcpyVideoTrack(MediaStreamTrack): feeds decoded frames to WebRTC |
| `server/routes.py` | Add WebRTC signaling endpoint for Android nodes |
| `web/src/components/DesktopView.tsx` | Handle portrait aspect ratio; label changes for Android nodes |

### scrcpy-server Binary

Bundle `scrcpy-server-v2.7` in the repo (it's ~60KB). Push to device on first connection. The server is version-locked to our client implementation.

Location: `server/vendor/scrcpy-server-v2.7`

---

## 4. Computer-Use via scrcpy

### Design: `AdbTarget` Using scrcpy

The `AdbTarget` class wraps `ScrcpyClient` to implement the same `get_frame()` + `inject()` interface as `DesktopTarget`. The `UITarsAgent` is already target-agnostic — it just needs a target with those two methods.

```
                         ┌─ DesktopTarget (WebRTC) ─── Remote Node
UITarsAgent ── target ──┤
                         └─ AdbTarget (scrcpy) ─────── Android Phone
```

### Why scrcpy Instead of Raw ADB Commands

| | `adb screencap` + `adb input` | scrcpy |
|---|---|---|
| Screenshot latency | 300–1500ms | <50ms (continuous stream) |
| Input latency | 100–300ms per command | <70ms (binary socket) |
| Unicode text input | Unreliable | Native support via `inject_text` |
| Connection overhead | New subprocess per command | Persistent socket |
| Scroll support | Emulated via swipe | Native scroll events |

### New File: `server/adb_target.py`

```python
class AdbTarget:
    """scrcpy-based target for Android device control.

    Drop-in replacement for DesktopTarget — same get_frame()/inject() interface.
    Uses ScrcpyClient for both screen capture and input injection.
    """

    def __init__(self, device_id: str):
        self._scrcpy = ScrcpyClient(device_id, max_size=1080, max_fps=5)
        # Lower FPS for the agent (it only needs ~1 frame per iteration)
        # The live view uses a separate ScrcpyClient at higher FPS

    async def start(self) -> None:
        """Start scrcpy client, verify device connection."""
        await self._scrcpy.start()

    async def stop(self) -> None:
        """Stop scrcpy client."""
        await self._scrcpy.stop()

    async def get_frame(self) -> av.VideoFrame | None:
        """Return latest decoded frame from scrcpy."""
        return await self._scrcpy.get_frame()

    async def inject(self, event: dict) -> None:
        """Translate JSON input events to scrcpy control commands.

        Events arrive in the same format as desktop WebRTC data channel:
        mousemove, mousedown, mouseup, text, keydown, keyup, wheel.

        Coalesces mouse events into touch gestures:
        - mousemove + mousedown + mouseup at same position → tap
        - mousedown + mousemove + mouseup at different position → swipe
        - wheel → scroll event
        - mousemove without mousedown → no-op (no cursor on Android)
        """
```

### Event Coalescing

The existing `execute_action()` in `ui_tars_actions.py` sends sequences of low-level mouse events (designed for desktop WebRTC). ADB/scrcpy needs these coalesced into touch gestures:

| Desktop event sequence | scrcpy action |
|---|---|
| `mousemove(x,y)` → `mousedown(0)` → `mouseup(0)` | `inject_touch(ACTION_DOWN, x, y)` → `inject_touch(ACTION_UP, x, y)` |
| `mousedown(x1,y1)` → `mousemove(x2,y2)` → `mouseup(x2,y2)` | `inject_touch(DOWN, x1, y1)` → `inject_touch(MOVE, x2, y2)` → `inject_touch(UP, x2, y2)` |
| `text("hello")` | `inject_text("hello")` |
| `keydown("Enter")` → `keyup("Enter")` | `inject_key(ACTION_DOWN, KEYCODE_ENTER)` → `inject_key(ACTION_UP, KEYCODE_ENTER)` |
| `wheel(dy=300)` at `(x,y)` | `inject_scroll(x, y, 0, -3)` |
| `mousemove` without `mousedown` | No-op (Android has no hover cursor) |

Implementation: buffer `mousedown` event. On `mouseup`, compare position with buffered `mousedown` — if same → tap, if different → swipe. On `mousemove` between down/up, send `ACTION_MOVE` for real-time swipe tracking.

### Coordinate Translation

```
VLM output: 0–1000 normalized grid
  → execute_action() converts to 0.0–1.0 fractions
    → AdbTarget.inject() receives fractions, multiplies by screen dimensions
      → scrcpy inject_touch() gets absolute pixel coordinates
```

Same pipeline as desktop, just the final step uses scrcpy binary protocol instead of WebRTC data channel JSON.

### Key Mapping

```python
_KEYCODE_MAP = {
    "Enter": 66,        # KEYCODE_ENTER
    "Backspace": 67,    # KEYCODE_DEL (Android DEL = backspace)
    "Delete": 112,      # KEYCODE_FORWARD_DEL
    "Tab": 61,          # KEYCODE_TAB
    "Escape": 111,      # KEYCODE_ESCAPE
    "Home": 3,          # KEYCODE_HOME
    "Back": 4,          # KEYCODE_BACK
    "ArrowUp": 19,      # KEYCODE_DPAD_UP
    "ArrowDown": 20,    # KEYCODE_DPAD_DOWN
    "ArrowLeft": 21,    # KEYCODE_DPAD_LEFT
    "ArrowRight": 22,   # KEYCODE_DPAD_RIGHT
    "Space": 62,        # KEYCODE_SPACE
    "AppSwitch": 187,   # KEYCODE_APP_SWITCH
    "Power": 26,        # KEYCODE_POWER
    "VolumeUp": 24,     # KEYCODE_VOLUME_UP
    "VolumeDown": 25,   # KEYCODE_VOLUME_DOWN
}
```

### Session Creation Integration

When creating a computer-use session for an Android node:

1. `POST /api/sessions/create { backend: "computer-use", nodeId: "android-node-id" }`
2. Hub looks up node → `nodeType: "android"`, gets `device_id`
3. In `main.py:on_computer_use_created()`:
   ```python
   if node.nodeType == "android":
       target = AdbTarget(device_id=node.device_id)
   else:
       target = DesktopTarget(signaling_fn=..., ...)
   ```
4. Creates `UITarsAgent(session_id, target, vlm)` — agent is target-agnostic
5. Session has `associatedNodeId = "android-node-id"`

### VLM Prompt Tuning

Add a device-type hint to the VLM prompt when targeting Android:

```
You are controlling an Android phone. The screen shows a mobile interface.
Prefer tap and swipe gestures. Use the Back key to navigate back.
Do not use desktop keyboard shortcuts.
```

This biases UI-TARS toward mobile-appropriate actions (tap/swipe vs keyboard shortcuts).

### Shared scrcpy Connection

The phone view (section 3) and the computer-use agent (section 4) both need scrcpy access to the same device. Two options:

- **Option A: Shared ScrcpyClient** — Single scrcpy session, both the video track and AdbTarget read from the same decoded frame stream. Input goes through the same control socket.
- **Option B: Separate ScrcpyClient instances** — The live view runs at 30fps for smooth display, the agent runs at 5fps for efficient VLM inference. Two scrcpy-server instances on the device (different SCIDs).

**Decision: Option A (shared).** One scrcpy connection per device. The live view and agent both read `get_frame()` from the same client. The decode loop runs at 30fps (for smooth live view), and the agent simply reads the latest cached frame whenever it needs one. Input commands from the agent go through the shared control socket. This avoids double-encoding on the phone and simplifies lifecycle management.

The `AndroidRegistry` owns the `ScrcpyClient` instance per device. Both the video track and `AdbTarget` reference the same client.

### Files to Create / Modify

| File | Change |
|---|---|
| `server/adb_target.py` | **New.** `AdbTarget`: wraps ScrcpyClient, implements get_frame()/inject() with event coalescing |
| `server/ui_tars_agent.py` | Relax `desktop_target` type hint to accept any target with `get_frame()`/`inject()` |
| `server/main.py` | Branch in `on_computer_use_created()`: AdbTarget vs DesktopTarget based on node type |
| `server/cli_launcher.py` | Pass through `nodeId` → `associatedNodeId` for Android nodes |
| `server/routes.py` | Session creation: detect Android node, route accordingly |

---

## Implementation Order

```
Phase 1: Foundation
├── 1a. adb_utils.py — ADB command helpers
├── 1b. android_registry.py — Registration, persistence, status tracking
├── 1c. API endpoints — /api/android/* CRUD + discovery
└── 1d. Frontend — Android device management UI in Settings

Phase 2: scrcpy Client
├── 2a. scrcpy_client.py — Server lifecycle, H.264 decode, control socket
├── 2b. Bundle scrcpy-server binary
└── 2c. Manual testing — verify stream + input on real device

Phase 3: Phone Screen View
├── 3a. scrcpy_track.py — MediaStreamTrack from ScrcpyClient frames
├── 3b. WebRTC signaling for Android nodes (hub-side peer)
├── 3c. DesktopView.tsx — Portrait aspect ratio handling
└── 3d. Node type routing — Android nodes use hub-side WebRTC

Phase 4: Session Proxying
├── 4a. associatedNodeId on Session + SessionStore
├── 4b. Session creation routing (canRunSessions check)
├── 4c. ring0_mcp.py — node_id param on create_session
└── 4d. Frontend — filter/show associated sessions per node

Phase 5: Computer-Use Agent
├── 5a. adb_target.py — AdbTarget with event coalescing
├── 5b. main.py — AdbTarget vs DesktopTarget branching
├── 5c. ui_tars_agent.py — Generalize target type hint
├── 5d. VLM prompt tuning for mobile
└── 5e. End-to-end testing
```

Phases 1–2 are sequential. Phases 3–4 can run in parallel after Phase 2. Phase 5 depends on 2 + 4.

---

## Dependencies

### New Python Packages

| Package | Purpose | Notes |
|---|---|---|
| `adbutils` | ADB device management, push, shell, forwarding | Pure Python, well-maintained |
| `zeroconf` | mDNS/Bonjour discovery for wireless ADB devices | For `_adb-tls-connect._tcp` service discovery |

### Already Available

| Package | Purpose |
|---|---|
| `av` (PyAV) | H.264 decoding (already used for desktop screen capture) |
| `aiortc` | WebRTC peer connections (already used for desktop streaming) |
| `asyncio` | Async subprocess for ADB commands |

### System Requirements

- `adb` binary on the host machine (Android SDK Platform Tools)
- USB debugging enabled on Android device (for USB mode)
- Wireless debugging enabled on Android device (for IP/mDNS mode, Android 11+)

---

## File Summary

### New Files

| File | Purpose |
|---|---|
| `server/adb_utils.py` | ADB command helpers: list_devices, connect, disconnect, shell, device_info |
| `server/android_registry.py` | Android node registration, persistence, connection management, status |
| `server/mdns_discovery.py` | mDNS/Bonjour discovery for ADB devices |
| `server/scrcpy_client.py` | scrcpy server lifecycle, H.264 decode loop, control socket protocol |
| `server/scrcpy_track.py` | MediaStreamTrack wrapper for ScrcpyClient frames |
| `server/adb_target.py` | AdbTarget: ComputerUseAgent-compatible target via scrcpy |
| `server/vendor/scrcpy-server-v2.7` | Bundled scrcpy server binary (~60KB) |
| `web/src/components/AndroidDevices.tsx` | Android device management UI |

### Modified Files

| File | Change |
|---|---|
| `server/node_registry.py` | Expose android nodes in combined node list; `canRunSessions` checks |
| `server/routes.py` | Android API endpoints; session creation routing; WebRTC signaling for Android |
| `server/main.py` | `on_computer_use_created()` branching; scrcpy lifecycle on startup/shutdown |
| `server/ws_bridge.py` | `associated_node_id` on Session |
| `server/session_store.py` | Persist `associatedNodeId` |
| `server/cli_launcher.py` | Pass through `associatedNodeId` |
| `server/ui_tars_agent.py` | Generalize target type hint (DesktopTarget → Protocol) |
| `server/ring0_mcp.py` | `node_id` param on `create_session` |
| `web/src/api.ts` | Android device types and API functions |
| `web/src/store.ts` | Android node state; `associatedNodeId` filtering |
| `web/src/components/DesktopView.tsx` | Portrait aspect ratio; node type label |
| `web/src/components/Sidebar.tsx` | Show associated sessions under android nodes |
| `web/src/components/SettingsPage.tsx` | Android devices section |

### Unchanged

| File | Why |
|---|---|
| `server/computer_use_agent.py` | Protocol is target-agnostic |
| `server/desktop_target.py` | Desktop-only path, not touched |
| `server/ui_tars_actions.py` | Action parser/executor is target-agnostic |
| `server/vlm.py` | Model loading unchanged |
| `server/screen_capture.py` | Desktop-only |
| `server/input_injector.py` | Desktop-only |
| `server/webrtc.py` | Voice/audio path, not involved |
| `vibr8_node/` | Android nodes don't use node_agent |

---

## Design Decisions (Resolved)

### 1. scrcpy-server Version: v2.7

Use v2.7 — it has a stable, well-documented wire protocol, broad Android compatibility (11–15), and avoids the protocol churn in v3.x (UHID v2, new message types). The v2.x protocol is simpler to implement: 3-socket model (video/audio/control), 12-byte frame headers, straightforward binary control messages. If we hit device-specific issues on newer Android versions, we can bump the server JAR without changing our client code (the v2.x wire format is stable across minor versions).

### 2. Screen Rotation: Adapt On-the-Fly

Handle rotation changes mid-session. When the phone rotates:
1. scrcpy-server recreates the encoder with new dimensions
2. A new SPS/PPS config packet arrives on the video socket
3. `ScrcpyClient._decode_loop()` parses the new SPS to extract updated width/height
4. `_screen_width` / `_screen_height` are updated atomically
5. Next `get_frame()` returns a frame at the new resolution
6. `AdbTarget.inject()` uses the updated dimensions for coordinate translation
7. The WebRTC video track naturally adjusts (aiortc handles resolution changes)

Do NOT lock orientation — let the agent and user see the real screen state.

### 3. Wireless ADB Reconnect: 3 Retries + UI Fallback

When the ADB/scrcpy connection drops:
1. Attempt reconnect 3 times with 2s/4s/8s backoff
2. Each attempt: `adb connect` → restart scrcpy-server → reconnect sockets
3. If all 3 fail: mark node as offline, show a "Reconnect / Reconfigure" button below the node in the sidebar
4. "Reconfigure" opens the connection settings (IP/port may have changed)
5. If a computer-use agent was running, pause it (don't terminate) — it resumes automatically on reconnect

### 4. Multi-Device Concurrency: Not a Concern

One ScrcpyClient per device is sufficient. No special pooling, throttling, or resource limits needed. The per-device architecture handles multiple phones naturally.

### 5. `launch_app`: Keep Separate

The existing `launch_app` MCP tool (native WebSocket to vibr8 Android app) and ADB-based app launching are separate paths. Do not unify them. ADB app launching (`adb shell am start`) is part of the computer-use agent's action space, not a Ring0 MCP tool.
