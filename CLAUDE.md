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

The codebase is split into three Python packages:

- **`vibr8_core/`** — Shared node-scoped modules. Both the hub's self-node subprocess and every remote node import from here. The single canonical implementation of every node operation lives here.
- **`server/`** — Hub-only modules. Browser WebSockets, WebRTC, STT/TTS, node registry, tunnel server, auth.
- **`vibr8_node/`** — Node agent process. Connects to the hub via tunnel; on startup, the hub spawns its own as the "self-node" (via `--self-mode`).

### `vibr8_core/` — shared node code

- **`node_operations.py`** — `NodeOperations`: single canonical implementation of every per-node action (sessions, FS, git, envs, artifacts, Ring0 control, scheduler, ring0_events). Both hub and node import this. Methods are tunnel-callable; the node-side dispatcher routes `_cmd_*` → `NodeOperations.method` via generic `getattr`.
- **`node_client.py`** — `NodeClient` protocol + `RemoteNodeClient` (tunnel via `__getattr__` generic dispatch) + `SwappableNodeClient` + `QualifyingNodeClient` (rewrites sessionId at the hub boundary so self-node sessions appear as remote-prefixed IDs).
- **`hub_browser_bridge.py`** — `HubBrowserBridge`: hub-only browser/client tracking + broadcasts. Today delegates to `WsBridge` via `__getattr__`; designed so its backing can be swapped during a future code-purity refactor.
- **`ws_bridge.py`** — Per-node session router: sessions dict, pen system, message routing CLI↔browser, Ring0 event emission. On the hub it operates in proxy-only mode (browser-tracking + tunneled session forwarding). On the node it owns real session state.
- **`ring0.py`**, **`ring0_events.py`**, **`ring0_scheduler.py`**, **`ring0_mcp.py`** — Ring0 manager + event router + scheduler + MCP server (all per-node).
- **`cli_launcher.py`** — Spawns/manages Claude/Codex/OpenCode/Hermes CLI subprocesses.
- **`session_store.py`**, **`session_types.py`**, **`session_names.py`** — Session persistence and shared TypedDicts.
- **`env_manager.py`**, **`artifacts.py`**, **`worktree_tracker.py`**, **`git_utils.py`** — Per-node resource managers.
- **`{codex,hermes,opencode}_adapter.py`** — Backend ACP/JSON-RPC adapters used by `CliLauncher`.

### `server/` — hub-only modules

