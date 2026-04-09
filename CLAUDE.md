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
- **`stt.py`** — Speech-to-text via Whisper + Silero VAD, with prompt accumulation (multi-segment utterances)
- **`tts.py`** — Text-to-speech via OpenAI API
- **`audio_track.py`** — `QueuedAudioTrack` for outgoing TTS frames (48kHz, 20ms/frame)
- **`session_store.py`** / **`session_types.py`** — Session persistence and TypedDict message types
- **`node_tunnel.py`** — Bidirectional NDJSON command channel over WebSocket between hub and remote nodes
- **`node_registry.py`** — Remote node registration, status tracking, persistence (`~/.vibr8/nodes.json`)
- **`computer_use_agent.py`** — `ComputerUseAgent` protocol (interface for desktop/Android agents)
- **`ui_tars_agent.py`** — UI-TARS VLM agent: Act mode (goal-directed actions) and Watch mode (periodic observation)
- **`ui_tars_actions.py`** — Action parsing (model output → `ParsedAction`) and execution (device-agnostic)
- **`desktop_target.py`** — WebRTC peer that receives desktop video and sends input events (used by computer-use agent)
- **`vlm.py`** — VLM model loading (UI-TARS-7B-DPO, Qwen2-VL, BitsAndBytes int4 quantization)

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

### Remote Nodes

vibr8 supports remote nodes (Docker containers, EC2 instances, macOS machines, etc.) that can host Claude Code sessions and desktop environments.

**Communication**: Nodes connect to the hub via plain WebSocket — outbound from the node to `wss://{hub_url}/ws/node/{node_id}?apiKey=...`. Internet-traversable, no SSH or local network access required. The hub never initiates connections to nodes. Protocol is NDJSON over WebSocket with request/response correlation (`node_tunnel.py`).

**Session creation**: When a `nodeId` is specified for a Claude/Codex session, the creation request is forwarded via the WebSocket tunnel to the remote node (`routes.py:353-367`). The remote node spawns the Claude CLI locally on itself using its own `CliLauncher`. The host never spawns the CLI for remote sessions. Session IDs are qualified with the node prefix (`{node_id}:{session_id}`) for routing.

**Computer-use sessions**: VLM inference always runs on the host (in-process, local GPU). The `nodeId` only determines which remote desktop to target for WebRTC screen capture and input injection. The remote node is a "dumb terminal" — it captures screen frames and injects input events.

**Ring0 on nodes**: Each remote node runs its own Ring0 instance locally, auto-launched on startup. The node advertises `ring0Enabled` in heartbeats.

**Voice routing**: When a remote node is the active node (selected via UI or `vibr8 node {name}` voice command), voice transcripts are forwarded to that node's Ring0 via the `ring0_input` tunnel command. Falls back to local Ring0 if no remote node is active.

**Key files**: `server/node_tunnel.py`, `server/node_registry.py`, `vibr8_node/node_agent.py`, `install-node.sh`, `Dockerfile.node*`

### Computer-Use Pipeline

Vision-language model (UI-TARS) controls desktop GUIs autonomously. The agent takes screenshots, runs inference, parses actions, and executes them in a loop.

```
UITarsAgent (Act/Watch modes, server/ui_tars_agent.py)
  → VLM inference (UI-TARS-7B-DPO, int4, server/vlm.py)
  → Action parsing (server/ui_tars_actions.py)
  → DesktopTarget.inject() (WebRTC data channel, server/desktop_target.py)
```

**ComputerUseAgent protocol** (`server/computer_use_agent.py`): Any agent implementing `start()`, `stop()`, `submit_task()`, `interrupt()`, `approve()`, `reject()`, `watch_start()`, `watch_stop()` can be registered with WsBridge.

**Execution modes**: AUTO (immediate), CONFIRM (ask user), GATED (auto if parsed cleanly, else confirm).

**Coordinates**: UI-TARS normalizes to 1000×1000 grid → `execute_action()` converts to 0.0-1.0 fractions → target converts to absolute pixels.

## Voice Commands (`server/webrtc.py`)

Guard word **"vibr8"** (or **"vibrate"**) triggers commands only when followed by a known keyword. If no command matches, the entire transcript passes through unmodified (guard word included). When a command matches, any pre-text before the guard word is submitted as input first.

**Commands:** `done`, `off`, `guard`, `listen`, `quiet`, `speak`, `ring zero on`, `ring zero off`, `note`, `node {name}`
**Escape sequences (also commands):** `vibr8 vibrate ...` → `vibrate ...`, `vibr8 app ...` → `vibr8 ...`

**Guard mode:** When enabled (default), discards transcripts without the guard word. Guard word presence is checked independently of command matching.

**Note mode** (`vibr8 note`): Accumulates speech silently, mutes Ring0 TTS. Only `vibr8 done` exits. On exit, submits `[voice note]` and sends `[note_mode ended]` to Ring0. Pre-text before "vibr8 done" is added as a final fragment.

**Node switching** (`vibr8 node {name}`): Switches the active node for Ring0 routing. Matches node name from the registry.

**Ring0 routing:** When Ring0 is enabled, all voice input routes to Ring0 instead of the active session. If a remote node is the active node, voice transcripts are forwarded to that node's Ring0 via the `ring0_input` tunnel command. Falls back to local Ring0 if no remote node is active or the node is offline.

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
