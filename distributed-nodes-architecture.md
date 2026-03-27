# Distributed Nodes Architecture

> Design document for extending vibr8 from a single-host application to a distributed multi-node system where Claude Code agents run across multiple machines.

---

## 1. Current Architecture

### 1.1 System Overview

vibr8 is a web UI for launching and interacting with Claude Code agents. All components run on a single host:

```
                         Internet
                            |
                       [nginx / EC2]
                            |
                       [SSH tunnel]
                            |
                    [Vite dev server :5174]
                      /            \
              [Static assets]    [Proxy]
                                    |
                          [vibr8 server :3456]
                         /     |      \       \
                   WsBridge  WebRTC  Routes  Ring0Manager
                     |         |       |         |
                  Sessions   STT/TTS  REST    MCP subprocess
                     |
              [Claude Code CLI subprocesses]
```

### 1.2 Core Components

**vibr8 server** (`server/main.py`, port 3456) — Python/aiohttp application that:
- Serves the React frontend (production) or proxies via Vite (dev)
- Manages WebSocket connections for both CLI subprocesses and browser clients
- Runs WebRTC peer connections for bidirectional voice
- Exposes 40+ REST endpoints under `/api/`
- Hosts the Ring0 meta-agent lifecycle

**WsBridge** (`server/ws_bridge.py`) — Central message router between Claude Code CLIs and browser clients:
- Maintains a `Session` object per agent session (state, message history, permissions, pending messages)
- Two WebSocket endpoints: `/ws/cli/{session_id}` (CLI connects here) and `/ws/browser/{session_id}` (browsers connect here)
- NDJSON protocol (newline-delimited JSON) on both sides
- Supports multiple browser clients per session (primary + second screens)
- Handles message deduplication, replay detection, history archiving
- "Take the Pen" ownership model: sessions are controlled by either Ring0 or the user

**CLI Launcher** (`server/cli_launcher.py`) — Spawns and manages Claude Code CLI subprocesses:
- Each session gets a subprocess: `claude --sdk-url ws://localhost:3456/ws/cli/{session_id} --print --output-format stream-json ...`
- Tracks `SdkSessionInfo` per session (PID, state, model, cwd, creation time)
- Supports `--resume` for conversation continuity across relaunches
- Persists state to `~/.vibr8/sessions/launcher.json` for crash recovery
- Auto-relaunches CLI when a browser reconnects to a dead session

**Ring0** (`server/ring0.py`, `server/ring0_mcp.py`) — Meta-agent that orchestrates other sessions:
- Fixed session ID: `"ring0"`, always pinned at top of session list
- Runs as a Claude Code CLI subprocess with `--permission-mode bypassPermissions`
- Connected to an MCP server (`ring0_mcp.py`) via stdio, which exposes 25+ tools
- MCP tools call back to vibr8's REST API over localhost (`http://localhost:3456/api/ring0/*`)
- Ring0 APIs are auth-exempt (localhost-only, not exposed externally)
- Voice input routes to Ring0 by default; Ring0 dispatches to target sessions via `send_message`

**WebRTC Pipeline** (`server/webrtc.py`, `server/stt.py`, `server/tts.py`, `server/audio_track.py`):
- Browser establishes WebRTC peer connection via REST-based SDP signaling (`POST /api/webrtc/offer`)
- Server-side: `aiortc` for peer connections, Silero VAD for voice activity detection, Whisper large-v3 for STT
- Outgoing audio: OpenAI TTS API -> Opus frames -> `QueuedAudioTrack` -> WebRTC
- Guard word system: "vibr8" prefix required (when guard mode on) before processing voice commands
- Voice commands: `done`, `off`, `guard`, `listen`, `quiet`, `speak`, `ring zero on/off`, `note`, `node <name>`

**Session Persistence** (`server/session_store.py`):
- Sessions saved to `~/.vibr8/sessions/{session_id}.json` with debounced atomic writes
- Message archives in `~/.vibr8/archives/{session_id}/{YYYY-MM-DD}.jsonl`
- Launcher state in `~/.vibr8/sessions/launcher.json`

