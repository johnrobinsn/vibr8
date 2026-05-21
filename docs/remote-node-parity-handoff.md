# Remote Node Parity — Handoff / Recovery Guide

**Purpose**: bootstrap a new Claude (or human) session that needs to pick up the remote-node-parity refactor without access to the prior conversation. Read this *and* `docs/remote-node-parity.md` before doing anything.

**Audience assumption**: you have the repo at `/mntc/code/vibr8` (or equivalent), but the running vibr8 hub may be down or stale. Don't trust prior memory; verify everything against `git log` and the repo on disk.

---

## TL;DR — Where We Are

The user wants remote nodes (e.g. the "Hermes" node) to be first-class equivalents of the hub-host ("neo"). The agreed architecture is **full loopback**: the hub becomes a thin registry + bridge that spawns its own `vibr8_node` subprocess and talks to itself over the same tunnel protocol used by remote nodes. The hub owns no node-scoped state.

**Refactor phases (per `docs/remote-node-parity.md`):**

| Phase | Status | Last commit |
|---|---|---|
| 0 — relocate node-scoped modules to `vibr8_core/` | ✅ | `85b020c` |
| 1 — `NodeOperations` + generic tunnel dispatch | ✅ | `ead00a6` |
| 2 — `NodeClient` interface + session-CRUD migration | ✅ | `983212b` |
| 3a — Per-node FS commands | ✅ | `a798e5e` |
| 3b — Per-node git commands | ✅ | `dbb785b` |
| 3c — Per-node Ring0 control | ✅ | `e0439c8` |
| 3d — Per-node env commands | ✅ | `b75ff87` |
| 3e — Pen visibility + release for remote sessions | ✅ | `80d3d56` |
| 3f — Per-node artifacts (second-screens deferred) | ✅ | `a9f06d0` |
| 4a — `--self-mode` flag on `vibr8_node` | ✅ | `e192292` |
| 4b — Hub-side self-node spawn machinery (gated) | ✅ verified live | `3ac81aa` |
| 4c-1 — Migrate remaining session-state routes through NodeClient | ✅ (`routes.py` has zero direct launcher/store/worktree refs) | `9c825e3` |
| 4c-2 — Migrate webrtc.py, session_registry.py, main.py callbacks | ⏳ NEXT | — |
| 4c-3 — Extract `HubBrowserBridge` from `WsBridge` | ⏳ | — |
| **4c-4 — Atomic flip** (spawn self-node by default, drop in-process managers, data dir consolidation) | ⏳ KEYSTONE | — |
| 4c-5 — Unify browser+CLI WS relays through tunnel | ⏳ | — |
| 4c-6 — Restart-on-crash; delete `LocalNodeClient` | ⏳ | — |
| 6 — Hub-side I/O bridging (STT/NoteMode/TTS to active node; `ring0_event` and `speak` tunnel commands) | ⏳ | — |
| 6b — Per-node scheduler (deferred from 3g) | ⏳ | — |
| 7 — Frontend node-scoping | ⏳ | — |
| 8 — Cleanup + docs | ⏳ | — |

### Phase 4c-1 — what's done vs deferred

**Done** (batches 1+2 in `5464068` + `aafd902`):
- `GET /api/sessions/{id}` — via `client.get_session()`
- `GET /api/sessions/{id}/history-archive` (+ `/dates`) — via `client.get_session_archive()` / `list_session_archive_dates()`
- `POST /api/ring0/respond-permission` — fallback path via `client.respond_to_permission()`
- `POST /api/ring0/interrupt` — fallback path via `client.interrupt()`
- `POST /api/ring0/send-message` — fallback path via `client.submit_message()`
- `POST /api/ring0/rename-session` — via `client.rename_session()`
- `POST /api/ring0/set-session-mode` — via `client.set_permission_mode()`
- `GET /api/ring0/get-session-mode` — via `client.get_session()`
- `GET /api/ring0/session-output` — fallback via `get_message_history()` + `get_pending_permissions()`
- `POST /api/ring0/switch-ui` — session resolution via NodeClient (hub broadcast stays)
- `POST /api/sessions/{session_id}/upload` — fixed pre-existing `bridge` NameError
- Deleted legacy `_resolve_session_id` helper from `routes.py`
- Added `NodeOperations`: `get_session`, `get_message_history`, `get_pending_permissions`, `get_pending_permission_count`, `respond_to_permission`, `get_session_archive`, `list_session_archive_dates`, `set_archived`

