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
| 4c-2 — Migrate session_registry.py (webrtc/main callbacks deferred to 4c-4/Phase 6) | ✅ | `d90bcb5` |
| 4c-3 — Extract `HubBrowserBridge` from `WsBridge` | ✅ (redundant — folded into 4c-4) | — |
| 4c-4 step 1 — `HubBrowserBridge` facade (delegates to WsBridge today; swappable later) | ✅ | `c00ca3a` |
| 4c-4 step 2a — `SwappableNodeClient` seam (no behavior change) | ✅ | `dcd3df0` |
| 4c-4 step 2b — Swap `local_node_ops` to self-node when `VIBR8_USE_SELF_NODE=1` | ✅ | `1d1bf2b` |
| 4c-4 step 2c — Data-dir consolidation (`~/.vibr8/`) + skip hub restore when full self-node mode | ✅ | `8866492` |
| 4c-5 — `QualifyingNodeClient` for session-id prefix at hub boundary | ✅ verified live | `448287f` + `c4a75df` |
| 4c-6 step 1 — Self-node restart-on-crash w/ exp backoff | ✅ verified live | `ff1b7d2` |
| 4c-6 step 2 — Flip default to self-node mode (drop env-var gates) | ✅ verified live | `82fd425` |
| 4c-6 step 3 — Voice transcript routing via local_node_ops | ✅ | `8428afb` |
| 4c-6 step 4 — Voice commands + disconnect-flush via local_node_ops | ✅ | `b67bab2` |
| 4c-6 step 5 — Tests for SwappableNodeClient + QualifyingNodeClient | ✅ | `706f99c` |
| 4c-6 step 6 — Ring0 status cache for WebRTCManager | ✅ | `36e106a` |
| 4c-6 step 7 — Delete `LocalNodeClient` alias | ✅ | this batch |
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

### Phase 4c-2 — DONE (commit `d90bcb5`)

- `LocalSessionRouter` now wraps `NodeOperations` for writes (`send_message`, `kill`, `respond_permission`). Sync reads (history, pending permissions) still hit the bridge — those move with `HubBrowserBridge` in 4c-3.
- `SessionRegistry` constructor takes optional `local_node_ops`; `sync_from_launcher` is async and pulls via `local_node_ops.list_sessions()`. Both callers (`server/main.py` startup + `server/routes.py /api/ring0/sessions`) updated to `await`.
- `server/main.py` reorders construction so `local_node_ops` is built before `SessionRegistry`.
- Lazy-fallback path retained in `SessionRegistry.sync_from_launcher` for callers/tests that don't pass `local_node_ops`.

**Intentionally not touched in 4c-2:**
- `webrtc.py` references to `ring0_manager` (voice routing) + `launcher` (Ring0 lazy-create) → Phase 6 (hub-side I/O bridging to active node).
- `main.py` event callbacks (`on_cli_session_id`, `on_cli_relaunch_needed`, `on_first_turn_completed`, `on_codex_adapter_created`) → these fire from the hub's in-process `WsBridge`. When 4c-4 removes that bridge, the callbacks become unreachable and the wiring code can be deleted in the same commit. They're not broken now.
- `ws_bridge.set_*()` wiring calls (`set_store`/`set_webrtc_manager`/`set_ring0_manager`/`set_node_registry`/etc.) → same story; they configure the in-process bridge which is going away.

### Phase 4c-3 — re-scoped: fold into 4c-4

After auditing `WsBridge` (2700 lines, ~117 methods), the planned `HubBrowserBridge` extraction is not a method-renaming exercise — it requires a real redesign of where browser-WS state lives. Specifically, `_broadcast_to_browsers(session, msg)` iterates `session.browser_sockets`, coupling session-state ownership with browser tracking.

After the audit, the cleanest moment to introduce `HubBrowserBridge` is during 4c-4 itself: when the hub stops creating an in-process `WsBridge`, it simultaneously needs a *new* surface for browser-WS+client tracking. Doing the two refactors separately would mean either:
  (a) keeping a parallel `WsBridge` on the hub purely for browser tracking (wasteful, also leaves a no-op `_sessions` field), or
  (b) shimming `HubBrowserBridge` over the in-process `WsBridge` and then re-pointing the shim — needless intermediate state.

