<p align="center">
  <img src="web/public/logo.svg" alt="vibr8" width="80" />
</p>

<h1 align="center">vibr8</h1>

<p align="center">
  Web UI for launching and interacting with Claude Code agents
</p>

---

## Features

- **Multi-session management** — Launch, monitor, and switch between multiple Claude Code or Codex sessions
- **Real-time voice** — Bidirectional WebRTC audio with push-to-talk, speech-to-text (Whisper + Silero VAD), and text-to-speech (OpenAI)
- **Prompt accumulation** — Multi-segment utterances are combined before submission, so pauses mid-thought don't split your input
- **Live streaming** — Watch agent output stream in real-time over WebSocket (NDJSON protocol)
- **Remote nodes** — Run sessions on remote machines (Docker, EC2, macOS) connected via WebSocket tunnel
- **Computer-use agent** — Vision-language model (UI-TARS) controls desktop GUIs autonomously — screenshots, clicks, typing, scrolling
- **Ring0 meta-agent** — Voice-controlled supervisor that manages sessions, permissions, second screens, and client devices via MCP tools
- **Second screen** — Push markdown, images, PDFs, HTML, or live session mirrors to paired display devices (TVs, tablets, etc.)
- **Code viewer** — Syntax-highlighted file viewer with CodeMirror
- **Git integration** — Branch tracking, worktree isolation, ahead/behind counts, and diff stats per session
- **Environment sets** — Create and switch between named sets of environment variables
- **Voice commands** — Guard-word activated commands for hands-free control (guard mode, note mode, node switching, TTS mute, etc.)
- **Auto-reconnect** — Automatic reconnection with timeout, cancel, and manual retry
- **Dark / light mode** — Persisted theme preference

## Prerequisites

