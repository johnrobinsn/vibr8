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