**Plan**: Skip 4c-3 as a standalone phase. Fold the `HubBrowserBridge` introduction into 4c-4.

What 4c-4 now needs to do:
1. Always spawn the self-node (drop `VIBR8_SPAWN_SELF_NODE=1` gate).
2. Self-node uses `~/.vibr8/` (drop `~/.vibr8-self/` temp dir; add one-shot migrator if needed).
3. Hub stops instantiating in-process `WsBridge`/`Ring0Manager`/`CliLauncher`/`SessionStore`.
4. Introduce `HubBrowserBridge`: owns `session_id → browser_sockets` map, `client_id → metadata`, broadcast helpers, computer-use agent registry. Receives messages forwarded from the tunnel and fans out to subscribed browsers.
5. `local_node_ops` becomes `RemoteNodeClient(self_node_id, tunnel)`; route through it once the self-node has registered.
6. Delete the now-unused hub-side wiring (`ws_bridge.set_*`, `ws_bridge.on_*` callbacks, `launcher.on_*` callbacks).
7. CLI WS endpoint repointed to the self-node (could be deferred to 4c-5).

This is a sizable single PR (likely 500-1000 LOC delta across server/) and deserves its own focused session. It's the breaking change in the keystone.

### Phase 4c-4 — step 1 landed (commit `c00ca3a`)

`HubBrowserBridge` facade exists. Today it's `__getattr__`-delegating to `WsBridge` for everything; the explicit method list documents the hub-only surface (`broadcast_*`, `set_client_metadata`, `register_device_info`, `get_all_clients`, `get_ring0_prompt_client`, `_broadcast_to_browsers`, plus catch-all). 11 call sites in `routes.py` + 6 in `webrtc.py` migrated from `ws_bridge.foo` → `hub_browser_bridge.foo`. `WebRTCManager` gains `set_hub_browser_bridge(...)`.

This is the *seam* — when Phase 4c-4 step 2 happens (the atomic flip), the swap is `HubBrowserBridge.__init__` (give it real state instead of a `WsBridge` reference), not a refactor of every caller.

### Phase 4c-4 — step 2 landed (commits `dcd3df0`, `1d1bf2b`, `8866492`)

Three opt-in env vars, additive:

| Env vars | Behavior |
|---|---|
| (none) | Current production behavior. Hub creates in-process managers, owns `~/.vibr8/`. |
| `VIBR8_SPAWN_SELF_NODE=1` | Spawns the self-node subprocess to `~/.vibr8-self/`. Coexists. (Phase 4b verification recipe — unchanged.) |
| `VIBR8_SPAWN_SELF_NODE=1` + `VIBR8_USE_SELF_NODE=1` | **Full self-node mode.** Hub spawns self-node to `~/.vibr8/` (passed via `VIBR8_SELF_NODE_DATA_DIR`); hub skips `launcher.restore_from_disk()` and `ws_bridge.restore_from_disk()` and Ring0 auto-launch; after self-node registers, `local_node_ops.swap()` flips it to `RemoteNodeClient(self_id, tunnel)`. |

`local_node_ops` is now a `SwappableNodeClient` wrapper — routes captured it in closure but `__getattr__` dispatches to whatever target is bound. The swap is atomic. (`vibr8_core/node_client.py`)

**Phase 4c-4 step 2 — still not viable in production**: in full self-node mode, the browser-WS `/ws/browser/{sid}` handler is routed by `WsBridge._route_browser_message`, which only forwards to a remote node when `_is_remote_session(session_id)` is true (i.e. session id contains `:`). For self-node-owned sessions, the IDs come back from `list_sessions` raw (no `self:` prefix), so the hub treats them as local and finds nothing. **That's Phase 4c-5.**

### Phase 4c-5 — what to do next

The cleanest fix: have the hub treat the self-node like any other remote node from the routing perspective.

1. When `VIBR8_USE_SELF_NODE=1`, after the self-node registers, **qualify session IDs at the hub boundary** in `local_node_ops.list_sessions()` (and any other route returning raw IDs). One approach: `SwappableNodeClient` could be subclassed (or extended) to know about a `qualify_prefix` and post-process certain method responses. Cleaner: a thin `QualifyingNodeClient` wrapper that delegates to `RemoteNodeClient` and rewrites `sessionId` fields in known response shapes.