### 1.3 Frontend Architecture

React 19 + TypeScript + Vite + Zustand state management:

- **Store** (`web/src/store.ts`): Sessions map, SDK session list, connection status per session, audio state (global singleton), client identity (UUID persisted in localStorage)
- **WebSocket client** (`web/src/ws.ts`): Per-session WebSocket connections, NDJSON message handling, RPC request/response for Ring0 queries, reconnection with 60s timeout
- **WebRTC client** (`web/src/webrtc.ts`): Browser-side peer connection, mic capture, remote audio playback, device enumeration and switching
- **Sidebar** (`web/src/components/Sidebar.tsx`): Session list with Ring0 pinned first, MRU sorting, 5-second polling of `/api/sessions`

### 1.4 Multi-Client Support

- **Primary clients**: Normal browser tabs, identified by `clientId` (UUID)
- **Second screens**: Paired display clients (`role=secondscreen`), can mirror sessions or show pushed content
- **RPC system**: Ring0 can query any connected client (get state, location, clipboard, screenshots, audio devices, launch apps)
- **Client metadata**: Persisted to `~/.vibr8/clients.json` with names, descriptions, device info

### 1.5 Authentication & Security

- HMAC-signed stateless tokens (survive server restarts), managed by `server/auth.py`
- Users configured in `~/.vibr8/users.json`
- Auth-exempt paths: `/api/ring0/*` (MCP subprocess, localhost-only), `/ws/cli/*` (CLI subprocess, localhost-only), `/api/second-screen/*`, `/api/clients`
- External access: nginx on EC2 reverse-proxies to SSH tunnel to Vite dev server

### 1.6 Key Data Flows

**User types in browser:**
```
Browser → WS /ws/browser/{sid} → WsBridge._route_browser_message
  → _handle_user_message → NDJSON to CLI via /ws/cli/{sid}
  → CLI processes → response NDJSON back → WsBridge._route_cli_message
  → _broadcast_to_browsers → all connected browser WS clients
```

**Voice input:**
```
Browser mic → WebRTC → aiortc incoming track → _consume_audio
  → 160ms batches → stt.process_buffer → Whisper STT
  → final_transcript → guard word check → command dispatch or submit
  → ws_bridge.submit_user_message(ring0_session_id, text)
  → Ring0 CLI processes → may call send_message MCP tool
  → _post("/ring0/send-message") → ws_bridge._handle_user_message on target session
```

**Ring0 MCP tool call:**
```
Ring0 CLI → stdio MCP → ring0_mcp.py tool function
  → httpx POST to http://localhost:3456/api/ring0/*
  → routes.py handler → ws_bridge/launcher/webrtc_manager method
  → response back through chain
```

---

## 2. Vision & Phases Overview

### The Problem

Today, vibr8 is a single-host application. All Claude Code sessions, Ring0, voice processing, and the UI server run on one machine. This limits:

- **Compute distribution**: Can't run agents on a powerful cloud GPU box while using the UI from a laptop
- **Environment isolation**: All sessions share one OS, filesystem, and network
- **Remote development**: Can't have agents work on remote servers where the code actually lives
- **Resilience**: Single point of failure

### The Goal

A distributed architecture where Claude Code agents run on multiple "nodes" (any host), while vibr8 provides a unified UI with voice control across all of them.

### Phase Summary

| Phase | What | Key Capability |
|-------|------|----------------|
| **Phase 1** | Multi-node discovery & connection | Switch between Ring0 instances on different hosts; voice works everywhere |
| **Phase 2** | Headless nodes + remote desktop | Screen sharing from remote nodes; bi-directional input events |
| **Phase 3** | Computer use agent | AI acting on remote desktops via voice + vision |

---

## 3. Phase 1: Multi-Node Discovery & Connection

### 3.1 Concept

Each node is a host running Ring0 + Claude Code sessions. The vibr8 UI server (vibr8.ringzero.ai) is the central hub. Nodes register with the hub and expose their sessions. The user sees a node switcher in the UI and can switch between nodes. Voice is always processed centrally and transcripts are forwarded to the active node's Ring0.