**Batch 3 (now done — commits `196d570`..`9c825e3`):**
- 3a (`196d570`): `list_nodes` session count, `ring0_sessions` fallback both pull via `local_node_ops.list_sessions()`.
- 3b (`ebed2c1`): `_cleanup_worktree` moved into `NodeOperations` as a private helper; `delete_session` / `archive_session` invoke it internally; routes simplified to drop duplicate `launcher.kill`/`launcher.remove_session`/`ws_bridge.close_session` calls.
- 3c (`c71320f`): `GET /api/sessions` aggregator — all three branches (Android-node, remote-node, local) now pull via `local_node_ops.list_sessions()`; extended `NodeOps.list_sessions` to also enrich `agentState`.
- 3d (`9c825e3`): `create_session` for both `POST /api/sessions` (all backends) and `POST /api/ring0/create-session` use new `NodeOperations.launch_with_options(opts, backend_type, name, worktree_mapping)`. Env resolution + worktree creation still happen on the hub (orchestration) and the resolved bits are handed off through `launch_with_options`.

**Outcome**: `server/routes.py` has **zero** direct references to `launcher`, `session_store`, or `worktree_tracker`. Remaining `ws_bridge.*` references in `routes.py` are all hub-only (proxy session lookup for remote-prefixed ids, `close_session` on hub proxy state, broadcasts, client metadata) — those move with the `HubBrowserBridge` split in Phase 4c-3.

**Hub-only references to ws_bridge** that stay (move to HubBrowserBridge in 4c-3, not 4c-1):
- `ws_bridge.broadcast_name_update`, `broadcast_ring0_switch_ui`, `_broadcast_to_browsers`
- `ws_bridge.get_ring0_prompt_client`, `set_client_metadata`, `get_all_clients`
- `ws_bridge._build_client_list`, `_client_sessions`, `register_device_info`
- `ws_bridge.register_computer_use_agent`, `attach_adapter`, `get_or_create_session`

Verify the table against `git log --oneline main` — commits should be on `main` in the order above, after `7ff55c3`.

---

## Architecture in one paragraph

A **node** (hub or remote) is a self-contained vibr8 instance: its own `WsBridge`, `Ring0Manager`, `CliLauncher`, `SessionStore`, env/artifact/etc managers. The **hub** plays an additional role beyond being a node: it's the **registry** for nodes, the **browser WebSocket relay**, the **WebRTC I/O endpoint** (microphone, TTS playback, computer-use VLM). All node-scoped operations go through `NodeClient`. `NodeOperations` (`vibr8_core/node_operations.py`) is the canonical implementation; `LocalNodeClient` is currently `= NodeOperations` bound to in-process managers; `RemoteNodeClient` (`vibr8_core/node_client.py`) wraps the tunnel via `__getattr__`-based generic dispatch. After Phase 4c, the hub stops creating in-process managers — `LocalNodeClient` is deleted and the hub talks to its own embedded `vibr8_node` subprocess over a loopback tunnel.

---

## Key Files & Patterns

### Where things live

