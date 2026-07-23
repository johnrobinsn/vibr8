# Hub–Node Contract v1

Status: **v1 draft — frozen on merge**. After freeze, changes are additive
only: new message types, new optional fields, new capability flags. Nothing
is ever renamed or removed. Receivers MUST silently ignore unknown message
types and unknown fields.

**Invariant: nodes are outbound-only.** The node initiates every connection
(registration HTTP call, tunnel WebSocket). The hub never dials a node. No
message may carry a node address for the hub or a browser to connect to
directly. A node behind NAT with zero open ports is the design baseline.

Wire encoding everywhere: NDJSON — one JSON object per line, UTF-8, `\n`
terminated. Binary payloads are base64 strings in `*B64` fields.

---

## Design principles

The plumbing below is shaped by three principles. Read them before adding
a new route, message type, or capability flag — most "should this live on
the hub?" questions have already been answered here.

**1. Sessions are node-internal.** A session id is only meaningful on the
node that owns it. Any operation that names a specific session id
(`send_message`, `switch_ui`, `respond_permission`, `get_session_output`,
…) is a **node-scoped** operation: Ring0 lives on the node, the session
state lives on the node, and the vended iframe UI is served by the node.
The node's own `ws_bridge` pushes session events (including `ring0_switch_ui`)
down the iframe's session WS directly. Session-scoped work must never
traverse the hub route table or the shell — no exceptions.

**2. Shell contract is small and node-agnostic.** The shell knows about
nodes; it does not know about sessions. Its allowed responsibilities:

- Node selection: active-node dropdown, `/api/nodes/{id}/activate`,
  `ring0_switch_node` pushes over `/ws/hub-shell/{clientId}`.
- Voice pipeline: STT / TTS / guard word / voice modes (§B).
- Theme, focus, tab visibility via the §D postMessage bridge.
- Desktop viewer (frozen `desktop/v1`).

The shell must not learn session ids, session names, permission requests,
message content, or any other per-session state. If a proposed change
would put "sessionId" on a shell WS message, on a `/ws/hub-shell/*`
payload, on `/api/clients/*` state, or in a §D postMessage type — the
change belongs on the node side instead.

**3. `_HUB_ONLY_PREFIXES` shrinks over time, never grows.** The list in
`vibr8_node/node_agent.py` enumerates the routes the node's hub-proxy
middleware forwards to the hub. Every entry there is a hub-side
dependency the node can't service locally. Adding a session-scoped route
to that list is a **design smell** — it usually means the caller should
be talking to the node's own copy of the route (via the node's local
server) instead of round-tripping through the hub.

---

## A. Plumbing

### A1. Registration (HTTP, node → hub)

`POST {hub}/api/nodes/register`

```json
{
  "name": "hermes",
  "apiKey": "…",
  "capabilities": {
    "protocolVersion": 1,
    "contract": ["ui/v1", "events/v1", "desktop/v1"],
    "hostname": "…", "platform": "linux", "arch": "x86_64",
    "ring0Enabled": true, "sessionCount": 0,
    "defaultBackend": "claude", "version": "0.1.0"
  }
}
```

→ `{ "nodeId": "…", "serviceToken": "…" }`

`protocolVersion` (int) and `contract` (list of capability flags) are the
negotiation surface. A missing `protocolVersion` means a pre-contract node.
Flags in v1: `ui/v1` (node vends its own web UI, supports `http_request` /
`ws_*`), `events/v1` (node consumes `transcript` and emits
`speak`/`busy`/`attention` per §B — the hub must not synthesize TTS from
this node's session traffic), `desktop/v1` (screen share + input injection
per §C2). The hub exposes a node's flags to its own UI; features absent
from the flag list are simply not offered.

### A2. Tunnel (WebSocket, node → hub)

`wss://{hub}/ws/node/{nodeId}?apiKey=…` — one connection, NDJSON both ways,
auto-reconnect with backoff is the node's job.

**Correlated requests** (either direction): sender sets `requestId`
(opaque string, unique per sender); receiver answers
`{"type": "response", "requestId": …, "data": {…}}`. `data.error` (string)
signals failure. `requestId` is reserved for correlation — payloads must
never use it for anything else.

**Node → hub, fire-and-forget:**

| type | fields | meaning |
|---|---|---|
| `heartbeat` | `sessionCount`, `ring0Enabled`, `defaultBackend` | every 30 s |
| `speak` | `text`, `clientId?` | say this aloud (§B) |
| `busy` | `busy` (bool) | node Ring0 working/idle (§B) |
| `attention` | `reason`, `clientId?` | node wants the user (§B) |
| `title` | `text` | node-published UI title (e.g. current session name). Additive — hubs may ignore. |

**Hub → node, correlated:**

| type | fields | meaning |
|---|---|---|
| `http_request` | `method`, `path`, `query?`, `headers?`, `bodyB64?` | proxied browser HTTP request (§A3) |
| `webrtc_offer` | `clientId`, `sdp`, `sdpType`, `desktopRole`, `iceServers` | desktop/v1 signaling (§C2) |

**Hub → node, fire-and-forget:**

| type | fields | meaning |
|---|---|---|
| `transcript` | `text`, `clientId` | voice/typed input for this node's Ring0 (§B) |

**WS channel multiplexing (both directions, fire-and-forget):**

| type | fields | meaning |
|---|---|---|
| `ws_open` | `channelId`, `path`, `query?` | hub opens a logical WS to the node's local server |
| `ws_data` | `channelId`, `text?` \| `dataB64?` | one WS message |
| `ws_close` | `channelId`, `code?` | either side closes the channel |

`channelId` is hub-generated and opaque. The node answers `ws_open` with a
`ws_close` if the local endpoint refuses. After `ws_close` in either
direction the `channelId` is dead.