```
                        ┌─────────────────────┐
                        │   vibr8 UI server    │
                        │  (vibr8.ringzero.ai) │
                        │                      │
                        │  - Web frontend      │
                        │  - WebRTC/STT/TTS    │
                        │  - Node registry     │
                        │  - Auth              │
                        └──────┬───────┬───────┘
                               │       │
                    ┌──────────┘       └──────────┐
                    │                              │
             ┌──────┴──────┐              ┌───────┴──────┐
             │   Node A    │              │   Node B     │
             │  (local)    │              │  (cloud VM)  │
             │             │              │              │
             │  Ring0      │              │  Ring0       │
             │  Session 1  │              │  Session 3   │
             │  Session 2  │              │  Session 4   │
             └─────────────┘              └──────────────┘
```

### 3.2 Node Agent (`vibr8-node`)

A lightweight Python process that runs on each remote host. Responsibilities:

- **Registration**: Connect to the central vibr8 server and register (API key auth)
- **Heartbeat**: Periodic liveness signal (every 30s)
- **Session proxy**: Expose local Ring0 + sessions to the central server
- **CLI management**: Launch/manage Claude Code CLI subprocesses locally (reuses `cli_launcher.py`)
- **Ring0 hosting**: Run Ring0 MCP server locally (reuses `ring0.py` / `ring0_mcp.py`)

**What vibr8-node is NOT:**
- Not a full vibr8 server (no web frontend, no WebRTC, no STT/TTS)
- Not user-facing (no browser connects to it directly)

**Package structure:**
```
vibr8-node/
  __main__.py          # Entry point
  node_server.py       # aiohttp server exposing node API
  cli_launcher.py      # Reused from vibr8 server (or imported)
  ring0.py             # Reused Ring0 manager
  ring0_mcp.py         # Reused MCP server
  session_store.py     # Reused persistence
```

### 3.3 Node Registration Protocol

**Registration flow:**
```
1. Node starts with config: { hub_url: "https://vibr8.ringzero.ai", api_key: "..." }
2. Node POST /api/nodes/register
   Body: { name: "cloud-dev", apiKey: "...", capabilities: {...}, nodeUrl: "..." }
3. Hub validates API key, stores node, returns { nodeId: "uuid", ok: true }
4. Node begins heartbeat: POST /api/nodes/{nodeId}/heartbeat every 30s
5. Hub marks node offline after 3 missed heartbeats (90s)
```

**Node capabilities advertised on registration:**
```json
{
  "name": "cloud-dev",
  "hostname": "ip-172-31-42-1",
  "platform": "linux",
  "arch": "x86_64",
  "ring0Enabled": true,
  "sessionCount": 3,
  "version": "0.1.0"
}
```

### 3.4 Central Server Changes

**New components on the hub:**

**Node Registry** (`server/node_registry.py`):
```python
@dataclass
class RegisteredNode:
    id: str                    # UUID assigned by hub
    name: str                  # User-friendly name
    api_key_hash: str          # bcrypt hash of API key
    node_url: str              # How hub reaches the node (may be empty for tunnel-based)
    capabilities: dict         # Platform, arch, etc.
    status: str                # "online" | "offline"
    last_heartbeat: float      # timestamp
    session_ids: list[str]     # Known sessions on this node
    ws: WebSocketResponse | None  # Persistent WS connection to node
```

**Connection model — WebSocket tunnel (preferred over HTTP polling):**

Rather than the hub calling back to the node (which requires the node to be publicly reachable), the node maintains an **outbound WebSocket** to the hub:

```
Node → WSS wss://vibr8.ringzero.ai/ws/node/{nodeId}?apiKey=...
```

This WebSocket serves as a bidirectional command channel:
- **Node → Hub**: Heartbeat, session list updates, session state changes
- **Hub → Node**: Create session, send message, list sessions, get session output, etc.

This is critical because most nodes will be behind NAT/firewalls. The node initiates the connection outward; the hub sends commands back over the same socket.