```
vibr8_core/                         # shared package — both hub and node import
├── node_operations.py              # NodeOperations — single source of truth per op
├── node_client.py                  # NodeClient protocol, RemoteNodeClient, resolve_node_client
├── ws_bridge.py                    # session/pen/event router (per-node)
├── ring0.py                        # Ring0Manager (per-node)
├── ring0_events.py                 # Ring0EventRouter (per-node)
├── ring0_scheduler.py              # TaskScheduler
├── cli_launcher.py                 # spawns CLI subprocesses
├── session_store.py                # session persistence
├── session_types.py                # wire TypedDicts (camelCase)
├── session_names.py                # naming utilities
├── env_manager.py                  # ~/.vibr8/envs/
├── artifacts.py                    # ~/.vibr8/artifacts/
├── worktree_tracker.py             # worktree mappings
├── git_utils.py                    # git operations
└── {codex,hermes,opencode}_adapter.py  # backend ACP/JSON-RPC adapters

server/                              # hub-only modules
├── main.py                          # entry point; instantiates managers + spawns self-node
├── routes.py                        # REST handlers; uses NodeClient via resolve_node_client
├── node_registry.py                 # node registry (hub-only)
├── node_tunnel.py                   # tunnel server (hub side)
├── webrtc.py, stt.py, tts.py        # voice/audio I/O (hub-only)
├── auth.py                          # auth (hub-only)
├── computer_use_agent.py, ui_tars_*  # VLM-based agents (hub-only)
└── ...                              # other hub-only modules

vibr8_node/                          # remote node process (and, post-4c, also hub's self-node)
├── __main__.py                      # CLI entry (argparse + spawn NodeAgent)
└── node_agent.py                    # NodeAgent — connects to hub, dispatches tunnel commands
                                     # via getattr(self._ops, cmd)(**payload_to_kwargs(msg))

docs/
├── remote-node-parity.md            # canonical plan doc + progress log
└── remote-node-parity-handoff.md    # this file
```

### Adding a new tunnel command (the Phase 3 pattern)

1. Add an `async def my_op(self, ...) -> dict:` method to `NodeOperations` in `vibr8_core/node_operations.py`. Use **snake_case** kwargs; return a dict (never raise — return `{"error": "..."}`).
2. (Optional) Add the method to the `NodeClient` Protocol in `vibr8_core/node_client.py` for typing.
3. In `server/routes.py`, change the REST handler to:
   ```python
   node_id = body.get("nodeId", "")  # or request.query.get("nodeId", "")
   try:
       client, _ = _resolve_node_client(node_id)
   except Exception as e:
       return web.json_response({"error": str(e)}, status=503)
   result = await client.my_op(arg1=..., arg2=...)
   if "error" in result:
       return web.json_response(result, status=_status_for_error(result["error"]))
   return web.json_response(result)
   ```
4. The tunnel command **name = method name**. The dispatch on the node side is generic via `getattr(self._ops, cmd_type)(**payload_to_kwargs(msg))` — no code change needed.
5. Wire formatting: snake_case in Python ↔ camelCase on the wire. `payload_to_kwargs` and `RemoteNodeClient.__getattr__` handle the translation automatically.

### Session-prefixed IDs

Remote session IDs from the hub's perspective look like `{node_id}:{raw_session_id}`. Use `ws_bridge.get_session_node_id(sid)`, `ws_bridge._is_remote_session(sid)`, `ws_bridge._raw_session_id(sid)`, and `WsBridge.qualify_session_id(node_id, raw)` to handle them. `resolve_node_client(sid, ...)` (in `vibr8_core/node_client.py`) does all of this and returns `(client, raw_sid, is_remote)`.

---

## How to Verify the Refactor's Current State

Run all of these before making changes; they're a tripwire if any prior commit broke something.

```bash
# All tests should pass (1 unrelated test_video_track failure is pre-existing).
uv run pytest server/tests/ --ignore=server/tests/test_video_track.py

# Imports should resolve cleanly with no side effects in the import path.
uv run python -c "
from vibr8_core.node_operations import NodeOperations
from vibr8_core.node_client import NodeClient, RemoteNodeClient, LocalNodeClient, resolve_node_client
from vibr8_node.node_agent import NodeAgent
from server import main
print('OK')
"

# Verify the self-node CLI flag exists.
uv run python -m vibr8_node --help | grep -- --self-mode
```

Expected: 230 tests pass, all imports succeed, `--self-mode` appears in help.

---

## How to Pick Up Where We Left Off (Phase 4c)

**Goal**: flip the hub from using in-process `LocalNodeClient` to using `RemoteNodeClient` against the self-node subprocess. After Phase 4c the hub holds **zero** node-scoped state.

### Phase 4c — audit summary (2026-05-21)

Total in-process manager references across `server/` (excluding `vibr8_core/`, `vibr8_node/`):