**v2 reserved names** (do not reuse): `notify`, `present`, `vlm_infer`.

### A3. UI vending (`ui/v1`)

The hub maps its URL space onto the node's loopback server through the
tunnel — never through a direct connection:

```
{hub}/nodes/{nodeId}/ui/{p}   → http_request  path=/ui/{p}
{hub}/nodes/{nodeId}/api/{p}  → http_request  path=/api/{p}
{hub}/nodes/{nodeId}/ws/{p}   → ws_open       path=/ws/{p}
```

`http_request` response `data`: `{ "status": int, "headers": {…},
"bodyB64": "…" }`. The hub passes through `Content-Type` and caching
headers; hop-by-hop headers are dropped. The node serves its built UI at
`/ui/` (SPA fallback to `index.html`).

Auth: the browser authenticates to the hub (cookie / bearer); the hub
authenticates to the node implicitly via the tunnel. Proxied requests are
already trusted when they reach the node — same trust model as every other
tunnel command. v1 serves all node UIs same-origin under the hub's origin;
per-node origins are a v2 hardening item.

## B. Events — the voice/status contract

The node UI never touches audio. Spoken and typed input arrive identically.

- **`transcript`** (hub → node): final guard-word-processed text routed to
  the *originating client's active node*. The node feeds it to its Ring0
  (or wherever it likes — hub doesn't care).
- **`speak`** (node → hub): the hub synthesizes TTS and plays it to
  `clientId` if given, else to every voice client whose active node is the
  sender.
- **`busy`** (node → hub): drives hub-side UI/voice affordances (e.g.
  thinking indicator). Best-effort, idempotent.
- **`attention`** (node → hub): the node wants the user's attention.
  Required field `reason` (free display-ready text). Optional additive
  fields: `severity` (`"info"`/`"warning"`/`"urgent"`), `contextKey`
  (dedup key so re-fires from the same node collapse), `expiresAt`
  (ISO-8601 when clients may clear a stale notification). Hubs relay
  the message to any subscribed native observer with the envelope
  documented in `docs/native-client-contract.md` §B2 (enriched with
  `nodeId` + `nodeName` for provenance).

Guard words, note mode, and `vibr8 node {name}` switching stay hub-side —
they route *between* nodes and are part of the shell, not the node.

## C. Hub services

### C1. STT / TTS
Implicit in §B. Nodes never receive audio and never produce it.

### C2. `desktop/v1` (frozen)
Signaling: the hub sends `webrtc_offer` over the tunnel; the node answers
`{sdp, sdpType}` in the response. Media: WebRTC with ICE (STUN/TURN per the
offered `iceServers`). Up: screen video track. Down: JSON input events on
the data channel (the existing `desktop_webrtc.py` event schema: mouse,
keyboard, scroll, clipboard). Consumers: the shell's desktop viewer and the
hub-side UI-TARS agent. This contract is frozen — hub-side clients may
evolve freely against it.

### C3. v2 reserved
`vlm_infer` (hub GPU inference for node-side agents).

## D. postMessage bridge (shell ↔ node iframe)

Every message: `{ "vibr8": 1, "type": …, … }`. Same additive-only rules.

| dir | type | fields | meaning |
|---|---|---|---|
| iframe → shell | `hello` | `protocolVersion`, `capabilities` | iframe is alive |
| shell → iframe | `hello_ack` | `protocolVersion`, `theme`, `voiceState` | shell config snapshot |
| shell → iframe | `theme` | `value` (`"dark"`/`"light"`) | theme changed |
| shell → iframe | `voice_state` | `enabled`, `ring0` | voice pipeline state |
| shell → iframe | `focus` / `blur` | — | tab visibility |
| iframe → shell | `title` | `text` | suggested tab title |

The shell MUST function with an iframe that never sends `hello`
(non-vibr8 UIs are legal); the iframe MUST function without `hello_ack`
(standalone/local development).

---

## Versioning rules

1. `protocolVersion` bumps only for incompatible changes — the goal is that
   it never bumps. Features arrive as new capability flags.
2. Additive only. No renames, no removals, no semantic changes to existing
   fields.
3. Unknown message types and fields are silently ignored, never errors.
4. Every new feature must answer: *node-land or contract?* Default
   node-land. Contract additions need a written amendment to this file.

---

## Future considerations

### Binary payloads on the tunnel

The tunnel is NDJSON with `*B64` fields for binary — every byte transmitted
pays ~33% base64 overhead plus JSON escaping and per-line framing. Fine
today for small vending traffic (HTML, JSON, small WS frames). Gets
expensive for large or hot payloads: multi-megabyte artifact bytes over
`http_request` / `response`, high-rate session streams, video/audio.
Direct browser → node isn't an option (the outbound-only invariant is
load-bearing). Escape hatch when it starts hurting:

- **Binary WebSocket frames on the tunnel**: current channel multiplexing
  is text-only (`ws_data.text` / `ws_data.dataB64`); a binary framing
  addition could pass bytes as `ws_data.data` without base64. Requires a
  new capability flag; keep the base64 path for pre-flag nodes.
- **WebRTC data channels for bulk transfer**: `desktop/v1` already
  negotiates WebRTC through the hub for screen frames + input; the same
  peer connection could carry an artifact-bytes data channel. Uses UDP
  hole-punching / TURN, so no inbound listener on the node.
- **Hub-cached artifact CDN**: for immutable content (artifacts are
  immutable by design), the node uploads once to the hub over the tunnel;
  browsers fetch from the hub directly at native speed. Keeps the
  outbound-only invariant but moves per-node storage semantics.

None of these are urgent while the working set is small and warm-cached.
Revisit when a specific payload class is measurably slow.