- Python 3.11 or 3.12
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [Bun](https://bun.sh/) (JavaScript runtime / package manager)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated

## Quick Start

```bash
# Install dependencies
make install

# Start development servers (backend on :3456, frontend on :5174)
make dev
```

Open [http://localhost:5174](http://localhost:5174) in your browser.

## Commands

| Command              | Description                                    |
|----------------------|------------------------------------------------|
| `make install`       | Install Python and frontend dependencies       |
| `make dev`           | Run backend + frontend together                |
| `make dev-api`       | Backend only (port 3456)                       |
| `make dev-frontend`  | Frontend only (port 5174, proxies to backend)  |
| `make build`         | Build frontend for production                  |
| `make test`          | Run all tests                                  |
| `make test-py`       | Python tests (`pytest`)                        |
| `make test-frontend` | Frontend tests (`vitest`)                      |

## Remote Nodes

vibr8 supports remote nodes that can host Claude Code sessions and desktop environments. Nodes connect to the hub via outbound WebSocket — no SSH or local network access required.

### How It Works

- Nodes register with the hub using API keys and maintain a persistent WebSocket connection
- Sessions created with a `nodeId` are forwarded to the remote node, which spawns the CLI locally
- Computer-use sessions run VLM inference on the hub but target the remote node's desktop for screen capture and input injection
- Each node can run its own Ring0 instance for voice-controlled session management

### Node Installation

```bash
# Interactive install (macOS / Linux)
curl -fsSL https://vibr8.ringzero.ai/install-node | bash

# From local repo
./install-node.sh

# Docker (non-interactive)
./install-node.sh --non-interactive --no-service --source ./

# Add desktop streaming to an existing install
./install-node.sh --desktop-only
```

Docker images are provided for various configurations:

| Dockerfile | Purpose |
|---|---|
| `Dockerfile.node` | Runtime node (no desktop) |
| `Dockerfile.node-gpu` | GPU-enabled variant |
| `Dockerfile.node-gui` | X11 GUI desktop support |

### Node Management

- **API keys**: Manage node API keys in the Settings page
- **Voice switching**: Say "vibr8 node {name}" to switch the active node
- **Node config**: Stored at `~/.vibr8-node/config.json` on the node

## Computer-Use Agent

The computer-use agent uses a vision-language model (UI-TARS 7B) to control desktop GUIs autonomously. It takes screenshots, runs inference, parses actions, and executes them in a loop.

### Supported Actions

`click`, `type`, `scroll`, `press`, `wait`, `finished`

### Execution Modes

| Mode | Behavior |
|---|---|
| AUTO | Execute actions immediately |
| CONFIRM | Always ask user before executing |
| GATED | Auto-execute if parsed cleanly, otherwise ask |

### Controls

The agent panel provides pause/resume, watch/act mode toggle, and an execution mode selector. In CONFIRM/GATED mode, an approval dialog appears before risky actions.

## Ring0 Meta-Agent

Ring0 is a supervisor agent that controls vibr8 via voice. When enabled, all voice input routes to Ring0 instead of the active session. Ring0 exposes MCP tools for:

- **Session management** — Create, list, rename, interrupt, and send messages to sessions (Claude, Codex, or computer-use backends)
- **Permission handling** — View and respond to pending permission requests
- **UI control** — Switch which session the browser displays, set session modes (plan / acceptEdits)
- **Client management** — List connected clients, query device info, send notifications, control audio devices, read/write clipboard
- **Second screen** — Pair, list, push content, adjust scale/dark mode/TV-safe margins
- **Screenshot capture** — Capture any client's current view
- **Android control** — Launch apps on connected Android devices
- **Node info** — Query the current node's environment (platform, hostname, containerized, display available)
- **Guard mode** — Enable/disable voice guard mode programmatically

### Ring0 Configuration

Config stored at `~/.vibr8/ring0.json`:

```json
{
  "enabled": true,
  "sessionId": "...",
  "model": "claude-sonnet-4-6"
}
```

Changing the model requires a Ring0 restart (toggle off/on).

### Ring0 Event Rules

Events sent to Ring0 (session state changes, second screen events) are configurable via `~/.vibr8/ring0-events.json5`:

```json5
{
  "rules": [
    // Custom template for state changes, collapsed in UI
    {
      "match": { "type": "session_state_change" },
      "template": "Session ${evt.session} changed: ${evt.transition}",
      "summary": "${evt.session}: ${evt.transition}",
      "ui": "collapsed"
    },
    // Hide second screen events from Ring0 but show in UI
    { "match": { "type": "second_screen_*" }, "send": false },
    // Catch-all
    { "match": { "type": "*" } }
  ]
}
```

Rules are first-match-wins with glob pattern matching. Options: `send` (submit to LLM), `template`/`summary` (text formatting with `${evt.field}` interpolation), `ui` (`"visible"`, `"collapsed"`, `"hidden"`).

## Second Screen

Pair external displays (TVs, tablets, spare monitors) to show content pushed by Ring0 or mirrored from sessions.

### Setup

1. Open `/second-screen` on the display device
2. A pairing code appears on screen
3. Ring0 pairs it: "pair second screen, code 123456"

### Content Types

| Type | Description |
|---|---|
| `markdown` | Rendered markdown |
| `image` | URL or base64-encoded image |
| `file` | Text file viewer |
| `pdf` | URL or base64-encoded PDF |
| `html` | Raw HTML |
| `session` | Live mirror of a session's chat |
| `desktop` | Stream a remote desktop |
| `home` | Return to default Ring0 view |

### Display Settings

- **Scale** — Adjust font size (absolute 0.5–3.0 or relative delta)
- **TV-safe mode** — Add padding for TV bezels (configurable percentage)
- **Dark mode** — Independent dark/light toggle per screen

## Voice Commands

vibr8 supports voice input via WebRTC. Speech is transcribed by Whisper + Silero VAD and routed to the active session (or Ring0 if enabled).

### Guard Word

The guard word is **"vibr8"** (also recognized as **"vibrate"**). It serves two purposes:

1. **Command prefix** — When followed by a known command, the guard word and command are stripped and the command is executed.
2. **Guard mode gate** — When guard mode is enabled, only transcripts containing the guard word are accepted (others are silently discarded).

If the guard word appears but is **not** followed by a known command, the entire transcript passes through **unmodified**. This allows natural speech like "check the vibr8 session" to arrive intact.

When a command **is** matched, any text spoken *before* the guard word is submitted as separate input first, then the command executes. For example, "finish this up vibr8 guard" submits "finish this up" then enables guard mode.

### Commands

All commands are prefixed with the guard word (e.g., "vibr8 guard").

| Command | Effect |
|---------|--------|
| `done` | End the active voice mode (e.g., note mode). Always recognized, even inside voice modes. |
| `off` | Disconnect audio entirely. |
| `guard` | Enable guard mode — only guard-prefixed speech is accepted. |
| `listen` | Disable guard mode — all speech is accepted. |
| `quiet` | Mute TTS — the agent stops speaking responses aloud. |
| `speak` | Unmute TTS — resume spoken responses. |
| `ring zero on` | Enable Ring0 meta-agent. All voice routes to Ring0. |
| `ring zero off` | Disable Ring0. Voice routes to the active session. |
| `note` | Enter note mode — speech is accumulated silently until "vibr8 done". |
| `node {name}` | Switch the active remote node for Ring0 routing. |

### Escape Sequences

These are treated as commands (guard word is stripped) but produce transformed output:

| Escape | Result | Use case |
|--------|--------|----------|
| `vibr8 vibrate ...` | `vibrate ...` | Say the literal word "vibrate" |
| `vibr8 app ...` | `vibr8 ...` | Say "vibr8" literally when a command would otherwise intercept |

### Note Mode

Say **"vibr8 note"** to enter note mode:

- All speech is silently accumulated (not submitted to any session).
- Ring0's TTS is muted so it doesn't talk over dictation.
- Only **"vibr8 done"** exits — all other speech is captured as fragments.
- Text before "vibr8 done" in the final transcript is added as a last fragment.

On exit, the accumulated note is submitted as a `[voice note]` message. If Ring0 is enabled, a `[note_mode ended]` review message follows so Ring0 can summarize anything that happened during dictation. If audio disconnects during note mode, a `[voice note interrupted]` message is submitted instead.

### Guard Mode

When guard mode is **enabled** (default), transcripts without the guard word are discarded. When **disabled** ("vibr8 listen"), all speech is submitted.

The guard word check is independent of command matching — "check the vibr8 session" passes the guard check (word is present) even though no command matches (text passes through unmodified).

## CLI Tools

### User Management

vibr8 supports optional password authentication. When enabled, all API and WebSocket endpoints require a session cookie.

```bash
# Add a user (prompts for password)
uv run python -m server.manage_users add <username>

# List all users
uv run python -m server.manage_users list

# Remove a user
uv run python -m server.manage_users remove <username>
```

Auth is **opt-in**: if no users exist, all endpoints are open (local dev mode). Adding the first user enables auth and requires login.

### Voice Replay

Debug voice transcription issues by replaying recorded audio segments through the STT pipeline.

```bash
# List recent voice segments for a user
uv run python -m server.voice_replay --user <username> --list

# Replay a specific segment
uv run python -m server.voice_replay --user <username> --segment <segment_id>
```

Voice logs are stored at `~/.vibr8/data/voice/logs/{username}/` with recordings, segments, and an index file.

## Configuration

### Configuration Directory

vibr8 stores configuration in `~/.vibr8/`:

| File / Directory | Description |
|---|---|
| `users.json` | User credentials (bcrypt-hashed). Presence enables auth. |
| `ring0.json` | Ring0 config: `enabled`, `sessionId`, `model` |
| `ring0-events.json5` | Ring0 event routing rules |
| `nodes.json` | Registered remote nodes |
| `envs/` | Environment profiles (named sets of env vars) |
| `sessions/` | Persisted session data (atomic writes) |
| `worktrees/` | Worktree-to-session mappings |
| `worktrees.json` | Worktree tracking metadata |
| `ice-servers.json` | STUN/TURN server config for WebRTC (optional) |
| `secret.key` | HMAC signing key for auth tokens |
| `data/voice/logs/` | Voice recordings and segment logs |

### Environment Variables

| Variable          | Default | Description                                      |
|-------------------|---------|--------------------------------------------------|
| `PORT`            | `3456`  | Backend server port                              |
| `NODE_ENV`        | —       | Set to `production` to serve built frontend      |
| `OPENAI_API_KEY`  | —       | Required for text-to-speech                      |

For HTTPS (required for WebRTC on non-localhost), place `key.pem` and `cert.pem` in `certs/`.

## Architecture

```
Browser (React 19 + WebRTC)
    │
    ├── WebSocket (NDJSON) ──► WsBridge ──► Claude Code CLI subprocess
    │                                  └──► Codex app-server (JSON-RPC)
    │
    ├── RTCPeerConnection ──► WebRTCManager
    │                              ├── AsyncSTT (Whisper + Silero VAD)
    │                              └── QueuedAudioTrack (OpenAI TTS)
    │
    └── Second Screen ──► WebSocket (content push, session mirror)

Remote Nodes ──► WebSocket tunnel ──► Hub
    ├── CliLauncher (spawns Claude/Codex locally)
    ├── DesktopWebRTC (screen capture + input injection)
    └── Ring0 (optional, local MCP server)

Computer-Use Pipeline:
    UITarsAgent ──► VLM inference (UI-TARS 7B, int4)
                      ──► Action parsing
                      ──► DesktopTarget.inject() (WebRTC data channel)
```

**Backend** — Python/aiohttp with 40+ REST endpoints for sessions, git, filesystem, environments, nodes, and WebRTC signaling. Each session spawns a Claude Code CLI subprocess (or Codex app-server) managed via WebSocket bridge.

**Frontend** — React 19, TypeScript, Tailwind CSS 4, Zustand state management. Code viewing with CodeMirror, file trees with react-arborist, terminal via xterm.js.

## Tech Stack

**Backend:** Python 3.11+, aiohttp, aiortc, PyTorch, Whisper, Silero VAD, Transformers, BitsAndBytes

**Frontend:** React 19, TypeScript, Vite, Tailwind CSS 4, Zustand, CodeMirror, xterm.js

## License

[Apache 2.0](LICENSE)
