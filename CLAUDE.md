# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

vibr8 is a Web UI for launching and interacting with Claude Code agents. Python/aiohttp backend with a React/TypeScript frontend. Ported from The Vibe Companion (TypeScript/Hono).

## Commands

```bash
make install          # Install all deps (uv sync + cd web && bun install)
make dev              # Run backend (port 3456) + frontend (port 5174) together
make dev-api          # Backend only
make dev-frontend     # Frontend only (Vite, proxies /api and /ws to :3456)
make build            # Build frontend for production
make test             # All tests
make test-py          # pytest server/tests/ -v
make test-frontend    # cd web && bun run test (vitest)
```

Single Python test: `uv run pytest server/tests/test_foo.py::test_name -v`
Single frontend test: `cd web && bun run vitest run src/foo.test.ts`
Frontend typecheck: `cd web && bun run typecheck`

## Architecture

### Backend (`server/`)

Python 3.11+, aiohttp, asyncio-heavy. Key modules:

- **`main.py`** — App factory, startup/shutdown hooks, serves built frontend in production
- **`ws_bridge.py`** — Central message router: Claude Code CLI ↔ browser WebSocket. NDJSON protocol. Two WS endpoints: `/ws/cli/{session_id}` and `/ws/browser/{session_id}`
- **`cli_launcher.py`** — Spawns/manages Claude Code CLI subprocesses per session. Auto-relaunches when browser reconnects to an offline session
- **`routes.py`** — 40+ REST endpoints under `/api/` for sessions, git, filesystem, environments, usage limits, WebRTC signaling
- **`webrtc.py`** — WebRTC peer connections with bidirectional audio (aiortc)
- **`stt.py`** — Speech-to-text via Whisper + Silero VAD
- **`tts.py`** — Text-to-speech via OpenAI API
- **`audio_track.py`** — `QueuedAudioTrack` for outgoing TTS frames (48kHz, 20ms/frame)
- **`session_store.py`** / **`session_types.py`** — Session persistence and TypedDict message types

### Frontend (`web/src/`)

React 19, TypeScript, Vite, Tailwind CSS 4, Zustand for state.

- **`api.ts`** — REST client
- **`ws.ts`** — WebSocket client (NDJSON)
- **`store.ts`** — Zustand state management
- **`webrtc.ts`** — WebRTC connection handler
- **`components/`** — UI components (terminal via xterm.js, file tree via react-arborist, code view via CodeMirror)

### Communication Flow

Browser ↔ (WebSocket NDJSON) ↔ `WsBridge` ↔ (WebSocket NDJSON) ↔ Claude Code CLI subprocess

The WebSocket protocol is reverse-engineered and documented in `WEBSOCKET_PROTOCOL_REVERSED.md`.

## Voice Commands (`server/webrtc.py`)

Guard word **"vibr8"** (or **"vibrate"**) triggers commands only when followed by a known keyword. If no command matches, the entire transcript passes through unmodified (guard word included). When a command matches, any pre-text before the guard word is submitted as input first.

**Commands:** `done`, `off`, `guard`, `listen`, `quiet`, `speak`, `ring zero on`, `ring zero off`, `note`
**Escape sequences (also commands):** `vibr8 vibrate ...` → `vibrate ...`, `vibr8 app ...` → `vibr8 ...`

**Guard mode:** When enabled (default), discards transcripts without the guard word. Guard word presence is checked independently of command matching.

**Note mode** (`vibr8 note`): Accumulates speech silently, mutes Ring0 TTS. Only `vibr8 done` exits. On exit, submits `[voice note]` and sends `[note_mode ended]` to Ring0. Pre-text before "vibr8 done" is added as a final fragment.

**Ring0 routing:** When Ring0 is enabled, all voice input routes to Ring0 instead of the active session.

See `README.md` for full voice command documentation.

## Conventions

- **Wire format**: REST API and TypedDicts use camelCase for JSON compatibility with the frontend. Python internals use snake_case.
- **Logging**: Structured with prefixes: `[server]`, `[ws]`, `[webrtc]`, `[routes]`, `[ws-bridge]`
- **Async**: All I/O is async. Blocking work (ML inference) goes through thread pool executors.
- **Package managers**: `uv` for Python, `bun` for frontend
- **pytest-asyncio**: `asyncio_mode = "auto"` — async test functions are auto-detected

## Environment

- `PORT` (default 3456) — Backend server port
- `NODE_ENV=production` — Enables serving built frontend from `web/dist/`
- `OPENAI_API_KEY` — Required for TTS
- Optional TLS: place `key.pem` and `cert.pem` in `certs/` for HTTPS