**New REST endpoints on the hub:**
```
POST   /api/nodes/register          # Node registers with hub
GET    /api/nodes                   # List all nodes (for UI)
DELETE /api/nodes/{nodeId}          # Remove a node
WS     /ws/node/{nodeId}            # Persistent node connection
```

**New REST endpoints for the UI (browser-facing, auth-required):**
```
GET    /api/nodes                   # List nodes with status
POST   /api/nodes/{nodeId}/activate # Switch active node
GET    /api/nodes/active            # Get currently active node
```

### 3.5 Message Routing

When the user is connected to a remote node, the hub acts as a proxy:

**Browser sends a message to a remote session:**
```
Browser → WS /ws/browser/{session_id} → Hub WsBridge
  → Hub detects session belongs to Node B
  → Hub forwards via Node B's WebSocket tunnel
  → Node B's WsBridge delivers to local CLI
  → CLI response → Node B → Hub WS tunnel → Hub WsBridge
  → Hub broadcasts to browser clients
```

**Voice transcript routed to remote Ring0:**
```
Browser mic → WebRTC → Hub STT (Whisper)
  → transcript text → Hub checks active node
  → Hub forwards to Node B via WS tunnel: { type: "submit_message", sessionId: "ring0", content: "..." }
  → Node B's Ring0 CLI processes
  → Response flows back via WS tunnel → Hub → Browser
```

### 3.6 Hub WsBridge Changes

The current `WsBridge` assumes sessions are local. Key changes:

**Session routing table:**
```python
# New field on WsBridge
self._session_node_map: dict[str, str] = {}  # session_id → node_id ("local" for hub sessions)
```

**Modified `_route_browser_message`:**
```python
async def _route_browser_message(self, session, msg, ws=None):
    node_id = self._session_node_map.get(session.id, "local")
    if node_id == "local":
        # Existing local handling
        ...
    else:
        # Forward to remote node via WS tunnel
        node = self._node_registry.get_node(node_id)
        await node.send_command({
            "type": "browser_message",
            "sessionId": session.id,
            "message": msg,
            "sourceClientId": session.browser_sockets.get(ws, ""),
        })
```

**Remote session state:**
For remote sessions, the hub maintains a lightweight `Session` proxy object — enough for browser clients to connect and receive broadcasts, but actual message processing happens on the node.

### 3.7 Frontend Changes

**Node switcher in Sidebar:**
```
┌─────────────────────────┐
│ ▼ cloud-dev (online)    │  ← Node dropdown
├─────────────────────────┤
│ ★ Ring0                 │  ← This node's Ring0
│   Session A             │
│   Session B             │
├─────────────────────────┤
│   + New Session         │
└─────────────────────────┘
```

**Store changes** (`web/src/store.ts`):
```typescript
// New state
nodes: NodeInfo[];                    // All registered nodes
activeNodeId: string | null;          // Currently selected node ("local" or UUID)
setActiveNode: (nodeId: string) => void;
setNodes: (nodes: NodeInfo[]) => void;

interface NodeInfo {
  id: string;
  name: string;
  status: "online" | "offline";
  platform: string;
  sessionCount: number;
  ring0Enabled: boolean;
}
```

**Session polling changes** (`Sidebar.tsx`):
- Poll includes `?nodeId={activeNodeId}` parameter
- Hub returns sessions for the active node only
- On node switch: disconnect all current session WebSockets, clear session state, reconnect to new node's sessions

**Voice routing:**
- No WebRTC changes — audio always goes to the hub
- Hub's `_submit_text` in `webrtc.py` checks active node:
  - If local: submit directly to Ring0 session (existing flow)
  - If remote: forward transcript to remote Ring0 via node WS tunnel

