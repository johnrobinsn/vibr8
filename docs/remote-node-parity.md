# Remote Node Parity — Architecture & Implementation Plan

**Status**: Approved, in progress
**Author**: discussion 2026-05-20
**Goal**: Make remote nodes first-class equivalents of the hub-host. All features available on `neo` (the hub-host) must be available on every remote node, scoped appropriately.

## Architectural Decision: Full Loopback

The hub becomes a thin **registry + bridge** that owns no node-scoped state. It spawns its own `vibr8_node` subprocess that connects back to itself on 127.0.0.1 via the existing tunnel protocol. Every node-scoped operation — including operations on the hub-host's own sessions — goes through the tunnel. There is one implementation of every node operation, located in `vibr8_node`, period.

### Why full loopback

- **Zero hub/remote drift possible** — same code runs everywhere.
- **Every feature works on every node by construction**, not by maintenance discipline.
- **Pen, events, FS, git, envs, artifacts, second-screens, Ring0 control, scheduler** become trivial to expose per-node — they *are* per-node.
- **The hub binary is small** — registry + tunnel hub + WebRTC + auth. Easy to reason about.
- **Future-proof**: adding a node implementation in another language only requires implementing the tunnel protocol.

### Accepted costs

- Loopback JSON overhead for local CLI I/O (verify it stays smooth, especially terminal output).
- Startup ordering complexity (hub waits for self-node before answering node queries).
- One-shot data migration on first deploy (`~/.vibr8/*` → `~/.vibr8-node/self/*`).
- Subprocess respawn semantics (matches what we'd want for remote nodes anyway).

## What Lives Where

### Hub process (`server/`)

- aiohttp HTTP/WS server
- Node registry + tunnel server endpoint
- Browser WebSocket relay (every browser message → tunnel → owning node)
- WebRTC pipeline: microphone, STT, TTS, audio track, peer connection (hub is the user-I/O endpoint)
- Voice command interpreter (NoteMode, guard word) — hub-side state, effects dispatched to active node
- Computer-use VLM inference (GPU lives at hub)
- Auth & user accounts
- Self-node spawner

**Hub no longer has**: `WsBridge` instance, env manager, second-screen manager, artifact store, scheduler, Ring0 manager, session persistence. All gone from the hub process. Hub stores only the node registry, auth state, and live WebRTC peer connections.

### Node process (`vibr8_node/` + relocated shared module)

- `WsBridge` — sessions, pen, events (relocated from `server/`)
- `Ring0Manager`, `Ring0EventRouter`, `Ring0Scheduler` (relocated)
- Per-node managers: env, artifact, second-screen, worktree tracker
- `CliLauncher`
- Session persistence (`~/.vibr8-node/{node-id}/sessions/`)
- `NodeOperations` class — single canonical implementation of all node-scoped methods
- Tunnel client + generic command dispatcher: `getattr(ops, cmd)(**payload)`

## What Stays Hub-Only (Genuine Asymmetries)

The hub is the user's I/O endpoint. These remain hub-only modules:

- WebRTC peer connections to the browser; audio track injection (TTS playback, microphone capture).
- STT pipeline.
- Voice command interpretation (NoteMode, guard word) — hub-side state machine; *effects* dispatched to active node via tunnel.
- Computer-use VLM — hub-side because that's where the GPU is. Remote nodes are "dumb terminals" providing screen capture + input injection.
- Node registry itself — only the hub has the master registry.

## Cross-Node Flows (post-refactor)

```
voice transcript: hub STT → active_node.ring0_input(text)
voice note done: hub NoteMode → active_node.ring0_input(text) + active_node.emit_ring0_event(note_mode_ended)
remote Ring0 TTS: node.speak(text) tunnel cmd → hub TTS → browser WebRTC
computer-use: hub VLM → active_node.send_desktop_input(...) tunnel cmd → node injects
browser keystroke: browser WS → hub relay → tunnel → owning node WsBridge → CLI subprocess
CLI output: node WsBridge → tunnel → hub relay → browser WS
```

## Phased Execution

### Phase 0 — Extract shared node module (1 day)

Move `WsBridge`, `Ring0*`, env/artifact/second-screen/scheduler managers from `server/` into a shared package. Both hub and node import from there. **No behavior changes**; tests still pass. Mechanical relocate.

### Phase 1 — `NodeOperations` + generic tunnel dispatch (1 day)

Define `NodeOperations` class wrapping the moved managers. Surface every node-scoped operation as one method. Refactor `vibr8_node/node_agent.py` `_cmd_*` handlers into a single generic dispatcher: `await getattr(self.ops, cmd)(**payload)`. Hub still calls into in-process managers; `NodeOperations` is defined and used on the node side first.

### Phase 2 — `NodeClient` interface + RemoteNodeClient method-per-op (1 day)

Define `NodeClient` protocol — one method per `NodeOperations` method. Implement `RemoteNodeClient`: each method serializes to a tunnel command. `LocalNodeClient` is a **temporary** in-process wrapper around the hub's existing managers (deleted in Phase 4). Migrate `routes.py` to use `NodeClient` throughout: `node = node_clients.get(node_id); return await node.fs_read(path)`. No more `_forward_to_remote()` branches.

### Phase 3 — Add all missing tunnel commands (1–2 days)

With the dispatcher in place, adding tunnel commands is one-line each: add a method to `NodeOperations`. Land the long list — fs, git, envs, ring0 control, artifacts, second-screens, worktrees, scheduler, ring0_event, speak. Test against the existing Hermes node.

### Phase 4 — Self-node spawn + delete `LocalNodeClient` (1–2 days) [KEYSTONE]

- Hub spawns `python -m vibr8_node --hub ws://127.0.0.1:3456 --name self --self-mode` at startup
- `--self-mode` tells the node to use the hub-host's data dir (post-migration) and register with a well-known ID (e.g. `self` or hostname)
- Auth: hub generates one-time service token, passes via env var
- Replace `LocalNodeClient` with `RemoteNodeClient` pointed at the loopback
- Delete `LocalNodeClient`
- Delete hub's in-process `WsBridge`/Ring0/manager instantiations
- Data migrator runs once on hub startup to relocate `~/.vibr8/*` → `~/.vibr8-node/self/*`
- After this: hub has zero node-scoped state. Every existing hub-local feature flows through loopback.
- Verify perf: terminal output, CLI input, state-change events.

### Phase 5 — Browser WebSocket relay unified (½ day)

Today the hub's browser WS handler has two paths: hub-local session (direct WsBridge call) vs remote session (tunnel forward). Remove the hub-local path. Every browser message forwards through tunnel to the owning node — including the self-node. Deletes the special case.

### Phase 6 — Hub-side I/O bridging to active node (1 day)

- STT transcript → `node_clients[active_node].ring0_input(text)`
- `vibr8 note` / `done`: NoteMode state stays on hub; on `done`, hub calls `active_node.ring0_input(text)` + `active_node.emit_ring0_event(note_mode_ended)`
- Remote Ring0 TTS: node sends `speak` tunnel command → hub TTS → WebRTC out
- Computer-use: hub VLM → `node.send_desktop_input(...)` via tunnel; node returns screen frames via WebRTC data channel (existing path)

### Phase 7 — Frontend node-scoping (1–2 days)

Every panel acquires a `nodeId` from a node selector or session context. Hub appears in the node list as the self-node. Pen indicator, FS browser, git, envs, artifacts, second-screens, scheduler, Ring0 settings — all per-node and node-aware. No frontend code assumes "hub == implicit context".

### Phase 8 — Cleanup & docs (½ day)

- Delete dead code paths in `server/`
- Update `CLAUDE.md`, `README.md`, `WEBSOCKET_PROTOCOL_REVERSED.md`
- Add architecture diagram
- Add node-startup ordering test (hub waits gracefully when self-node isn't yet up)

## Migration Concerns

1. **Bootstrapping**: hub starts → opens tunnel listener → spawns self-node → self-node connects → registers as `self` → only now can the hub answer "list sessions". UI shows "Initializing local node…" until handshake completes (usually <1s).
2. **Self-node restart**: hub monitors subprocess; on crash, exponential-backoff respawn. Browser sessions appear "disconnected" during the gap, same as remote-node offline behavior.
3. **Data migration**: existing `~/.vibr8/sessions/`, `~/.vibr8/artifacts.json`, `~/.vibr8/second-screens.json`, `~/.vibr8/envs/`, `~/.vibr8/ring0/`, `~/.vibr8/worktrees.json` move into self-node's data dir. A one-shot versioned migrator on first boot relocates them.
4. **Perf**: every browser keystroke and every byte of CLI output now traverses loopback WebSocket + JSON. Verify smoothness, especially terminal output.
5. **Single binary** stays. Hub spawns a child process; no deployment changes.

## First PR

Phases 0–2 together, with no behavioral change. Foundation: `NodeOperations` defined, `NodeClient` interface in use, `routes.py` migrated to call the interface. Everything still in-process behind the scenes — but the seams are in the right place, so Phase 4 (the keystone) becomes a contained change.

## Progress Log

### 2026-05-20 / 2026-05-21 — initial implementation pass

Landed (commits 85b020c..a9f06d0):

- **Phase 0** — vibr8_core/ relocate of 15 modules (ws_bridge, ring0*, cli_launcher, session_*, env_manager, artifacts, worktree_tracker, codex/hermes/opencode adapters). Plus git_utils in 3b.
- **Phase 1** — `NodeOperations` class + generic `getattr(self._ops, cmd)(**payload)` dispatcher in vibr8_node/node_agent.py. All 17 existing `_cmd_*` handlers collapsed.
- **Phase 2** — `NodeClient` protocol, `LocalNodeClient` (= `NodeOperations`), `RemoteNodeClient` (tunnel via `__getattr__` generic dispatch). `resolve_node_client()` helper. Session-CRUD routes (rename/kill/relaunch/delete/archive/unarchive) migrated; `_forward_to_remote()` deleted.
- **Phase 3a** — Per-node FS tunnel commands (list/tree/read/write/mkdir/rename/delete/home). `/api/fs/*` accepts `?nodeId=`.
- **Phase 3b** — Per-node git tunnel commands (repo-info/branches/worktrees/fetch/pull/create_worktree/delete_worktree). `/api/git/*` accepts `?nodeId=`.
- **Phase 3c** — Per-node Ring0 control (toggle/switch-backend/switch-model/mute-events/status). Hub UI can drive any node's Ring0.
- **Phase 3d** — Per-node env tunnel commands (list/get/create/update/delete).
- **Phase 3e** — Pen indicator visible in sidebar for remote sessions (controlledBy flows through list_sessions); pen release via UI works for remote sessions (POST /api/sessions/{id}/pen routes through NodeClient).
- **Phase 3f** — Per-node artifacts (list/create/delete/read_content). Artifact bytes base64-encoded over the tunnel.

Tests stayed green at 230 passing throughout. Remote forwarding requires the remote node to be running the new code; nodes started before commit 85b020c need a restart to pick up the new tunnel commands.

### Deferred (originally part of Phase 3, moved to later phases)

- **Per-node scheduler** (`/api/ring0/tasks/*`): each node needs its own `TaskScheduler` instance. Niche use case; defer to "Phase 6+" with the broader hub-event-forwarding design.
- **Per-node second-screens**: pairing flow is tightly coupled to hub-side WebRTC + browser UI. Today the node's existing `hub_proxy` middleware in `vibr8_node/node_agent.py` already forwards `/api/second-screen/*` to the hub, so remote-node Ring0 can use the hub's screens. Per-node screens needs a UX redesign; revisit only if a concrete use case appears.
- **ring0_event forwarding** (hub → active node's Ring0) and **speak** (active node → hub TTS): both belong with Phase 6 (hub-side I/O bridging) where the "active node" routing concept gets first-class treatment.

### Pending phases (unchanged)

- **Phase 4** [keystone]: spawn self-node subprocess, delete `LocalNodeClient`, data migration.
- **Phase 5**: unify browser WebSocket relay through tunnel.
- **Phase 6**: hub-side I/O bridging — STT routing to active node, NoteMode integration, TTS-text forwarding from active node, ring0_event forwarding.
- **Phase 7**: frontend node-scoping (the UI doesn't yet pass `nodeId` for fs/git/envs/etc., so the new backend tunnel commands aren't user-visible yet).
- **Phase 8**: cleanup + docs.

## Open Questions / Decisions Deferred

- Self-node ID: `self` literal vs hostname-based. (Probably `self` for simplicity; hostname collides if multiple hubs run on same host.)
- Self-node working directory: separate `~/.vibr8-node/self/` vs reuse `~/.vibr8/`. (Separate, for clean per-node semantics.)
- Tunnel protocol versioning: bump version on Phase 3 (new commands)?
- Frontend: do we surface "self" as a node in the UI, or hide it behind "Local"? (Probably show it explicitly — first-class means first-class.)