- **`main.py`** — App factory, startup/shutdown hooks, **spawns the self-node subprocess** at boot, swaps `local_node_ops` to point at the loopback tunnel once registered, restart-on-crash for the self-node.
- **`routes.py`** — REST API. Handlers are manager-free: they resolve a `NodeClient` via `_resolve_client(session_id)` or `_resolve_node_client(node_id)` and call the same `NodeClient` methods regardless of whether the target is hub-self or remote.
- **`webrtc.py`** — WebRTC peer connections, audio I/O. Voice routing forwards transcripts to the active node via `local_node_ops.ring0_input`. Ring0 status cached from periodic `local_node_ops.ring0_status()` calls.
- **`stt.py`** — Speech-to-text via Whisper + Silero VAD, with prompt accumulation.
- **`tts.py`** — Text-to-speech via OpenAI API.
- **`audio_track.py`** — `QueuedAudioTrack` for outgoing TTS frames (48kHz, 20ms/frame).
- **`node_tunnel.py`** — Hub-side WebSocket tunnel server. NDJSON with request/response correlation.
- **`node_registry.py`** — Remote node registration, status tracking, persistence (`~/.vibr8/nodes.json`).
- **`session_registry.py`** — Qualified ↔ raw session ID mapping; `LocalSessionRouter` (wraps `NodeOperations`) + `TunneledSessionRouter` (wraps a remote node's tunnel).
- **`computer_use_agent.py`**, **`ui_tars_agent.py`**, **`ui_tars_actions.py`**, **`desktop_target.py`**, **`vlm.py`** — Computer-use VLM and its targets (VLM stays hub-side because the GPU is here).

### Frontend (`web/src/`)

React 19, TypeScript, Vite, Tailwind CSS 4, Zustand for state.

- **`api.ts`** — REST client
- **`ws.ts`** — WebSocket client (NDJSON)
- **`store.ts`** — Zustand state management
- **`webrtc.ts`** — WebRTC connection handler
- **`components/`** — UI components (terminal via xterm.js, file tree via react-arborist, code view via CodeMirror)

### Communication Flow

```
                                    ┌──────── Hub host (Python/aiohttp, port 3456) ────────┐
                                    │                                                       │
 Browser tab A ─ WS NDJSON ─────────▶  ┌──────────────────────┐                            │
   (clientId, tabId, activeNode=Hermes)│ Hub WsBridge (proxy) │                            │
                                    │  │ HubBrowserBridge     │   ┌─ Self-node subprocess ─┐
 Browser tab B ─ WS NDJSON ─────────▶  │ (per-client active   │   │ vibr8_node --self-mode │
   (same clientId, different tabId,    │  node map; per-tab   │   │ NodeOperations         │
   activeNode=local)                   │  active node lookup  │   │ WsBridge (real state)  │
                                    │  └────────┬─────────────┘   │ CliLauncher → CLIs     │
                                    │           │ tunnel NDJSON   │ Ring0Manager           │
                                    │           │ (loopback) ────▶│ env/artifacts/git/fs   │
                                    │           │                 │ scheduler              │
                                    │           │                 └────────────────────────┘
                                    │           │ tunnel NDJSON       (owns ~/.vibr8/)
                                    │           │ (wss://) ────────▶ ┌─ Remote node (Hermes,
                                    │           │                    │  Docker, EC2, …)
                                    │           │                    │ vibr8_node
                                    │  ┌────────▼─────────────┐      │ NodeOperations
                                    │  │ WebRTC peers keyed   │      │ CliLauncher → CLIs
 Browser tab A ─ WebRTC ─◀──────────▶─│ by peer_key =        │      │ Ring0Manager
 Browser tab B ─ WebRTC ─◀──────────▶─│ clientId#tabId       │      │ ...
                                    │  │ STT/TTS per peer     │      └─────────────────────┘
                                    │  │ Ring0EventForwarder  │
                                    │  │  → event.source_     │
                                    │  │    client_id →       │
                                    │  │    active node       │
                                    │  └──────────────────────┘
                                    └───────────────────────────────────────────────────────┘
```

Three things to notice:

1. **The hub owns no node-scoped state.** Sessions, Ring0, FS, git, envs, artifacts, scheduler all live on a node. The hub spawns its own `vibr8_node` subprocess at startup (the **self-node**) and reaches it via a loopback tunnel; remote nodes (Hermes, Docker, EC2, …) use the same NDJSON-over-WebSocket protocol over the public internet. Session IDs are qualified at the hub boundary as `{node_id}:{raw_id}` so the browser sees one flat namespace.

2. **The active node is per-client, per-tab.** Each browser tab has its own `activeNodeId` (sessionStorage) and POSTs it to `/api/clients/{client_id}/active-node`. Voice routing reads the originating client's active node; Ring0 events route via `event.source_client_id`. There is no hub-wide active node.

3. **WebRTC peers are per-tab.** Per-peer state (PC, STT, outgoing track, video, screen capture, input injector) is keyed by `peer_key = "{client_id}#{tab_id}"`. User-level state (guard, tts_muted, voice modes, speaker gates, usernames) stays keyed by `client_id`. Two tabs of the same browser can run independent voice connections.

Set `VIBR8_DISABLE_SELF_NODE=1` to fall back to the legacy in-process path (no subprocess; hub directly owns session state).

The WebSocket protocol is reverse-engineered and documented in `WEBSOCKET_PROTOCOL_REVERSED.md`. The node parity refactor is in `docs/remote-node-parity.md` (plan) and `docs/remote-node-parity-handoff.md` (recovery guide).

### Nodes (hub-host's self-node + remote nodes)

Every vibr8 instance is a "node" — including the hub-host. The hub spawns its own `vibr8_node` subprocess (the **self-node**) and treats it like any other remote node from the routing perspective. Hermes-style remote nodes (Docker containers, EC2 instances, etc.) connect the same way.

**Communication**: Nodes connect to the hub via WebSocket — outbound from the node to `wss://{hub_url}/ws/node/{node_id}?apiKey=...`. Internet-traversable, no SSH required. The hub never initiates connections to nodes. Protocol is NDJSON over WebSocket with request/response correlation (`server/node_tunnel.py`).

**Session creation**: Every `create_session` (regardless of target) flows through `local_node_ops.launch_with_options()` → tunnel → target node's `NodeOperations.launch_with_options` → that node's `CliLauncher`. The host never spawns CLIs for non-self sessions. Session IDs are qualified at the hub boundary (`{node_id}:{raw_id}`) by `QualifyingNodeClient` so browser/CLI WebSockets can route via the tunnel.

**Computer-use sessions**: VLM inference always runs on the hub (GPU lives there). The `nodeId` determines which remote desktop to target for WebRTC screen capture and input injection. The remote node is a "dumb terminal" — captures screen frames and injects input events.

**Ring0 on nodes**: Each node runs its own Ring0 instance locally, auto-launched on startup if enabled. The hub-host's self-node runs Ring0 just like any other node.

**Active node (per-client)**: Each browser client (and each browser tab — `tabId` in sessionStorage) picks its own active node. The frontend persists it in `sessionStorage["vibr8_active_node_id"]` and POSTs to `/api/clients/{client_id}/active-node` whenever it changes. The hub holds the map in `HubBrowserBridge._client_active_nodes`. There is no hub-wide active node.

**Voice routing**: Voice transcripts route to the originating WebRTC client's per-client active node. `server/webrtc.py` resolves the target via `HubBrowserBridge.get_client_active_node(client_id)` (falls back to the self-node). Remote-node targets go via that node's tunnel; the self-node goes through `local_node_ops.ring0_input`.

**Hub-side events**: `note_mode_ended`, `second_screen_*` etc. are emitted on the hub and forwarded to the source client's active node's Ring0. Callers populate `Ring0Event.source_client_id`; the forwarder in `server/main.py` calls `HubBrowserBridge.get_client_active_node(source_client_id)` and routes via `local_node_ops.emit_ring0_event` (self-node) or the remote tunnel directly. Events without a source client fall back to the self-node. Session-bound events (`user_returned`, `task_completed`, scheduler `task_due`) fire on the *node-side* WsBridge and reach that node's own Ring0 without going through the forwarder.

**Key files**: `server/main.py` (self-node spawn + swap), `vibr8_core/node_client.py` (SwappableNodeClient + QualifyingNodeClient), `vibr8_core/node_operations.py` (canonical per-node ops), `vibr8_node/node_agent.py`, `server/node_tunnel.py`, `server/node_registry.py`, `install-node.sh`, `Dockerfile.node*`

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

### Pen System (`server/ws_bridge.py`)

Per-session ownership control that prevents Ring0 and the user from stepping on each other. Each session has `controlled_by: "ring0" | "user"` (default: `"ring0"`).

**Taking the pen**: When a user sends a message directly from the browser UI (identified by `source_client_id`), the session's pen automatically transfers to the user. This is implicit — no explicit "take pen" action needed.

**What the pen suppresses**: When the user holds the pen for a session, **both** Ring0 message sends **and** Ring0 event notifications for that session are suppressed. This is by design — the user is actively working in that session and doesn't want Ring0 interfering or reacting to state changes.

**Auto-release**: The pen returns to Ring0 after 5 minutes of idle (no new user messages). The timer resets on each user message and when the session goes idle after finishing work.

**Explicit release**: Ring0 or the frontend can release the pen via `POST /api/sessions/{id}/pen` with `controlledBy: "ring0"`.

**Key invariant**: The pen check in `_notify_ring0_state_change` must stay — removing it causes Ring0 to react to sessions the user is directly controlling, which creates interference.

## Voice Commands (`server/webrtc.py`)

Guard word **"vibr8"** (or **"vibrate"**) triggers commands only when followed by a known keyword. If no command matches, the entire transcript passes through unmodified (guard word included). When a command matches, any pre-text before the guard word is submitted as input first.

**Commands:** `done`, `off`, `guard`, `listen`, `quiet`, `speak`, `ring zero on`, `ring zero off`, `note`, `node {name}`
**Escape sequences (also commands):** `vibr8 vibrate ...` → `vibrate ...`, `vibr8 app ...` → `vibr8 ...`

**Guard mode:** When enabled (default), discards transcripts without the guard word. Guard word presence is checked independently of command matching.

**Note mode** (`vibr8 note`): Accumulates speech silently, mutes Ring0 TTS. Only `vibr8 done` exits. On exit, submits `[voice note]` and sends `[note_mode ended]` to Ring0. Pre-text before "vibr8 done" is added as a final fragment.

**Node switching** (`vibr8 node {name}`): Switches **this voice client's** active node for Ring0 routing. Updates `HubBrowserBridge._client_active_nodes[client_id]` and broadcasts `ring0_switch_node` to that client so its UI flips. Other browser clients/tabs are unaffected. Matches node name from the registry.

**Ring0 routing:** When Ring0 is enabled, all voice input routes to Ring0 instead of the active session. The target node is the originating client's per-client active node. If that's a remote node, voice transcripts are forwarded via its `ring0_input` tunnel command; if it's the self-node, they go via `local_node_ops.ring0_input`. Falls back to the self-node if the per-client choice is unset or the chosen remote is offline.

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