**Voice node switching:**
- New voice command: `vibr8 node <name>` — switches the active node
- Processed on the hub (voice is always central), before transcript routing
- Fuzzy match on node display name (case-insensitive, partial match accepted)
- On match: updates active node, re-routes subsequent voice transcripts to the new node's Ring0, confirms via TTS: "Switched to node <name>"
- On no match: TTS response: "No node named <name> found"
- On ambiguous match: TTS response: "Multiple nodes match <name>: ..." (lists candidates)
- Hub notifies the browser via WebSocket so the UI node picker updates in sync
- Fits alongside existing voice commands (`done`, `guard`, `ring zero on/off`, etc.)

### 3.8 The "Local" Node

The hub itself is always a node — the "local" node. It runs its own Ring0 and sessions, just like today. When no remote nodes are registered or active, the UI behaves identically to today. The node switcher simply doesn't appear (or shows "Local" as the only option).

### 3.9 Node WS Tunnel Protocol

Messages on the `ws/node/{nodeId}` WebSocket, NDJSON format:

**Node → Hub:**
```json
{"type": "heartbeat", "sessionCount": 3, "ring0Enabled": true}
{"type": "sessions_update", "sessions": [{...SdkSessionInfo...}]}
{"type": "session_message", "sessionId": "abc", "message": {...}}
{"type": "ring0_state", "enabled": true, "sessionId": "ring0"}
```

**Hub → Node:**
```json
{"type": "create_session", "options": {...LaunchOptions...}}
{"type": "submit_message", "sessionId": "abc", "content": "hello", "sourceClientId": "..."}
{"type": "list_sessions"}
{"type": "get_session_output", "sessionId": "abc"}
{"type": "interrupt", "sessionId": "abc"}
{"type": "set_permission_mode", "sessionId": "abc", "mode": "plan"}
{"type": "respond_permission", "sessionId": "abc", "requestId": "...", "behavior": "allow"}
```

Each command includes a `requestId` for correlating responses.

### 3.10 Phase 1 Design Decisions

1. **Session ID collisions**: No namespacing. Bare UUIDs are used as-is across all nodes. Collision probability is ~2^-122 — not worth the complexity of `{nodeId}:{sessionId}` namespacing. All existing code paths (WsBridge, session store, frontend) continue to work unchanged.

2. **Which Ring0 gets voice**: Active node's Ring0 only. When the user switches nodes (via UI or voice command), voice transcripts route to the new node's Ring0. There is no split-view where voice targets one node while the UI shows another — voice and UI always point at the same active node.

3. **MCP tool scope**: Ring0 MCP tools calling `localhost` work naturally on each node — the node runs its own `ring0_mcp.py → localhost:node_port` pipeline. The `query_client` tool (browser RPC) is a known exception: it needs to reach the browser through the hub. This is solved by proxying `query_client` calls through the node WS tunnel (hub relays to the browser and returns the response).

4. **Latency budget**: The distributed voice pipeline adds only 2-10ms (nearby nodes) or 100-300ms (cross-continent) on top of the existing 2-8s voice round-trip (dominated by STT, LLM inference, and TTS). The added network hops are negligible and acceptable for conversational voice.

5. **Offline recovery**: Minimal — presence tracking only. When a node goes offline, the hub marks it offline and shows a toast notification in the UI. No message queuing, no state sync, no automatic failover to another node. When the node reconnects, it re-registers and sends its current session list; the hub reconciles. If the user was viewing the offline node, they see "Reconnecting..." on active sessions (existing behavior) and can manually switch to another node.

---

## 4. Phase 2: Headless Nodes + Remote Desktop

### 4.1 Concept

Nodes can share their screen via WebRTC video tracks. The remote desktop is streamed to the vibr8 UI or a second screen. The user can send keyboard, mouse, and touch events back to the node for bi-directional remote control.

### 4.2 Screen Capture on Nodes

The `vibr8-node` daemon is extended with screen capture:

**macOS**: `CGDisplayStream` API (via PyObjC) for efficient screen capture
**Linux**: PipeWire / X11 `XShmGetImage` / Wayland screen copy protocol
**Encoding**: Frames encoded to VP8/VP9/H.264 and sent as WebRTC video track

```
Node screen → Platform capture API → Frame encoder → WebRTC video track
                                                         ↓
                                            Hub relays (or direct P2P)
                                                         ↓
                                            Browser <video> element
```

