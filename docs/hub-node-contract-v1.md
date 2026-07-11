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
- **`attention`** (node → hub): the node wants the user's attention
  (`reason` is free text). v1 hubs may surface it as a notification line;
  richer handling is v2 (`notify`/`present`).

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