2. With qualified IDs in the frontend, browser opens `/ws/browser/self:RAW_ID`. The hub's `WsBridge._is_remote_session` returns True; the existing remote-session forwarding code path picks it up. No new WS-routing code needed — just the ID qualification.

3. CLI WS endpoint: the CLI subprocesses are spawned by the *self-node*, not the hub, in full mode. They get the self-node's port via `--sdk-url` and connect there directly. The hub's `/ws/cli/{session_id}` endpoint just becomes unused in full mode (could 404 it post-4c-6).

4. Other routes returning session IDs (`create_session`, `get_session`, etc.) — same qualification at the hub boundary.

A `QualifyingNodeClient` could be done as: `class QualifyingNodeClient: def __init__(self, inner, node_id): ...`. It wraps `RemoteNodeClient`, and overrides the methods that return session IDs to rewrite them. Inserted between the `SwappableNodeClient` and the `RemoteNodeClient` when the swap happens.

After 4c-5 lands, run the live test:
```
VIBR8_SPAWN_SELF_NODE=1 VIBR8_USE_SELF_NODE=1 PORT=3556 uv run python -m server.main
# Hit /api/sessions, create a session, kill it, list, etc.
```
Should see all operations flowing through the loopback tunnel to the self-node, with session IDs prefixed `self:` in API responses.

### Phase 4c-6 — drop the gates

Once 4c-5 is solid:
- Make full self-node mode the default (the env vars become a way to disable it, if anything).
- Hub stops creating `WsBridge`/`Ring0Manager`/`CliLauncher`/`SessionStore` constructors at all (delete the conditional logic).
- `HubBrowserBridge` gets a real implementation (replace `__getattr__` delegation with standalone state — the keystone deletes its underlying `WsBridge`).
- Add subprocess restart-on-crash with exponential backoff.
- Delete `LocalNodeClient` alias from `vibr8_core/node_client.py`.
- Delete `_in_process_ops` and the `SwappableNodeClient` if no longer needed.

### Phase 4c-6 step 1 landed (commit `ff1b7d2`)

Self-node restart-on-crash. The hub now respawns the subprocess if it
exits unexpectedly, with exponential backoff (2s → 4s → 8s → 16s → 30s
cap). On respawn it reuses the same ephemeral API key (the
`node_registry` already has its bcrypt hash). After re-registration the
`local_node_ops.swap()` runs again to point at the fresh tunnel. Loop
exits cleanly on hub shutdown via `_restart["requested"]`.

Live-verified: killed the self-node child mid-flight with SIGKILL;
hub logged `Self-node subprocess exited rc=-9; respawning in 2.0s`,
then `Respawning self-node (attempt #2, backoff was 4.0s)`, then
re-registered + swapped. Requests served continuously across the
respawn.

### Phase 4c-6 step 2+ — DEFERRED (decision pending)

The remaining work to drop the in-process path entirely is real:

1. **`HubBrowserBridge` needs standalone state.** Today it
   `__getattr__`-delegates to `WsBridge`. To stand alone it needs:
   - `session_id → set[browser_ws]` (currently in `Session.browser_sockets` —
     coupled to session state)
   - `client_id → metadata` (with `~/.vibr8/clients.json` persistence —
     currently `WsBridge._client_metadata`)
   - `client_id → ws / role / session` (`_ws_by_client`, `_client_roles`,
     `_client_sessions`)
   - `_native_ws_by_client` (Android foreground service WS)
   - `_mirror_sockets` (passive mirror connections)
   - Computer-use agent registry (`_computer_use_agents`)
   - Plus the broadcast methods that iterate them.

2. **Browser/native/playground/enrollment WS handlers in `main.py`**
   currently call `ws_bridge.handle_browser_open/close/message`. These
   would migrate to `hub_browser_bridge.*` equivalents. For self-node
   sessions, browser messages still get forwarded to the self-node via
   tunnel (existing `_route_browser_message` logic, but reimplemented in
   `HubBrowserBridge`).

