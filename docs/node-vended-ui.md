# Node-Vended UI: Architecture Direction and Plan

Status: **proposed** — exploratory direction captured 2026-06-12; no implementation yet.

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

**v2 parking lot:** `notify`/`present` for watch + second screen (second
screen becomes "render a node-vended URL", unifying with the iframe model),
`vlm.infer` service, per-node origins, custom non-vibr8 node UIs, read-only
fleet status feed.

## Risks / things to validate early

- **WS + asset transfer through the NDJSON tunnel**: chunking and
  backpressure for `web/dist` assets; cache headers so assets transfer once.
- **iframe storage**: sessionStorage/localStorage work under the same-origin
  path-prefix approach; revisit when moving to per-node origins (third-party
  storage partitioning).
- **Contract creep**: every future feature request must answer "node-land or
  contract?" — default node-land. The spec doc is the gate.
- **Going dark on native push** between Phases 3–4 (accepted; v2 restores).
