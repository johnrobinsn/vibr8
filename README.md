<p align="center">
  <img src="web/public/logo.svg" alt="vibr8" width="80" />
</p>

<h1 align="center">vibr8</h1>

<p align="center">
  Web UI for launching and interacting with Claude Code agents
</p>

---

## Features

- **Multi-session management** — Launch, monitor, and switch between multiple Claude Code sessions
- **Real-time voice** — Bidirectional WebRTC audio with push-to-talk, speech-to-text (Whisper), and text-to-speech (OpenAI)
- **Live streaming** — Watch agent output stream in real-time over WebSocket (NDJSON protocol)
- **Code viewer** — Syntax-highlighted file viewer with CodeMirror
- **Git integration** — Branch tracking, worktree isolation, ahead/behind counts, and diff stats per session
- **Environment sets** — Create and switch between named sets of environment variables
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

### Configuration Directory

vibr8 stores configuration in `~/.vibr8/`:

| File / Directory | Description |
|---|---|
| `users.json` | User credentials (bcrypt-hashed). Presence enables auth. |
| `envs/` | Environment profiles (named sets of env vars) |
| `worktrees/` | Worktree-to-session mappings |
| `worktrees.json` | Worktree tracking metadata |
| `ice-servers.json` | STUN/TURN server config for WebRTC (optional) |

## Environment Variables

| Variable          | Default | Description                                      |
|-------------------|---------|--------------------------------------------------|
| `PORT`            | `3456`  | Backend server port                              |
| `NODE_ENV`        | —       | Set to `production` to serve built frontend      |
| `OPENAI_API_KEY`  | —       | Required for text-to-speech                      |

For HTTPS (required for WebRTC on non-localhost), place `key.pem` and `cert.pem` in `certs/`.

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

### Ring0 Meta-Agent

When Ring0 is enabled, all voice routes to Ring0 instead of the active session. Ring0 can list/create/interrupt sessions, send messages, switch the UI, manage permissions, and control second screen displays. Ring0's TTS is automatically muted during note mode.

### Ring0 Event Configuration

Events sent to Ring0 (session state changes, second screen connect/disconnect/pair) are structured JSON objects routed through a configurable rules engine. Configure via `~/.vibr8/ring0-events.json5`:

```json5
{
  "rules": [
    // Suppress all second screen events
    { "match": { "type": "second_screen_*" }, "suppress": true },

    // Custom template for state changes, collapsed in UI
    {
      "match": { "type": "session_state_change" },
      "template": "Session ${evt.session} changed: ${evt.transition}",
      "summary": "${evt.session}: ${evt.transition}",
      "ui": "collapsed"
    },

    // Catch-all: pass everything else through
    { "match": { "type": "*" } }
  ]
}
```

**Rules** are first-match-wins. Each rule has a `match` object — events are flat key/value dicts and all match keys use glob patterns (`fnmatch`).

**Rule options:**
- `suppress: true` — drop the event (Ring0 never sees it)
- `template` — text the LLM sees (`${evt.fieldName}` for fields, `${evt}` for full JSON)
- `summary` — short label for collapsed UI mode (also supports interpolation)
- `ui` — `"visible"` (default, system-message divider), `"collapsed"` (disclosure triangle), or `"hidden"` (sent to Ring0 but not shown in browser)

**Event types:** `session_state_change` (fields: `session`, `sessionId`, `transition`, `detail`), `second_screen_connected` / `second_screen_disconnected` / `second_screen_paired` / `second_screen_unpaired` / `second_screen_enabled` / `second_screen_disabled` (fields: `clientId`, plus `user` for paired), `note_mode_ended` (no fields).

See `ring0-events.example.json5` for a fully documented example config.

## Architecture

```
Browser (React 19 + WebRTC)
    │
    ├── WebSocket (NDJSON) ──► WsBridge ──► Claude Code CLI subprocess
    │
    └── RTCPeerConnection ──► WebRTCManager
                                  ├── AsyncSTT (Whisper + Silero VAD)
                                  └── QueuedAudioTrack (OpenAI TTS)
```

**Backend** — Python/aiohttp with 40+ REST endpoints for sessions, git, filesystem, and environments. Each session spawns a Claude Code CLI subprocess managed via WebSocket bridge.

**Frontend** — React 19, TypeScript, Tailwind CSS 4, Zustand state management. Code viewing with CodeMirror, file trees with react-arborist.

## Tech Stack

**Backend:** Python 3.11+, aiohttp, aiortc, PyTorch, Whisper, Silero VAD

**Frontend:** React 19, TypeScript, Vite, Tailwind CSS 4, Zustand, CodeMirror, xterm.js

## License

[Apache 2.0](LICENSE)
