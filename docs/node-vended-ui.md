# Node-Vended UI: Architecture Direction and Plan

Status: **Phases 0–3 implemented and verified** (2026-06-12, this branch);
Phase 4 staged behind preconditions listed at the bottom.

- Phase 0 — `docs/hub-node-contract-v1.md` written; nodes announce
  `protocolVersion` + `contract` flags at registration.
- Phase 1 — `server/node_ui_proxy.py` maps `/nodes/{id}/{ui,api,ws}/*`
  onto the node's loopback server via `http_request` / `ws_open` /
  `ws_data` / `ws_close` tunnel messages. Node serves `web/dist` at
  `/ui/` and accepts browser WebSockets locally.
- Phase 2 — frontend node mode (prefix-aware API/WS, shell-owned node
  switching and voice, postMessage bridge); `NodeShellFrame` renders any
  ui/v1 node's vended UI in the hub shell.
- Phase 3 — events/v1: `transcript` in; `speak`/`busy`/`attention` out;
  hub TTS sniffing disabled for contract nodes.
- Verified end-to-end on a live hub + self-node: vended index/assets/API
  over the tunnel, session created via `/nodes/local/api/`, browser WS
  round-trip through the channel proxy delivering `session_init`.

## Motivation

The hub↔node relationship today is "one app stretched across machines," not a
distributed system with a contract. Measured coupling surface:

- `NodeOperations` exposes ~80 tunnel-callable methods
- the bridge broadcasts ~39 distinct WS message types
- the frontend WS client switches on ~28 of them
- `store.ts` encodes deep node semantics (content-block merge rules,
  permission dedupe, pen state)

Hub and node only interoperate because they ship from the same repo at the
same SHA. A node deployed today will not work against the hub a year from
now. Compare ssh/tmux: a 2005 ssh client drives a 2026 server because the
contract is a byte pipe + vt100 — tiny, frozen, with all rendering
intelligence on the node side.

## Direction

Push rendering intelligence to the node; shrink the hub's *contract* (not
necessarily its code) to one page. Each node serves its own web UI, embedded
in the hub page as a sandboxed iframe, reached through an HTTP-over-tunnel
proxy. The node's UI and backend ship together from one commit, so version
skew between a node and its UI is eliminated **by construction**. The only
remaining skew surface is the minimal hub contract.

### The governing rule

The desktop-sharing layer proves the principle. Its contract is pixels up +
JSON input events down over WebRTC (VNC-shaped, frozen-by-nature); the hub
does signaling only; `vibr8_node/desktop_webrtc.py` was already deliberately
decoupled from hub internals. Result: the desktop viewer UI and the UI-TARS
agent live hub-side, work against every node, and never break.

> **Frozen contract → the feature can live anywhere (hub-side clients are
> fine, upgrade freely, work against old nodes).
> Evolving semantics → the feature must ship with the node (chat, sessions,
> permissions, Ring0 interaction → node-vended UI).**

The hub is not a pure router. It is **rendezvous + auth + a small set of
capability services** (STT/TTS today; VLM inference, GPU-bound, stays
hub-side with the agent loop layered on the frozen desktop contract).

## Decisions (locked 2026-06-12)

1. **Cross-node session features are dropped.** Each node's Ring0 and
   sessions are fully internal. No flat cross-node session namespace, no
   cross-node Ring0 messaging, no qualified-ID machinery. You switch worlds
   with `vibr8 node {name}`. Optional later: a read-only fleet status feed.
2. **Spec first, then variant B.** Write the one-page contract before
   implementing. The first conforming node vends the *existing* vibr8 web UI
   built at its own commit (variant B) — near-zero new UI work; per-node
   custom UIs become possible later, not required.
3. **Big-bang migration.** All nodes are operator-controlled; hub + nodes
   upgrade together on a branch. No dual-protocol compatibility window.
4. **Native clients (watch, second screen) deferred to v2.** v1 reserves
   `notify(title, body)` and `present(url, target)` message names but does
   not implement them; native push goes dark during the transition.

## Contract v1 (to be specified in `docs/hub-node-contract-v1.md`)

**Invariant: nodes are outbound-only.** A node behind NAT with zero open
ports must work forever. The node initiates its single WebSocket to the hub;
the hub never dials a node, and no contract message may carry a node address
for the hub (or a browser) to connect to directly. UI vending honors this by
design: `/nodes/{id}/ui|api|ws/*` requests are wrapped as tunnel messages
down the node's existing outbound connection and answered by the node's
loopback-bound local server. Desktop media uses ICE (STUN/TURN) with
signaling relayed over the tunnel, as today.

Three sides, all versioned, all additive-only after freeze:

### A. Plumbing (node → hub, outbound WS as today)
- register/auth (`wss://{hub}/ws/node/{id}?apiKey=...`), heartbeat, disconnect
- **capability + protocolVersion announcement at registration** (do this
  regardless of everything else)
- NDJSON tunnel framing with request/response correlation (unchanged)
- HTTP-over-tunnel forward proxy: hub maps `/{nodes}/{id}/ui/*`,
  `/nodes/{id}/api/*`, `/nodes/{id}/ws/*` onto the node's local server