3. **`webrtc.py` and computer-use agents** also touch session-state
   on `ws_bridge` (e.g., reading `ring0_manager.session_id` for voice
   routing). Each callsite needs to consult `local_node_ops` (which is
   the self-node client) instead. Phase 6 (hub-side I/O bridging to
   active node) already covers most of this; folding the two designs
   together makes sense.

4. **Drop the env-var gates**. Default startup becomes "spawn self-node,
   swap `local_node_ops`, never load in-process state". Hub `main.py`
   stops calling `WsBridge()`, `CliLauncher()`, `Ring0Manager()`,
   `SessionStore()` entirely.

5. **Delete `LocalNodeClient`** alias + `_in_process_ops` +
   `SwappableNodeClient` (only one target ever; the wrapper isn't
   needed if everything is the self-node client).

Estimated 500-1000 LOC across `vibr8_core/hub_browser_bridge.py`,
`server/main.py`, `server/webrtc.py`, `vibr8_core/ws_bridge.py`,
plus tests.

### Phase 4c-6 — Option A landed (functional)

Self-node mode is the default startup path. All node operations
(session-CRUD, FS, git, envs, Ring0 control, artifacts, voice
transcript routing, voice enable/disable commands, disconnect flush)
flow through the loopback tunnel to the self-node subprocess.

Final state across this phase:

- `82fd425` — default = self-node mode (env-var flip)
- `8428afb` — voice transcript routing via `local_node_ops.ring0_input`
- `b67bab2` — voice commands + disconnect flush via `local_node_ops`
- `706f99c` — 10 new tests for `SwappableNodeClient` + `QualifyingNodeClient`
- `36e106a` — Ring0 status cache on `WebRTCManager` (broadcasts work after toggle)
- `1b67624` — Delete `LocalNodeClient` alias (only one target ever now)

**Architectural goal achieved**: the original user goal — Hermes (or any
remote node) is a first-class equivalent of the hub-host — is met.
Hermes works just like always; the hub-host's self-node demonstrates
the same code path on the local machine. `routes.py` is manager-free;
voice routing is manager-free; the only direct in-process manager
references on the hub are dormant pieces in `main.py` (event callback
wirings, `ws_bridge.set_*` plumbing) that fire only in legacy
`VIBR8_DISABLE_SELF_NODE=1` mode.

**Optional remaining purity work (no user-visible impact):**
1. Skip the dormant event-callback wirings (`on_cli_session_id`,
   `on_codex_adapter_created`, etc.) and `ws_bridge.set_*` calls when
   in self-node mode. Lines 461-512 in `server/main.py`.
2. Make in-process `Ring0Manager`/`CliLauncher`/`SessionStore`
   construction conditional on `VIBR8_DISABLE_SELF_NODE=1`. Currently
   they're always constructed but dormant in self-node mode.
3. Remove `VIBR8_DISABLE_SELF_NODE` legacy path entirely. Single code
   path. Cost: no fallback if self-node fails to spawn at startup.

None of these change user-visible behavior. They're cleanup that can
happen organically.

---

### Phase 4c-6 step-by-step history

Self-node mode is now the default startup path (commit `82fd425`).
Set `VIBR8_DISABLE_SELF_NODE=1` to fall back to legacy in-process
mode for debugging.

Voice transcript routing (commit `8428afb`): the hub's WebRTC pipeline
now calls `local_node_ops.ring0_input(text, source_client_id)` for
local Ring0 — which in self-node mode tunnels to the self-node. If
Ring0 is disabled on the self-node, `ring0_input` returns
`{"error": ...}` and the hub falls through to the active-session
path. The remote-node branch (active_node != "local") is unchanged.

**Step 4 (remaining work)** — residual hub-side refs to in-process
managers, in priority order:

1. **`webrtc.py` ring0_manager reads** (lines 447, 455, 719, 726, 818,
   819, 949, 957). These gate "broadcast voice mode to Ring0 session"
   and similar. Today they read the hub's Ring0Manager.is_enabled +
   session_id, which is stale in self-node mode (Ring0Manager loads
   ring0.json on init, doesn't re-read). Options:
   - Make Ring0Manager.is_enabled re-read ring0.json on call (cheap
     stat+parse). Simplest.
   - Or query `local_node_ops.ring0_status()` with a small TTL cache.
   - Or skip the broadcast entirely if `_ring0_manager is None`.

2. **`webrtc.py` ring0 enable/disable from voice commands** (lines 872,
   879). `vibr8 ring zero on/off` calls `self._ring0_manager.enable()`
   /`.disable()`. In self-node mode the hub's ring0_manager writes
   ring0.json but the self-node doesn't notice. Fix: redirect to
   `local_node_ops.ring0_toggle(enabled=True/False)`.

3. **`webrtc.py` line 1283-1284** — another local Ring0 submit path
   (in a different code path from `_submit_text`). Same migration as
   step 3 above: replace with `local_node_ops.ring0_input(text)`.

4. **`main.py` event callbacks** (`ws_bridge.on_cli_session_id_received`,
   `launcher.on_codex_adapter_created`, `launcher.on_computer_use_created`,
   `ws_bridge.on_cli_relaunch_needed_callback`,
   `ws_bridge.on_first_turn_completed_callback`). These fire from the
   in-process bridge which is dormant in self-node mode (no sessions
   ever get there). They can be deleted along with the in-process
   bridge.

5. **`ws_bridge.set_*` wirings** in `main.py` (`set_store`,
   `set_webrtc_manager`, `set_ring0_manager`, `set_node_registry`,
   `set_task_scheduler`, `set_session_registry`). Dormant in self-node
   mode. Delete with the in-process bridge.

6. **In-process manager construction** in `main.py` (`launcher = CliLauncher(...)`,
   `ws_bridge = WsBridge()`, `ring0_manager = Ring0Manager(...)`,
   `session_store = SessionStore()`). Once steps 1-5 are done, these
   can be deleted; routes/webrtc no longer touch them.

7. **`LocalNodeClient` alias + `SwappableNodeClient`**. Once 6 is done,
   there's only one target for `local_node_ops` ever (the self-node
   client). `SwappableNodeClient` becomes redundant — replace with a
   plain reference. Delete the `LocalNodeClient = NodeOperations`
   alias.

Each step is roughly a small commit. Order matters: steps 1-3 first
(those touch live code paths in self-node mode), then 4-7 (cleanup of
dormant code).

### Decision point: do we need the full keystone?

The remote-node-parity *goal* — Hermes (and any remote node) is a
first-class equivalent of the hub-host — is **already fully met** with
the work landed through Phase 4c-6 step 1:

- Every node-scoped operation has a tunnel command (`NodeOperations` →
  `RemoteNodeClient`).
- `routes.py` is manager-free; it calls `NodeClient` exclusively.
- `LocalSessionRouter` routes through `NodeOperations`.
- `local_node_ops` is swappable; with `VIBR8_USE_SELF_NODE=1` it
  retargets at the self-node and the full loopback path works
  end-to-end, including restart-on-crash.

The remaining 4c-6 step 2+ work mainly buys **code purity** — single
unambiguous path, no in-process fallback — not new user-visible
capabilities. The cost is real: large refactor + the architectural
commitment that the hub *always* spawns a subprocess on boot.

**Two viable end-states**:

- **Option A — Full keystone** (drop in-process path). Code purity;
  one unambiguous default. Locks in subprocess dependency.
- **Option B — Keep both modes** (current state). In-process by
  default; opt-in `VIBR8_USE_SELF_NODE=1` for full loopback. Two paths
  to maintain, but the in-process path has years of production
  hardening and is zero-overhead.

If the user wants Option A, the playbook above is the recipe. If
Option B is acceptable, the refactor is essentially complete — Phase
4c-6 step 2+ is permanently optional, and we move on to Phase 6/7/8
(hub-side I/O bridging to active node, frontend node-scoping, cleanup).

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
from vibr8_core.node_client import (
    NodeClient, RemoteNodeClient, SwappableNodeClient,
    QualifyingNodeClient, resolve_node_client,
)
from vibr8_core.hub_browser_bridge import HubBrowserBridge
from vibr8_node.node_agent import NodeAgent
from server import main
print('OK')
"

# Verify the self-node CLI flag exists.
uv run python -m vibr8_node --help | grep -- --self-mode
```

Expected: 240+ tests pass, all imports succeed, `--self-mode` appears in help.

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