### 4.3 Input Event Channel

Bi-directional: browser sends input events back to the node via a WebRTC data channel or WebSocket messages:

```json
{"type": "mouse_move", "x": 500, "y": 300}
{"type": "mouse_click", "x": 500, "y": 300, "button": "left"}
{"type": "key_down", "key": "a", "modifiers": ["shift"]}
{"type": "key_up", "key": "a"}
{"type": "touch_start", "touches": [{"id": 0, "x": 100, "y": 200}]}
{"type": "scroll", "x": 500, "y": 300, "deltaX": 0, "deltaY": -120}
```

**Node-side input injection:**
- macOS: `CGEventPost` / `CGEventCreateKeyboardEvent`
- Linux: `uinput` virtual device or `xdotool` / `ydotool`

### 4.4 Capability Negotiation

On connection, the browser and node exchange device capabilities:

```json
// Browser → Node
{
  "type": "input_capabilities",
  "touch": true,
  "mouse": true,
  "keyboard": true,
  "screenWidth": 1920,
  "screenHeight": 1080,
  "devicePixelRatio": 2
}

// Node → Browser
{
  "type": "display_capabilities",
  "width": 2560,
  "height": 1440,
  "dpi": 144,
  "platform": "macos"
}
```

### 4.5 WebRTC Topology

Two options for video:

**Option A — Hub relay (simpler, more latency):**
```
Node → WebRTC video → Hub → WebRTC video → Browser
```
Hub acts as an SFU (Selective Forwarding Unit). Reuses existing aiortc infrastructure. Adds latency but works through any NAT/firewall.

**Option B — Direct P2P (lower latency, harder NAT traversal):**
```
Node → WebRTC video → (STUN/TURN) → Browser
```
Node and browser establish a direct peer connection. Requires the node to handle WebRTC signaling. Better latency for interactive use.

**Recommended**: Start with Hub relay (Option A) for simplicity, add direct P2P as an optimization later. The node WS tunnel can carry signaling for either model.

### 4.6 UI Integration

The remote desktop appears as a new view mode alongside Chat and Editor:

```
[Chat] [Editor] [Desktop]
```

Or it can be pushed to a second screen for dual-monitor workflows:
- Primary screen: vibr8 chat/editor
- Second screen: live remote desktop from the node

### 4.7 Phase 2 Open Questions

1. **Frame rate vs quality tradeoff**: Interactive desktop needs 30fps minimum. What resolution and bitrate are acceptable over typical connections?
2. **Audio from remote desktop**: Should the remote node's audio output also be captured and streamed? (System audio, not just voice.)
3. **Clipboard sync**: Should clipboard be automatically synced between browser and remote node, or on-demand?
4. **Multi-monitor**: If the remote node has multiple displays, how does the user choose which one to share?
5. **Security**: Remote desktop access is powerful. Should it require additional authorization beyond the node API key?

---

## 5. Phase 3: Computer Use Agent

### 5.1 Concept

Ring0 (or a dedicated agent) can see and act on the remote desktop. Voice commands are augmented with visual understanding — the agent can describe what's on screen, click buttons, fill forms, and navigate applications.

### 5.2 Approach A: Claude Computer Use API

Claude's built-in computer use capability:
- Agent takes periodic screenshots of the remote desktop
- Screenshots sent as image content in the conversation
- Claude responds with `computer` tool calls: `{"action": "click", "coordinate": [500, 300]}`, `{"action": "type", "text": "hello"}`
- Tool results are translated to input events and injected on the remote node
- Next screenshot captures the result

**Pros**: Battle-tested, Claude understands UI layouts, works with any application
**Cons**: Screenshot-based (not real-time video), latency per action cycle, token-heavy

### 5.3 Approach B: Custom Vision Agent

A custom pipeline that processes the WebRTC video stream:
- Continuous frame analysis (not just screenshots)
- Object detection for UI elements (buttons, text fields, menus)
- OCR for text extraction
- Agent generates input events based on vision model output

