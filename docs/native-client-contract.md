# Native-Client Contract (v1)

Status: **v1 draft — frozen on merge**. After freeze, changes are
additive only: new commands, new push events, new inbound message
types, new fields. Nothing renamed or removed. Receivers MUST silently
ignore unknown message types and unknown fields.

The **native client** here is any process that keeps a long-lived
`wss://…/ws/native/{clientId}` connection open specifically because
the in-page WebView isn't reliably alive — most concretely, the
Android Capacitor wrapper's `KeepAliveService.java`, which runs as
a foreground service so the OS doesn't kill the WebSocket when the
app is backgrounded or the screen is off.

Related docs:
- Hub↔node tunnel + vending: `docs/hub-node-contract-v1.md`.
- Android wrapper source (out-of-tree): `/mntc/code/vibr8-android`,
  specifically `android/app/src/main/java/ai/ringzero/vibr8/KeepAliveService.java`.

---

## Design principles

The plumbing below is shaped by three principles. Read them before
adding a new command or push event.

**1. Native side is a strict receiver.** The Android client responds
to correlated RPC commands from the server; that's the only thing it
must implement. Everything else (subscribe, permission_response,
push events) is optional additional surface a future native client
version can opt into. The server MUST work when the native client
only implements the mandatory subset.

**2. Session-scoped state stays out of the native surface when it
can.** The full session UI is served by the vended iframe (contract
ui/v1). Native clients should only see things the WebView can't
observe on its own — cross-app OS actions (`bring_to_foreground`,
`launch_app`), and eventually notifications that need to fire while
the WebView is paused. Anything a browser tab can already observe
does not belong here.

**3. Unknown values are silently ignored, on both sides.** The
Android client's `KeepAliveService.onMessage` treats any message
without a `command` field as a no-op; a `command` it doesn't
recognize just replies with `{"result": "ok"}`. The server's
`handle_native_message` treats any `type` it doesn't recognize as a
no-op. This lets either side ship additive changes without breaking
the other.

---

## A. Plumbing

### A1. Connection

`wss://{hub}/ws/native/{clientId}` — one connection per running
native client. The server registers the socket in
`WsBridge._native_ws_by_client[clientId]` and unregisters on
`onClose`. The connection is authenticated by the WebView's session
cookie / device token; the same auth middleware that guards
`/api/*` and `/ws/browser/*` applies.

Wire encoding: JSON per message, UTF-8, sent as WebSocket text
frames. No NDJSON multiplexing — one message per frame.

### A2. RPC correlation

**Server → native client** requests carry `id` (opaque string):

```json
{ "command": "bring_to_foreground", "id": "…", "params": { … } }
```

**Native client → server** responses echo the same `id`:

```json
{ "id": "…", "result": "ok" }
```

or

```json
{ "id": "…", "error": "message" }
```

The server keeps a `_native_rpc_pending: {id → Future}` map and
resolves the future when a response arrives. Timeouts drop the
pending entry and raise a `RuntimeError` on the caller.

---

## B. Server → native client

### B1. RPC commands (correlated)

Pinned set: **`vibr8_core.ws_bridge.NATIVE_RPC_COMMANDS`**. A test
in `server/tests/test_native_client_contract.py` asserts this set
matches exactly the list below; adding one requires updating both
the constant and this table.

| command | params | meaning |
|---|---|---|
| `bring_to_foreground` | *(none)* | Bring the Android app to the foreground (used e.g. when Ring0 wants to alert the user). |
| `launch_app` | `{ package?: string, url?: string }` | Launch another Android app by package id, or open a URL with the OS default handler. Currently used from Ring0 MCP's `launch_app` tool. |

### B2. Push events (fire-and-forget, subscribed clients only)

Pinned set: **`vibr8_core.ws_bridge.NATIVE_PUSH_EVENTS`**. Two
events only. A test asserts every push call site uses either an
event in this set or the transitional
`_NATIVE_PUSH_EVENTS_LEGACY_DARK` set (see §"Migration status" at
the bottom of this doc).

Message shape:

```json
{ "type": "push",
  "event": "<name>",
  "nodeId": "<hub-registered id>",
  "nodeName": "<hub-registered name>",
  …event-specific fields }
```

`nodeId` and `nodeName` are populated by the *hub* from the
tunnel-authenticated source; nodes cannot spoof each other's
identity. Reaches only clients that have sent
`{"type": "subscribe", …}` first (see §C). Unsubscribed native
clients receive nothing from this channel.

#### `attention`

"This node wants the user, now, out-of-band." Origin: the node
emits an events/v1 `attention` tunnel event (see
`hub-node-contract-v1.md` §B); the hub relays it to native
observers with the envelope above.

Payload fields (all string-typed; all beyond `reason` optional):

