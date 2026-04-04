# vibr8 — Features & Capabilities

vibr8 is a voice-first, multi-node web interface for launching and orchestrating Claude Code agents. It provides real-time session management, remote desktop streaming, second screen support, Docker-based distributed compute nodes, and a Ring0 meta-agent that orchestrates everything via voice commands.

---

## Core Platform

- **Multi-backend support** — Claude Code CLI, Codex, and terminal pseudo-backend
- **Real-time WebSocket bridge** — NDJSON protocol connecting browser ↔ Claude CLI subprocesses
- **40+ REST API endpoints** — Sessions, git, filesystem, voice, WebRTC signaling, nodes, auth
- **Persistent state** — Sessions, messages, and config survive server restarts (`~/.vibr8/`)
- **Multiple concurrent sessions** — Each session is an independent Claude Code CLI process
- **Auto-reconnect** — Exponential backoff with timeout, cancel, and manual retry
- **TLS support** — Optional HTTPS via `certs/key.pem` and `certs/cert.pem`

---

## Web UI

### Views
- **Chat** — Primary session interaction with live-streaming message feed
- **Desktop** — Remote desktop streaming via WebRTC with input injection
- **Editor** — CodeMirror 6 file viewer with syntax highlighting (JS, TS, Python, JSON, HTML, CSS, Markdown)
- **Terminal** — xterm.js-based terminal with per-session WebSocket connection
- **Second Screen** — Dedicated view for paired displays (monitors, tablets, TVs)
- **Home** — Session launcher and management

### Sidebar
- Session list with real-time status indicators (idle, running, compacting, waiting_for_permission)
- Node switcher dropdown for remote Docker nodes
- Session actions: create, rename, archive/unarchive, delete
- Ring0 status indicator with event muting toggle
- Desktop stream start/stop button
- Environment manager and settings access

### Message Feed
- Live streaming assistant output with token count
- Inline tool blocks: Bash, Edit, Write, Read, Glob, Grep, and more
- Permission banners with allow/deny/add-rule actions
- File change tracking (files touched by Edit/Write tools)
- Notification sounds (toggleable)

### Command Palette (Ctrl+Cmd+Alt+K)
| Command | Description |
|---------|-------------|
| `session.new` | Create new session |
| `session.rename` | Rename current session |
| `session.archive` | Archive current session |
| `ui.sidebar` | Toggle sidebar |
| `ui.darkMode` | Toggle dark/light mode |
| `ui.sound` | Toggle notification sound |
| `ui.taskPanel` | Toggle task panel |
| `secondscreen.pair` | Pair a second screen device |
| `desktop.start` | Start desktop stream |
| `desktop.stop` | Stop desktop stream |

### Other UI Features
- Dark/light mode (persisted per device)
- Responsive layout with collapsible sidebar
- Git integration badges (branch, ahead/behind counts, worktree indicator)
- Streaming stats display (output tokens, duration)

---

## Session Management

### Lifecycle
- **Create** — Choose backend (Claude/Codex/Terminal), model, working directory, environment, permission mode
- **Auto-name** — Sessions are automatically named from the first message using a one-shot Claude/Codex call
- **Connect/Disconnect** — WebSocket bridge with auto-reconnect
- **Kill** — Hard stop the CLI subprocess
- **Relaunch** — Resume an offline session
- **Rename** — Custom session names (in-memory only)
- **Archive/Unarchive** — Move sessions to archive with full history preserved
- **Delete** — Permanent removal

### Session Attributes
- Unique session ID (UUID)
- Backend type: `claude`, `codex`, or `terminal`
- Model selection (e.g., `claude-sonnet-4-6`, `claude-opus-4-6`)
- Permission mode: `acceptEdits` (default) or `plan` (plan-first)
- Working directory with optional git worktree isolation
- MCP config (optional JSON)
- Git branch tracking (actual branch, display branch, ahead/behind)
- Output tokens and streaming stats

### Permission Handling
- Per-session permission requests from the CLI
- Interactive allow/deny with optional rule creation
- Rule scopes: session, project, or user-wide
- Ring0 can auto-approve or deny permissions programmatically

---

## Voice & WebRTC Audio

### Architecture
- Bidirectional WebRTC audio via aiortc
- **Speech-to-text**: Whisper (via transformers) + Silero VAD
- **Text-to-speech**: OpenAI API (`OPENAI_API_KEY` required)
- Audio formats: 48kHz stereo (transport), 16kHz mono (Whisper inference)
- Blocking ML inference runs in thread pool executors

### Audio Modes
- `off` — No audio connection
- `connecting` — WebRTC handshake in progress
- `in_out` — Full duplex (mic + TTS)
- `in_only` — Mic only (TTS muted)