**Pros**: Real-time, potentially lower latency, less token usage
**Cons**: Significant ML engineering, may not match Claude's UI understanding

### 5.4 Recommended Approach

Start with **Claude Computer Use API** (Approach A). It's proven and available today. The screenshot-based approach is sufficient for most tasks:

```
Voice: "Open the settings panel and change the theme to dark"
  ↓
Ring0 on remote node receives transcript
  ↓
Ring0 takes screenshot of remote desktop (via capture_screen tool)
  ↓
Ring0 analyzes screenshot, identifies settings icon
  ↓
Ring0 calls computer_use tool: click(x, y)
  ↓
Input event sent to node → injected into OS
  ↓
Ring0 takes another screenshot to verify
  ↓
Ring0 continues until task complete
  ↓
Ring0 responds via voice: "Done, switched to dark theme"
```

### 5.5 Ring0 MCP Extensions for CUA

New MCP tools for the remote node's Ring0:

```python
@mcp.tool()
async def take_screenshot() -> str:
    """Capture the current desktop and return as an image for analysis."""

@mcp.tool()
async def mouse_click(x: int, y: int, button: str = "left") -> str:
    """Click at the given screen coordinates."""

@mcp.tool()
async def mouse_move(x: int, y: int) -> str:
    """Move the mouse cursor to the given coordinates."""

@mcp.tool()
async def type_text(text: str) -> str:
    """Type text at the current cursor position."""

@mcp.tool()
async def key_press(key: str, modifiers: list[str] = []) -> str:
    """Press a key with optional modifiers (ctrl, alt, shift, meta)."""

@mcp.tool()
async def scroll(x: int, y: int, delta_y: int) -> str:
    """Scroll at the given position."""
```

### 5.6 Phase 3 Open Questions

1. **Which Claude model for CUA?** Computer use requires specific model support. Need to verify compatibility.
2. **Action confirmation**: Should the agent ask before performing destructive actions (closing apps, deleting files)?
3. **Concurrent control**: What happens if the user is also using the remote desktop while the agent is acting? Conflict resolution?
4. **Accessibility APIs**: On macOS, accessibility APIs (AXUIElement) could provide structured UI info without screenshots. Worth exploring as a complement to vision.

---

## 6. Cross-Cutting Concerns

### 6.1 Security

**Node authentication:**
- Pre-shared API key per node, generated on the hub when adding a node
- Key exchanged out-of-band (copy-paste into node config)
- Key transmitted in WS connection query param (over WSS/TLS)
- Hub stores bcrypt hash; node stores plaintext in config file
- Keys are revocable: delete node on hub to invalidate

**Transport security:**
- All node ↔ hub communication over WSS (TLS)
- Remote desktop input events are sensitive — same TLS protection
- Node API keys should be rotatable without downtime

**Authorization scope:**
- A node API key grants full control of that node's sessions
- Consider per-capability authorization in future (read-only vs full control)

### 6.2 Reliability

**Node offline/online transitions:**
- Hub marks node offline after 90s without heartbeat
- UI shows node status (green/red dot, like session connection status) and displays a toast notification on offline/reconnect events
- When node reconnects: re-registers, sends current session list, hub reconciles
- Active browser WS connections to offline node's sessions show "Reconnecting..." (existing behavior)
- No message queuing while a node is offline — messages are dropped (not buffered)
- No automatic failover to another node — user manually switches if desired
- No state sync on reconnect — only the current session list is exchanged, not session histories or pending messages

**Hub restart:**
- Nodes reconnect via WS (they already retry)
- Node registry persisted to `~/.vibr8/nodes.json`
- Sessions on the local node restored via existing persistence

**Message ordering:**
- WS tunnel is ordered (TCP). Messages within a node connection are delivered in order.
- Cross-node ordering is not guaranteed (not needed for current use cases)

### 6.3 Deployment

**Phase 1 deployment of a remote node:**
```bash
# On the remote host:
pip install vibr8-node  # or: git clone + uv sync
vibr8-node --hub wss://vibr8.ringzero.ai --api-key <key> --name "cloud-dev"
```