| field | required | meaning |
|---|---|---|
| `reason` | yes | Display-ready one-liner. The client renders this verbatim as the notification body. Do not parse it for structure. |
| `severity` | no | `"info"` \| `"warning"` \| `"urgent"`. Defaults to `"info"`. Clients may map to notification channels / priorities. |
| `contextKey` | no | Dedup key. Re-fires with the same key from the same node **replace** the prior notification rather than stacking. |
| `expiresAt` | no | ISO-8601. Clients may auto-clear a stale notification past this. |

**Client contract:** render an OS notification with `nodeName` as
title, `reason` as body; use `contextKey` as the notification id
so duplicates collapse; elevate to a high-priority channel with
vibration when `severity: "urgent"`; auto-clear after
`expiresAt` if the user hasn't acted; tap → invoke the local
`bring_to_foreground` action.

**Client MUST NOT:** parse `reason`; assume delivery reliability;
assume causal ordering with other events; treat missing
optional fields as errors.

**Clearing without action:** send another `attention` with the
same `contextKey`. Explicit `attention_cleared` is a v2 addition
if needed.

#### `busy`

"This node's main worker is / isn't currently active."
Level-triggered — the last value is the current state until the
next event supersedes it.

| field | required | meaning |
|---|---|---|
| `busy` | yes | Boolean. `true` means the node's Ring0 (or main worker) is doing something; `false` means idle. |

**Client contract:** update ambient status (persistent
foreground-service notification text, tray-icon state,
optional badge count on the 0→N or N→0 transition). Do **not**
fire a user-facing OS notification. Do **not** infer session
count or work quantity.

**Delivery guarantees (both events):** best-effort. WebSocket
drop = the event drops; not persisted, not replayed on reconnect.
For urgent-severity attention that must survive a dead socket,
FCM (or equivalent) is the future layer — kept out of v1 to
preserve the "additive only" constraint on this contract.

---

## C. Native client → server

Pinned set: **`vibr8_core.ws_bridge.NATIVE_INBOUND_TYPES`** plus the
correlated RPC-response shape (which is keyed by `id` rather than
`type` — not in the set).

| type | fields | meaning |
|---|---|---|
| `subscribe` | `sessionIds: string[]` OR `all: true` | Opt this client into the push-event channel. `all: true` means all sessions on this hub; otherwise the id list scopes which sessions produce pushes. |
| `unsubscribe` | *(none)* | Drop this client's subscription. |
| `permission_response` | `sessionId`, `request_id`, `behavior: "allow"\|"deny"`, `message?: string` | Resolve a pending permission that this native client displayed to the user. |

**RPC responses**: `{"id": <rpc_id>, "result": …}` or
`{"id": <rpc_id>, "error": "message"}` — matched against
`_native_rpc_pending` by id. Not tracked in `NATIVE_INBOUND_TYPES`
because there's no `type` field.

---

## Versioning rules

1. `NATIVE_RPC_COMMANDS`, `NATIVE_INBOUND_TYPES`, and
   `NATIVE_PUSH_EVENTS` grow but never shrink. Removing an entry is
   a wire break for any deployed native client that still uses it.
2. Field additions on existing message types are silently accepted;
   both sides ignore unknown fields.
3. When you add a new item to any of the three sets, add:
   - a row in the table above,
   - the string literal to the frozenset constant in `ws_bridge.py`,
   - a wire-shape sentence naming required and optional fields.
4. Contract additions need a written amendment to this file.

## Current implementation status

- **Server side**: all three sets are in use (see `ws_bridge.py`).
  `rpc_call` looks up `NATIVE_RPC_COMMANDS` to decide whether to
  prefer the native socket over the WebView; `handle_native_message`
  dispatches on `NATIVE_INBOUND_TYPES`; the hub's tunnel handler for
  `events/v1` `attention` and `busy` relays them to
  `_push_to_all_native_clients` on the hub bridge, enriched with
  `nodeId` and `nodeName`.
- **Android wrapper** (`vibr8-android`): implements the RPC command
  channel (both entries in `NATIVE_RPC_COMMANDS`). Does not yet
  send `subscribe`, so push events reach no client today. A future
  wrapper update sending `{"type":"subscribe", "all": true}` at
  connect and handling `type: "push"` messages will start receiving
  `attention` / `busy` without any server-side change.

## Migration status

Migration complete. The 26 `vibr8_node`-specific
`_push_to_native_clients(...)` call sites in `ws_bridge.py` have
been removed. The two helper methods
(`_push_to_native_clients` / `_push_to_all_native_clients`) survive
because the hub's tunnel handler for events/v1 `attention` and
`busy` uses them to relay to native observers. Their only remaining
callers live in `server/main.py`.

A regression test (`test_no_legacy_dark_fires_in_ws_bridge`) fails
if any `permission_request` / `status_change` / `cli_connected` etc.
event name reappears in a push call inside `ws_bridge.py`. Any new
node-agnostic observer event must be added to `NATIVE_PUSH_EVENTS`
and this doc's §B2.