### Guard Word System
The guard word **"vibr8"** (or **"vibrate"**) triggers voice commands:
- If followed by a known command → command executes, guard word stripped
- If not followed by a command → entire transcript passes through unmodified
- Pre-text before the guard word is submitted as separate input first

### Voice Commands
All commands are prefixed with the guard word:

| Command | Effect |
|---------|--------|
| `done` | End active voice mode (exits note mode, etc.) |
| `off` | Disconnect audio entirely |
| `guard` | Enable guard mode (discard non-guard speech) |
| `listen` | Disable guard mode (accept all speech) |
| `quiet` | Mute TTS responses |
| `speak` | Unmute TTS |
| `ring zero on` | Enable Ring0 meta-agent |
| `ring zero off` | Disable Ring0 |
| `note` | Enter note mode |

**Escape sequences** for literal text:
- `vibr8 vibrate ...` → `vibrate ...`
- `vibr8 app ...` → `vibr8 ...`

### Guard Mode
- Default: enabled — only processes speech containing the guard word
- When disabled: all speech is accepted and routed to the active session or Ring0
- Toggleable per-client via voice command or API

### Note Mode
- Activated with `vibr8 note`
- Accumulates speech silently (Ring0 TTS muted during note)
- Only `vibr8 done` exits note mode
- On exit: submits accumulated text as `[voice note]`
- If Ring0 enabled: sends `[note_mode ended]` for Ring0 to review
- If audio disconnects mid-note: submits `[voice note interrupted]`

### Voice Profiles
Per-user profiles with configurable parameters:
- `micGain` (0.1–5.0x amplification)
- `vadThresholdDb` (-50 to -10 dB noise gate)
- `sileroVadThreshold` (0.0–1.0 neural VAD sensitivity)
- `eouThreshold` (0.0–1.0 end-of-utterance detection)
- `eouMaxRetries` (1–10 retries before forced segment end)
- `minSegmentDuration` (0.1–2.0s minimum speech length)

Multiple profiles per user; activate/deactivate as needed.

### Voice Playground
Interactive tuning environment:
- Real-time RMS dB level visualization
- VAD activation indicator
- Segment preview with transcripts and timings
- Adjustable parameters with live feedback

### Voice Logging & Debugging
- Full recordings: 48kHz stereo WAV
- Segments: 16kHz mono (per-utterance, with Whisper transcript)
- Segment parameters: snapshot of active profile settings
- Index file: JSONL with metadata for all segments
- Storage: `~/.vibr8/data/voice/logs/{username}/`
- CLI replay tool: `uv run python -m server.voice_replay --user {username} --list`

### Audio Device Selection
- Enumerate available input/output devices
- Switch devices via API or Ring0 RPC
- Persisted input device preference (by label, survives deviceId rotation)
- Automatic re-detection on device change events (Bluetooth connect/disconnect)

---

## Ring0 Meta-Agent

Ring0 is a voice-controlled orchestration agent — a persistent Claude Code session with MCP tools that manages other sessions, controls the UI, and handles multi-device coordination.

### Behavior
- When enabled, all voice input routes to Ring0 instead of the active session
- Receives event notifications (session state changes, second screen events, etc.)
- Can create/manage sessions, approve permissions, push content to screens
- Configurable model (e.g., `claude-sonnet-4-6`) via `~/.vibr8/ring0.json`

### MCP Tools (24 total)

**Session Management:**
| Tool | Description |
|------|-------------|
| `create_session` | Create new session with optional name, backend, model, project dir, initial message |
| `list_sessions` | List all sessions with state, backend, pending permissions, last activity |
| `send_message` | Send a message to any session |
| `interrupt_session` | Cancel/interrupt a running session (Ctrl+C equivalent) |
| `switch_ui` | Switch browser UI to a specific session (optionally target a specific client) |
| `get_session_output` | Get recent messages and pending permission requests |
| `get_session_mode` | Query session's permission mode |
| `set_session_mode` | Set session to "plan" or "acceptEdits" mode |

**Permission Control:**
| Tool | Description |
|------|-------------|
| `respond_to_permission` | Allow or deny a pending permission request |

**Client Management:**
| Tool | Description |
|------|-------------|
| `get_active_clients` | List all connected browser/app clients with metadata |
| `query_client` | Send RPC to a specific client (see RPC methods below) |
| `update_client_metadata` | Update client name, description, or role |