**Configuration file** (`~/.vibr8-node/config.json`):
```json
{
  "hub_url": "wss://vibr8.ringzero.ai",
  "api_key": "sk-node-...",
  "name": "cloud-dev",
  "ring0": {
    "enabled": true,
    "model": "claude-sonnet-4-6"
  },
  "work_dir": "~/projects"
}
```

---

## 7. Implementation Milestones

### Phase 1: Multi-Node (estimated complexity: large)

**Milestone 1.1 — Node registry on hub**
- `server/node_registry.py`: Node data model, registration, heartbeat tracking
- `server/routes.py`: `/api/nodes/*` endpoints
- `/ws/node/{nodeId}` WebSocket handler
- API key generation and validation
- Persist registry to `~/.vibr8/nodes.json`

**Milestone 1.2 — vibr8-node agent**
- Package structure, entry point, config loading
- WS tunnel connection to hub (with auto-reconnect)
- Reuse `cli_launcher.py` and `ring0.py` for local session management
- Heartbeat loop
- Command handler for hub messages (create session, send message, etc.)

**Milestone 1.3 — Hub message routing**
- `WsBridge` changes: session → node routing table
- Proxy browser messages to remote nodes via WS tunnel
- Proxy node responses back to browser clients
- Remote session state synchronization (lightweight Session proxies)

**Milestone 1.4 — Frontend node switcher**
- Node list in store, polling `/api/nodes`
- Node dropdown in Sidebar
- Node switching: disconnect current sessions, load new node's sessions
- Node status indicators (online/offline)

**Milestone 1.5 — Voice routing to remote Ring0**
- `webrtc.py` `_submit_text` checks active node
- Forward transcripts to remote Ring0 via WS tunnel
- Remote Ring0 responses routed back and spoken via central TTS

**Milestone 1.6 — Integration testing**
- Local node + one remote node
- Create session on remote, send messages, receive responses
- Voice command → remote Ring0 → remote session
- Node offline/online transitions

### Phase 2: Remote Desktop (estimated complexity: very large)

**Milestone 2.1**: Screen capture daemon on vibr8-node (macOS first)
**Milestone 2.2**: WebRTC video track from node → hub → browser
**Milestone 2.3**: Input event channel (keyboard/mouse from browser to node)
**Milestone 2.4**: Desktop view in UI (new tab alongside Chat/Editor)
**Milestone 2.5**: Second screen integration (push remote desktop to second screen)
**Milestone 2.6**: Linux screen capture support

### Phase 3: Computer Use Agent (estimated complexity: large)

**Milestone 3.1**: Screenshot capture MCP tool on node
**Milestone 3.2**: Input injection MCP tools (click, type, scroll)
**Milestone 3.3**: Ring0 CUA mode (screenshot → analyze → act loop)
**Milestone 3.4**: Voice + CUA integration ("click the blue button")

### Dependencies

```
Phase 1.1 ──→ Phase 1.2 ──→ Phase 1.3 ──→ Phase 1.4
                                  │              │
                                  └──→ Phase 1.5 ┘
                                          │
                                     Phase 1.6
                                          │
                                     Phase 2.1 ──→ Phase 2.2 ──→ Phase 2.3
                                                                      │
                                                        Phase 2.4 ←──┘
                                                           │
                                                      Phase 3.1 ──→ Phase 3.2 ──→ Phase 3.3
```

### Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| WS tunnel latency for interactive voice | Conversational delay feels sluggish | Measure early; optimize message batching; consider regional hubs |
| NAT traversal for Phase 2 video | Remote desktop unusable behind strict NAT | Start with hub relay (SFU); add TURN server support |
| Screen capture platform fragmentation | Different APIs per OS, hard to maintain | Start macOS-only; Linux second; abstract behind trait/protocol |
| Security of remote desktop | Full desktop access is high-privilege | Require explicit opt-in per node; consider per-session scoping |
| Claude computer use API limitations | May not support all UI patterns | Supplement with accessibility APIs where available |