| Bucket | Count | Disposition |
|---|---|---|
| **SESSION-STATE** | ~38 | Migrate to NodeClient or remove |
| **HUB-ONLY** | ~22 | Keep on hub (browser WS tracking, client metadata, broadcasts, WebRTC, auth, registry) |
| **AMBIGUOUS / MIXED** | ~8 | Split (WsBridge split is the biggest single piece) |

Key realizations:

1. **`WsBridge` has two concerns** that must be separated:
   - **Session state** (sessions dict, pen, permissions, message history, CLI session id tracking, ring0_event router) → moves to the self-node.
   - **Hub-only browser/client tracking** (client metadata, Ring0 prompt-client tracking, browser broadcasts, computer-use agent registration, name-update broadcasts, switch-ui broadcasts) → stays on the hub.
   The cleanest path is to extract a `HubBrowserBridge` (or similar) for the hub-only half, and let the existing `WsBridge` keep being the session-state half (which then only lives in the node).

2. **`session_registry.py` already exists** with `LocalSessionRouter` and `TunneledSessionRouter` abstractions parallel to the newer `NodeClient` pattern. There's some duplication. Phase 4c should consolidate — keep `SessionRegistry` for hub-side ID-prefix mapping (qualified ↔ raw) and have routers/clients delegate to `NodeClient`/`NodeOperations`.

3. **Routes still using in-process managers** (not yet migrated through `NodeClient`): `create_session`, the big `list_sessions` aggregator, `get_session`, all `/api/ring0/*` message-flow routes (`send-message`, `switch-ui`, `session-output`, `respond-permission`, `interrupt`), `session-history-archive`, the session-`upload`, and the `_resolve_session_id` legacy helper.

4. **`webrtc.py` uses `launcher.set_launcher()`** to look up the active Ring0 session id. That coupling must move to a node-registry-aware lookup, or webrtc gets a thin client/proxy reference.

### Phase 4c sub-phases (revised, safer staging)

The all-at-once "big bang" is too big for one commit. Stage like this:

**4c-1** — Migrate the remaining session-state route handlers to call `NodeClient` (still backed by in-process `local_node_ops`; no behavior change). Add any missing `NodeOperations` methods: `submit_message`, `respond_permission`, `interrupt`, `get_message_history`, `get_pending_permissions`, `get_session_archive`, `track_worktree`. The route layer becomes manager-reference-free; `routes.py` only knows about `NodeClient`.

**4c-2** — Audit & migrate `webrtc.py`, `session_registry.py`, `main.py` event callbacks. Replace `launcher.*`/`ws_bridge._sessions[...]`/`ring0_manager.session_id` reads with `NodeClient` reads (or, where the lookup is genuinely cross-cutting, `node_registry`-based). Browser-broadcast calls (`ws_bridge.broadcast_*`, `set_client_metadata`, etc.) get carved out into a new `HubBrowserBridge`.

**4c-3** — Extract `HubBrowserBridge` from `WsBridge`. Hub creates one (always). Routes/voice/webrtc call into it for browser concerns. `WsBridge` is now strictly session-state.