**Client RPC Methods** (via `query_client`):
- `get_state` — Current session, date/time, timezone, locale
- `get_location` — GPS coordinates (may prompt user)
- `get_visibility` — Tab visibility, focus state
- `send_notification` — Browser notification with title/body
- `read_clipboard` / `write_clipboard` — Clipboard access
- `open_url` — Open URL in new tab
- `list_audio_devices` — Enumerate audio I/O devices
- `set_audio_output` / `set_audio_input` — Switch audio device
- `bring_to_foreground` — Bring app to front (Android)
- `launch_app` — Launch app by package name (Android)
- `capture_screenshot` — Capture client screen
- `set_scale` — Adjust second screen zoom

**Second Screen Control:**
| Tool | Description |
|------|-------------|
| `pair_second_screen` | Pair a device using a 6-digit code |
| `list_second_screens` | List all paired displays with status |
| `show_on_second_screen` | Push content (markdown, image, file, pdf, html, desktop, session) |
| `query_second_screen` | Get device info, dimensions, capabilities |
| `set_second_screen_scale` | Adjust font size (absolute or relative) |
| `set_tv_safe` | Enable TV-safe padding for bezel cutoffs |
| `set_dark_mode` | Toggle dark/light mode on second screen |
| `toggle_second_screen` | Enable or disable a paired screen |

**Utilities:**
| Tool | Description |
|------|-------------|
| `launch_app` | Launch an Android app by package name or URL |
| `capture_screen` | Capture a client's screen as PNG/JPEG |
| `set_guard_mode` | Toggle voice guard mode on/off |
| `get_node_environment` | Get platform, hostname, containerized flag, display info |

### Ring0 Events
Ring0 receives structured event notifications:
- `session_state_change` — idle → running, running → waiting_for_permission, etc.
- `second_screen_connected` / `disconnected` / `paired` / `unpaired` / `enabled` / `disabled`
- `note_mode_ended` — Voice note completed, ready for review

Event routing configurable via `~/.vibr8/ring0-events.json5`:
- `send`: whether to submit to Ring0 LLM (default true)
- `template`: custom text with variable interpolation
- `summary`: short label for collapsed display
- `ui`: "visible" (default), "collapsed", or "hidden"

---

## Remote Desktop Streaming

### Screen Capture
- **Linux**: ffmpeg x11grab with built-in hardware-accelerated scaling
- **macOS**: mss (CGDisplay) with av reformat for color conversion
- Output: yuv420p av.VideoFrame objects → WebRTC VideoStreamTrack
- Configurable: target FPS (default 30), max height (default 1080)
- Shared capture: ref-counted per display, shared across multiple viewers

### Input Injection
- **Linux**: xdotool against the X11 display
- **macOS**: pyautogui (Quartz event injection)
- Mouse: move, click (left/middle/right), scroll
- Keyboard: keydown, keyup, text input
- Touch: single-finger mapped to mouse events

### Clipboard Sync
- Bidirectional via WebRTC data channel
- Get remote clipboard (copy from remote → local)
- Set remote clipboard (paste local → remote)
- Linux: xclip; macOS: pbcopy/pbpaste
- Toast notifications for clipboard operations

### Desktop Controls (Web UI)
- Scale modes: fit, fill
- Fullscreen toggle
- Virtual keyboard overlay (IME support)
- Pinch-to-zoom with pan (touch devices)
- Connection quality indicator (FPS, bitrate, RTT)
- Transport type detection (direct vs TURN relay)
- Auto-reconnect with exponential backoff (up to 3 retries)

### Desktop Roles
- **Controller**: video stream + input data channel (mouse, keyboard, clipboard)
- **Viewer**: video stream only (no input)

---

## Second Screen

### Pairing
- Second screen device generates a 6-digit pairing code
- Primary device confirms the code via UI or Ring0 voice command
- Persistent pairing per user identity
- Rate limiting: 10 codes per IP per minute, failed lookup cooldown

### Content Types
| Type | Rendering |
|------|-----------|
| `markdown` | Rendered as HTML with full formatting |
| `image` | Image display (URL or inline base64) |
| `file` | Monospace text with optional filename header |
| `pdf` | Embedded PDF viewer |
| `html` | Sandboxed iframe (scripts + same-origin) |
| `session` | Live mirror of a session's chat feed |
| `desktop` | Full remote desktop stream with input (controller mode) |
| `home` | Return to Ring0 default view |

### Display Features
- Independent dark/light mode (separate from primary)
- Adjustable scale/zoom (absolute or delta)
- TV-safe mode with configurable padding percentage
- Device naming and metadata
- Online/offline status tracking

### Content Targeting
- All connected/enabled screens (default)
- Specific screen by client ID or device name