- WebRTC signaling relay (existing desktop-offer forwarding, named and kept)

### B. Events (the voice/status contract — the whole thing)
- hub → node: `transcript(text, client_id)` (post guard-word processing)
- node → hub: `speak(text)`, `busy(bool)`, `attention(reason)`
- v2 reserved: `notify(title, body)`, `present(url, target_client)`

Guard words, note mode, node switching stay hub-side (they route *between*
nodes). The node UI never touches audio; typed and spoken input arrive the
same way.

### C. Hub services (capability provider role)
- STT/TTS implicit in the voice path
- `desktop/v1`: the pixels+input+clipboard WebRTC contract, declared as a
  node capability, **frozen** — the UI-TARS agent and the shell's desktop
  viewer are clients of it
- v2 reserved: `vlm.infer(image, prompt)` for node-side agents

### D. postMessage bridge (hub shell ↔ node iframe)
- `hello` handshake with version + capabilities
- `theme`, `focus`/`blur`, `voiceState`, `navigate`
- This is the UI-side contract; version it like the tunnel.

## What stays in the hub shell

Login/auth, node switcher, voice controls + pipeline (GPU), WebRTC peer
management, **desktop viewer**, **computer-use controls** (UI-TARS task
submission, AUTO/CONFIRM/GATED approvals, watch mode — these currently ride
WsBridge session machinery and must move to a small hub-local API), TURN/ICE
config, node registry.

## What this deletes (Phase 4)

Qualified `{node_id}:{raw_id}` session IDs and `QualifyingNodeClient`,
`session_registry` (both routers), hub-side `WsBridge` proxy mode, the
hub-proxy middleware's session-resolving routes, native-push forwarding,
per-session REST routes on the hub, frontend node-qualification logic.

## Phases

**Phase 0 — Contract spec.** Write `docs/hub-node-contract-v1.md` covering
A–D above with versioning rules (capabilities flags; additive-only; nothing
removed, ever). Acceptance: the spec fits on roughly one page per side and
names every message that will exist.

**Phase 1 — Hub shell + tunnel HTTP proxy.** Implement the forward proxy
(including WebSocket proxying over the tunnel) and the iframe host page with
node switcher. Auth handoff: hub mints a short-lived signed token into the
iframe URL; node validates it. v1 serves node UIs same-origin under the
path prefix (acceptable: nodes are operator-owned machines); per-node-origin
isolation is a v2 hardening item. Acceptance: a static page served by a
remote node renders inside the shell.

**Phase 2 — Variant B node.** The existing React app gains a "node mode":
relative base path, talks to its own node's `/api` + `/ws` (proxied), no
node switcher, hub-only pages stripped. Node serves its own `web/dist`.
postMessage bridge client added. The self-node is just another node — the
hub shell iframes it identically. Acceptance: full chat/session UX works
against a node through the iframe, with hub and node intentionally built
from different commits.

**Phase 3 — Voice rewire.** Voice transcripts route to the active node via
the contract `transcript` message; the node emits `speak`/`busy`/`attention`.
Remove session-granular Ring0 proxying. Acceptance: voice → Ring0 → TTS
round-trip on a remote node touches only contract messages.

**Phase 4 — Demolition (big bang).** Delete the legacy surface listed above;
hub and all nodes cut over together. Desktop viewer and computer-use keep
working unchanged throughout (they ride `desktop/v1`).

Phase 4 status: **executed.** What changed:

- **Shell owns voice and desktop.** `VoiceControls` (extracted from
  TopBar) and a `desktop/v1` viewer toggle live in the `NodeShellFrame`
  strip; audio never enters a node iframe.
- **Vended local is the production default.** The shell iframes the
  self-node at `/nodes/local/ui/`; `localStorage["vibr8-legacy-ui"]="1"`
  is the escape hatch. The Vite dev server keeps the legacy in-shell UI
  by default (the node vends the last *built* bundle, not the live dev
  bundle) and opts in via `vibr8-vended-local=1`.
- **Deleted:** `QualifyingNodeClient` and qualified `{node_id}:{raw_id}`
  ids (session ids are raw end to end), `server/session_registry.py`
  (both routers), hub-side `WsBridge` proxy mode
  (`handle_remote_session_message`, `update_remote_sessions`,
  `remove_remote_node_sessions`), the node→hub `session_message`
  broadcast hook (node-local browser sockets — i.e. the vended UI over
  the tunnel channels — receive broadcasts directly), native-push
  forwarding (native clients are dark until v2 `notify`/`present`), and
  the hub-proxy middleware's session-resolving Ring0 routes
  (`NodeOperations._expand_session_id` resolves Ring0's 8-char prefixes
  node-locally).
- **Known limitation of the escape hatch:** the hub-root legacy UI can
  list but not chat with self-node sessions (the hub-root session WS
  path only functions in legacy in-process mode,
  `VIBR8_DISABLE_SELF_NODE=1` + `VIBR8_ALLOW_LEGACY_IN_PROCESS=1`).
  The vended path is the supported experience.