**4c-4** — Spawn the self-node by default (drop `VIBR8_SPAWN_SELF_NODE` gate). Self-node uses `~/.vibr8/` (the hub's data dir). Hub stops instantiating `WsBridge`/`Ring0Manager`/`CliLauncher`/`SessionStore`. `local_node_ops` becomes `RemoteNodeClient(self_id, tunnel)`. One-shot migrator copies `~/.vibr8-self/` → `~/.vibr8/` if needed.

**4c-5** — Migrate `/ws/browser/{id}` and `/ws/cli/{id}` to relay through the tunnel for all sessions (including self-node).

**4c-6** — Restart-on-crash for the self-node subprocess (exponential backoff). Delete `LocalNodeClient` alias from `vibr8_core/node_client.py`.

Each sub-phase is small enough to commit and verify independently. 4c-1 and 4c-2 are no-behavior-change refactors; 4c-3 is a real restructuring but still behavior-preserving; 4c-4 is the actual atomic flip; 4c-5/6 are cleanup that depends on 4c-4 being live.

### Phase 4c — original sub-steps (kept for reference)

1. **Wire `local_node_ops` to be the self-node client instead of in-process** when the self-node has registered.
   - In `server/main.py`, after `[server] Self-node registered: id=…` fires in `_spawn_self_node`, replace `local_node_ops` (or the `_resolve_node_client`/`_resolve_client` lookups) so an empty `nodeId` resolves to `RemoteNodeClient(self_id, self_node.tunnel)`.
   - Until the self-node is registered, requests still hit in-process. Once registered, they flip.
   - **Risk**: this is the moment the hub depends on the subprocess. If the subprocess dies, things break. Add restart-on-crash logic (exponential backoff).

2. **Stop instantiating in-process managers in `main.py`.**
   - Delete the in-process `WsBridge`, `Ring0Manager`, `CliLauncher`, `SessionStore` constructors at lines ~386-407 in `server/main.py`.
   - Routes that still touch these directly (search for `launcher.`, `ws_bridge.`, `ring0_manager.` in `routes.py`) need to either be migrated through `NodeClient` or accept that they no longer work.
   - **Trap**: hub-only code that uses `ws_bridge` for things like browser broadcasts, WebRTC orchestration, computer-use agents — those still need a reference. Keep ws_bridge for *those* concerns but stop using it for *session* state (the self-node has that).

3. **Migrate the browser WebSocket relay** (`/ws/browser/{session_id}` in `main.py`).
   - Today there's a "hub-local session" path (direct ws_bridge call) and a "remote session" path (tunnel forward). Remove the hub-local path entirely; *every* browser message goes through the tunnel to the owning node, including self-node.
   - Symmetric for the CLI WebSocket (`/ws/cli/{session_id}`): post-4c, CLIs spawned by the self-node connect to the *self-node's* port (default 3459), not the hub's. The hub's `/ws/cli` endpoint can be removed (or kept as a 404).

4. **Delete `LocalNodeClient`** alias in `vibr8_core/node_client.py`. Verify no code references it.

5. **Data dir**: today self-mode uses `~/.vibr8-self/` (Phase 4a/b decision to avoid coexistence corruption). Now that in-process managers are gone, change self-mode to use `~/.vibr8/` (the hub's existing data dir) and write a one-shot migrator that copies from `~/.vibr8-self/` if it has data and `~/.vibr8/` doesn't. See `vibr8_node/node_agent.py:run()`.

6. **Always-on self-node**: remove the `VIBR8_SPAWN_SELF_NODE` env-var gate. The hub now requires the self-node to function.

7. **Restart-on-crash**: monitor the subprocess; on exit, respawn with exponential backoff. The hub should never silently lose its self-node.

8. **Run live verification**:
   - Restart the hub. Verify it spawns the self-node, the self-node registers, and basic operations (list sessions, create session, kill session) work through the UI.
   - Test against the existing **Hermes** remote node — it should still work, since the tunnel protocol didn't change.

### Phase 4c risks (read these first)

- **Atomic change**: every "hub had `WsBridge` in-process" assumption breaks at once. Greater chance of subtle bugs than any previous phase.
- **Voice/WebRTC**: `server/webrtc.py` references `ws_bridge.*` to find Ring0 session id, last prompted at, etc. Those calls need to route to `local_node_ops` (which is now the self-node client). Same for `stt.py` if it touches the bridge.
- **Hub-side broadcasts**: `ws_bridge.broadcast_to_all_browsers` is used by `routes.py` (artifacts_changed event, etc.). Whose browsers? The hub's. So keep a "broadcast-only" hub-side `WsBridge` for browser tracking, even though session state moves to the self-node. Or: have the self-node broadcast via tunnel back to the hub which fans out.
- **`server/session_registry.py`** unifies local + remote sessions for the routes layer. After 4c, "local" = self-node, so this might collapse to just the registry-of-remotes pattern.
- **`server/routes.py` non-migrated handlers**: search for `launcher\.|ws_bridge\.|ring0_manager\.` — anywhere that's a direct call to in-process state will break.

### Verification of Phase 4b (already passed, for reference)

The Phase 4b smoke test (run on 2026-05-21):

```bash
VIBR8_SPAWN_SELF_NODE=1 PORT=3556 uv run python -m server.main > /tmp/test-hub.log 2>&1 &
# Then poll: grep -E "Self-node registered|self-node did not register" /tmp/test-hub.log
# Expected lines (in order):
#   [server] Spawning self-node: port=3459 hub=wss://127.0.0.1:3556
#   [server] Self-node registered: id=<8 hex chars>
#   [self-node] Registered as node self (id=<same id>)
#   [self-node] Local server running on http://127.0.0.1:3459
#   [self-node] Tunnel connected
```

This leaves a stale `self` node entry in `~/.vibr8/nodes.json` — clean it up after:
```python
import json, pathlib
p = pathlib.Path.home() / ".vibr8" / "nodes.json"
data = json.loads(p.read_text())
data["nodes"] = {nid: n for nid, n in data["nodes"].items() if n.get("name") != "self"}
data["api_keys"] = {kid: k for kid, k in data.get("api_keys", {}).items()
                    if not k.get("name", "").startswith("self-node bootstrap")}
p.write_text(json.dumps(data, indent=2))
```

**Caveat**: `pkill -TERM -f "python -m server.main"` is too broad if the user has `make dev` running — it kills that hub too. Use the PID returned by the background command instead, or grep for `PORT=3556` explicitly.

---

## Operational Context You Need

- **User's main hub** runs via `make dev` (loop in `Makefile`) at `PORT=3456`, behind nginx + autossh tunnel exposed at `https://vibr8.ringzero.ai`. Don't kill it; restart-cost is real (heavy ML models reload).
- **Hermes node** (a real remote node, not self-node) lives in `~/.vibr8/nodes.json` and is normally launched via:
  ```bash
  nohup uv run python -m vibr8_node --hub wss://localhost:3456 \
    --api-key sk-node-a1869e15225c3b6e811fe1ad80aa02e999fb7b04417fe1f2 \
    --name Hermes --port 3458 --default-backend hermes \
    > /tmp/hermes-node.log 2>&1 &
  ```
  Use it as the remote-side test target.
- **Data dirs**:
  - Hub: `~/.vibr8/` (sessions, ring0, envs, artifacts, nodes.json, etc.)
  - Remote nodes: `~/.vibr8-node/{name-slug}/` (isolated)
  - Self-node (Phase 4a/b): `~/.vibr8-self/` (will consolidate to `~/.vibr8/` in 4c)
- **Tests**: `uv run pytest server/tests/ --ignore=server/tests/test_video_track.py` — 230 passing. `test_video_track` is a pre-existing failure unrelated to this work.
- **CLAUDE.md** in the repo root has further conventions (camelCase wire / snake_case Python, logging prefixes, async-by-default, `uv` for Python, `bun` for frontend).

---

## What NOT To Do

- **Don't `pkill -f "server.main"`** without scoping to a port. Use `lsof -ti:3556 | xargs kill` instead when killing a test hub.
- **Don't run `--self-mode` against a live hub on `~/.vibr8/`** — until Phase 4c, the self-node and the hub's in-process managers share the data dir and will conflict. The flag uses `~/.vibr8-self/` for now to prevent this.
- **Don't migrate hub-only routes** (auth, node-registry, WebRTC, voice, computer-use VLM) to use `NodeClient`. They genuinely belong on the hub.
- **Don't trust this document blindly** — verify status with `git log --oneline` and `git diff main..HEAD` for any local changes. Phase numbers and commit hashes might have moved.
- **Don't generate URLs** for tutorials or external docs — you're working from the plan doc and the code, period.

---

## If You're Truly Lost

1. Read `docs/remote-node-parity.md` (the canonical plan).
2. Read `CLAUDE.md` in the repo root.
3. Run `git log --oneline 7ff55c3..HEAD` to see what's been done.
4. Run the verification commands in this doc.
5. The user (johnrobinsn@gmail.com) can be reached via the `/email` skill (`/mntc/code/agentmail/`) if needed — but only for genuine blockers.