### WebSocket Connection
- Dedicated WebSocket per paired screen to Ring0 session
- Application-level keepalive (15s ping)
- Auto-reconnect on disconnect
- Optional session mirroring WebSocket for non-Ring0 sessions

---

## Docker Nodes

### Architecture
- Remote vibr8 nodes run inside Docker containers
- Each node has its own Ring0 + Claude Code sessions
- Hub maintains a registry of all nodes
- Persistent WebSocket tunnel (NDJSON) connects each node to the hub
- Heartbeat monitoring (30s interval)

### Image Tiers
```
vibr8-node:base-deps         ← System packages, Node.js, Claude CLI, Python deps
├── vibr8-node:latest        ← + App code (standard headless node)
└── vibr8-node:gui-deps      ← + XFCE, Chrome, VS Code, Kitty, aiortc/av
    └── vibr8-node:gui       ← + App code (GUI/virtual-display node)
        └── vibr8-node:gpu   ← + NVIDIA OpenGL/EGL libs (GPU-accelerated node)
```

Code-only rebuilds (`:latest`, `:gui`) take under 1 second. Dep layers are cached and rarely rebuilt.

### vibr8-node CLI
```bash
vibr8-node create <name> [options]   # Create and start a new node container
vibr8-node start <name>              # Start a stopped node
vibr8-node stop <name>               # Stop a running node
vibr8-node restart <name>            # Restart a node
vibr8-node logs <name>               # View container logs
vibr8-node status [name]             # Show status (or all nodes)
vibr8-node list                      # List all node containers
vibr8-node update [name]             # Rebuild image and recreate container
vibr8-node shell <name>              # Open interactive shell
vibr8-node destroy <name>            # Remove container and config
```

### Create Options
| Flag | Description |
|------|-------------|
| `--hub <url>` | Hub WebSocket URL (e.g., `wss://vibr8.ringzero.ai`) |
| `--api-key <key>` | Node API key (generated on hub) |
| `--code-dir <path>` | Host directory to mount as `/code` |
| `--virtual-display` | Enable Xvfb + XFCE desktop (1920x1080) |
| `--gpu` | Enable NVIDIA GPU passthrough |
| `--gpu-device <id>` | Select specific GPU (e.g., `0`, `1`, `0,1`) |

### Virtual Display
- Xvfb on DISPLAY=:99 with configurable resolution
- XFCE4 desktop environment with taskbar and window management
- Pre-installed: Google Chrome (with `--no-sandbox`), VS Code, Kitty terminal
- Stale X lock file cleanup on container restart

### GPU Support
- NVIDIA GPU passthrough via `--gpus "device=<id>"`
- GPU enumeration in interactive wizard (`nvidia-smi`)
- Whiptail checklist for multi-GPU selection
- OpenGL/EGL userspace libraries for hardware rendering

### Desktop Streaming on Nodes
- Lightweight `DesktopWebRTCManager` runs inside the container
- Desktop WebRTC offers are forwarded from hub to node via tunnel
- ICE/TURN server config relayed from hub for NAT traversal
- Screen capture of the container's virtual display (DISPLAY=:99)
- Full input injection (mouse, keyboard, clipboard) via xdotool

### Node Tunnel Commands
The hub forwards these commands to nodes via the persistent WebSocket tunnel:
- Session management: create, list, kill, relaunch, delete, archive, unarchive, rename
- Message routing: submit_message, cli_input, browser_message, interrupt
- Permission handling: set_permission_mode, respond_permission
- State queries: get_session_output
- Voice: ring0_input
- Desktop: webrtc_offer (SDP exchange for remote desktop)

### Data Persistence
- Node config and session state: `~/.vibr8/docker-nodes/<name>/vibr8-data/`
- Claude CLI auth: `~/.vibr8/docker-nodes/<name>/claude-config/`
- Survives container destroy/recreate (bind-mounted volumes)

---

## Authentication

### User/Password Auth
- Optional — disabled when no `users.json` exists (local dev mode)
- Passwords: bcrypt hashed, stored in `~/.vibr8/users.json`
- Management CLI: `python -m server.manage_users add/remove/list <username>`

### Token Types
| Type | Format | Expiry | Revocable |
|------|--------|--------|-----------|
| Session | `s:username:timestamp:signature` | 30 days | No |
| Device | `d:username:timestamp:signature` | Never | Yes |
| Service | `svc:service:timestamp:signature` | Never | No |
| Legacy | `username:timestamp:signature` | 30 days | No |

All tokens are HMAC-signed with `~/.vibr8/secret.key` — stateless validation, survives server restarts.

### Device Tokens
- Created via API for native apps (mobile, watch, etc.)
- No expiry — revocable via API
- Last-used timestamp tracking
- Metadata: name, creation date