**v2 parking lot:** `notify`/`present` for watch + second screen (second
screen becomes "render a node-vended URL", unifying with the iframe model),
`vlm.infer` service, per-node origins, custom non-vibr8 node UIs, read-only
fleet status feed.

## Demolition aftercare (hub→browser broadcasts to node sessions)

The Phase 4 demolition emptied the hub's session bridge, breaking any
hub-originated broadcast that targeted a node session by id. Audit + status:

- **Voice transcript preview** — *fixed.* The hub no longer broadcasts to a
  (dead) hub session id; it routes the interim/cleared preview to the
  speaking client's active node via `broadcast_voice_preview`
  (`WebRTCManager._send_voice_preview` → `local_node_ops`/tunnel →
  `NodeOperations.broadcast_voice_preview`), and the node fans it out to its
  own Ring0 session over the vended session WS. The node owns session
  resolution; audio stays hub-side; the frontend handler is unchanged. Same
  treatment in `voice_service_client.py`. Verified piecewise (tunnel
  dispatch + node broadcast by unit test; node→vended-browser delivery by
  the mirror smoke test); the live STT→preview hop needs audio + models to
  confirm end to end.
- **Computer-use status broadcasts** (`main.py`) — *not broken.* CU sessions
  are hub-native (the agent registers on the hub bridge, VLM is hub-side),
  so `send_to_browsers` still resolves.
- **Non-Ring0 voice-to-active-session** — *known gap, deferred.* When Ring0
  is disabled, voice used to submit to the client's current hub session via
  `_resolve_session()`; the hub no longer tracks a vended client's current
  session, so this path is dark. Ring0 is the supported voice target in the
  vended model; reviving arbitrary-session voice needs the hub to learn the
  iframe's active session (its own small piece of work).
- **Vestigial remote-session qualify paths** (`routes.py` `/api/sessions`
  with `?nodeId=`) — *removed.* The hub no longer tunnel-fetches or
  forward-creates a remote node's sessions (those are reached through the
  node's vended UI); `/api/sessions?nodeId=<remote>` returns `[]` and create
  errors with a pointer to the node's UI. The seeded `local` node (no
  tunnel) still falls through to local handling. `WsBridge.qualify_session_id`
  is deleted (its last callers were these paths); `parse_qualified_id`
  stays — still used defensively in `ws_bridge.py`.

## Risks / things to validate early

- **WS + asset transfer through the NDJSON tunnel**: chunking and
  backpressure for `web/dist` assets; cache headers so assets transfer once.
- **iframe storage**: sessionStorage/localStorage work under the same-origin
  path-prefix approach; revisit when moving to per-node origins (third-party
  storage partitioning).
- **Contract creep**: every future feature request must answer "node-land or
  contract?" — default node-land. The spec doc is the gate.
- **Going dark on native push** between Phases 3–4 (accepted; v2 restores).

## Phase 5 — Self-node spawn deletion

Status: **executed** on branch `host-as-node-dev`. With Phase 4 the hub
already routed every session-bearing call through a node, but it still
spawned its own `vibr8_node --self-mode` subprocess at boot and held a
swappable `local_node_ops` pointer at it. Phase 5 finishes the original
design paragraph's promise — *"It deletes: self-node spawn..."* — and
makes the hub truly stateless.

What changed:

- `server/main.py`: deleted `_spawn_self_node()`, `resolve_self_node_enabled()`,
  `run_legacy_session_startup_sync()`, the `_use_self_node` branching, the
  Ring0 status cache refresher, the legacy in-process restore_from_disk
  path, and the shutdown-time subprocess termination. `local_node_ops`
  stays `NOT_READY` for the life of the process; the Ring0 event forwarder
  drops events that have no routable active node instead of falling back
  to a hub-owned default.
- `vibr8_node/__main__.py` + `node_agent.py`: dropped `--self-mode` (and
  the well-known `VIBR8_SELF_NODE_DATA_DIR` plumbing). Every node — host
  included — resolves its data dir from `VIBR8_NODE_DATA_DIR`.
- `dev-launch.sh`: the dev hub starts with no implicit node; the script
  mints a bootstrap API key against the dev hub's `NodeRegistry`, then
  starts a separate `vibr8_node` (name `host`, port 4459) with its own
  isolated data dir under `~/.vibr8-dev-host-node/`.

Verified end-to-end on the dev hub: hub on 4456 with no self-node
subprocess, host node registered as `host` (id from registry), Ring0
auto-launched on the host node with mcp-config under the dev-isolated
data dir, vended UI rendered through `/nodes/{id}/ui/`.

Known follow-ups (not blockers for the Phase-5 demolition):

- `routes.py` still has a couple of `node_registry.get_node_by_name("self")`
  fallbacks for paths that historically defaulted to the self-node. They
  now resolve to nothing on a stateless hub. Either route them through the
  per-client active node or 410-Gone them.
- `NodeRegistry._ensure_local_node()` still creates an always-present
  placeholder entry named "local" with empty `api_key_hash`. Harmless
  because nothing tunnels into it, but it shows up in the node picker.
  Either delete the placeholder or rename it as a "no node connected"
  empty-state sentinel.