### Pairing
- 6-digit pairing codes with 10-minute TTL
- Rate limiting: 10 codes per IP per minute
- Failed lookup cooldown (5 failures → 1s lockout)
- Type-specific: "Device" (phone/watch) or "SecondScreen" (display)

### Public Routes (no auth required)
- `/ws/cli/`, `/ws/node/` — Internal WebSocket tunnels
- `/api/auth/login`, `/api/auth/me` — Login flow
- `/api/pairing/*` — Device pairing
- `/api/ring0/*` — Ring0 MCP server
- `/api/nodes/register` — Node registration (API key auth)
- Static assets

---

## Git Integration

- **Repository info** — Root, name, current branch, default branch, worktree detection
- **Branch listing** — Local and remote branches with ahead/behind counts
- **Worktree management** — Create, list, and remove git worktrees
- **Fetch/Pull** — Remote fetch and pull with status reporting
- **Session isolation** — Sessions can be pinned to a git worktree for branch isolation
- **Worktree tracking** — Worktree-to-session mapping persisted in `~/.vibr8/worktrees.json`

---

## File System & Editor

### File Operations
- Directory listing with tree structure
- File read/write
- File and directory creation, rename, delete
- Git diff for changed files
- Raw file download

### Editor (CodeMirror 6)
- Syntax highlighting: JavaScript, TypeScript (JSX/TSX), Python, JSON, HTML, CSS, Markdown
- Image preview for image files
- File tree browser (react-arborist) with expand/collapse
- Breadcrumb navigation
- Read-only and editable modes
- Per-session open file tracking

---

## Environments

Named sets of environment variables that can be applied to sessions:
- Create, read, update, delete via API
- Stored in `~/.vibr8/envs/` as JSON files
- Variables passed to Claude CLI subprocess at launch
- Slugified names for filesystem compatibility

---

## Usage Limits

- Fetches Anthropic API usage data from OAuth credentials
- Rate buckets: 5-hour, 7-day, and extra usage (if enabled)
- In-memory cache with 60s TTL
- Graceful fallback when credentials unavailable
- Displayed in UI for rate limit awareness

---

## Clients

### Web Browser (Primary)
- Full-featured UI with all capabilities listed above
- Responsive layout for desktop and mobile
- PWA support (manifest.json, service worker)

### Android App (Planned)
- Native companion app for phone
- Shared auth via device tokens
- WebRTC audio for voice commands

### Wear OS Watch App (Planned — Pixel Watch 4)
- Voice-first smartwatch client
- Minimal UI: home screen, permission notifications, session list, node switcher
- WebSocket + WebRTC audio
- Push-to-talk or always-listening
- Shared auth via Wear OS Data Layer (from phone app) with PIN fallback
- **Phase 1**: WebSocket + permissions + session list
- **Phase 2**: WebRTC voice + TTS + echo cancellation
- **Phase 3**: Shared auth, complications, tiles, haptics

### Second Screen Devices
- Any browser: tablets, TVs, monitors, phones
- Paired via 6-digit code
- Receives pushed content from Ring0
- Full desktop controller mode (touch, keyboard, clipboard)
- Independent display settings (dark mode, scale, TV-safe)

---

## Deployment

- **Development**: `make dev` — runs backend (port 3456) + Vite frontend (port 5174)
- **Production**: `NODE_ENV=production` — serves built frontend from `web/dist/`
- **Docker nodes**: Distributed across machines via WebSocket tunnels
- **Reverse proxy**: Supports nginx/SSH tunnel chains with application-level keepalive (15s ping)
- **TLS**: Optional, via `certs/key.pem` and `certs/cert.pem`

---

## Configuration Files

| Path | Purpose |
|------|---------|
| `~/.vibr8/users.json` | User credentials (bcrypt) |
| `~/.vibr8/secret.key` | HMAC signing secret |
| `~/.vibr8/ice-servers.json` | STUN/TURN server config |
| `~/.vibr8/ring0.json` | Ring0 settings (enabled, model, sessionId) |
| `~/.vibr8/ring0-events.json5` | Ring0 event routing config |
| `~/.vibr8/sessions.json` | Session launcher state |
| `~/.vibr8/worktrees.json` | Worktree-to-session mapping |
| `~/.vibr8/envs/` | Environment variable profiles |
| `~/.vibr8/nodes.json` | Node registry and API keys |
| `~/.vibr8/device-tokens.json` | Device token metadata |
| `~/.vibr8/data/voice/` | Voice logs, profiles, recordings |
| `~/.vibr8/docker-nodes/` | Per-node persistent data |
