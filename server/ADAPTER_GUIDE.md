# Backend Adapter Guide

How to write a new backend adapter for vibr8. An adapter translates between a backend-specific protocol (JSON-RPC over stdio, HTTP/SSE, etc.) and vibr8's unified browser message format, so the frontend is backend-agnostic.

Reference implementations: `codex_adapter.py` (JSON-RPC stdio), `opencode_adapter.py` (HTTP/SSE).

---

## Required Interface

WsBridge calls these methods on every adapter. All must exist and behave as specified.

### Connection State

```python
def is_connected(self) -> bool:
    """Whether the adapter has an active connection to the backend."""

@property
def connected(self) -> bool:
    """Same as is_connected(). Both must exist — WsBridge uses the method,
    but some code paths read the property."""
```

Both must return consistent values. `is_connected()` is used by `WsBridge.is_cli_connected()` and `handle_browser_open()`.

### Callback Registration

```python
def on_browser_message(self, cb: Callable[[dict], Awaitable[None] | None]) -> None:
    """Register callback for messages TO the browser. Called by WsBridge.attach_adapter().
    The adapter calls this callback for every translated message (assistant, result,
    permission_request, session_init, session_update, stream_event, etc.)."""

def on_session_meta(self, cb: Callable[[dict], None]) -> None:
    """Register callback for session metadata updates. Called with a dict containing
    any of: cliSessionId, model, cwd. WsBridge uses this to update session state."""

def on_disconnect(self, cb: Callable[[], Awaitable[None]]) -> None:
    """Register callback invoked when the backend process exits or connection drops.
    WsBridge uses this to clean up state and notify browsers."""

def on_init_error(self, cb: Callable[[str], None]) -> None:
    """Register callback for initialization failures. CliLauncher uses this to
    mark the session as exited in the launcher state."""
```

### Message Sending

```python
def send_browser_message(self, msg: dict) -> bool:
    """Send a message FROM the browser TO the backend. Returns True if accepted.
    Must handle these message types:
    - user_message: User input text (and optional images)
    - permission_response: Allow/deny for a pending permission request
    - interrupt: Cancel the current turn
    - set_model: Change the model mid-session
    - set_permission_mode: Change permission mode

    If the adapter is not yet initialized, queue user_message and permission_response
    for delivery after init completes. Drop non-queueable messages (set_model, etc.)
    and return False."""
```

### Cleanup

```python
async def disconnect(self) -> None:
    """Terminate the backend process and clean up resources.
    Must set connected state to False and invoke the on_disconnect callback."""
```

---

## Lifecycle Contract

### Spawn → Attach → Init → Ready

1. **Spawn**: `CliLauncher` creates the subprocess and constructs the adapter.
2. **Attach**: `CliLauncher` calls `WsBridge.attach_adapter(session_id, adapter, backend_type)`, which:
   - Creates or retrieves the session
   - Registers `on_browser_message`, `on_session_meta`, `on_disconnect` callbacks
   - Flushes any queued `pending_messages` via `send_browser_message()`
   - Broadcasts `cli_connected` to all browser sockets
3. **Init**: The adapter performs backend-specific initialization (e.g., JSON-RPC handshake). This may be async and happen after attach.
4. **Ready**: The adapter sets `_connected = True`, `_initialized = True`, and emits `session_init` via the `on_browser_message` callback.

### Message Flow (Steady State)

```
Browser → WsBridge → adapter.send_browser_message(msg)
                         ↓ (translates to backend protocol)
                     Backend process
                         ↓ (backend events)
                     adapter._browser_message_cb(translated_msg)
                         ↓
                     WsBridge on_browser_msg callback
                         ↓ (state tracking + broadcast)
                     All connected browsers
```

### Disconnect

When the backend process exits or the connection drops:

1. Adapter sets `_connected = False`
2. Adapter invokes `_disconnect_cb()`
3. WsBridge's `on_disconnect` callback:
   - Cancels all pending permissions
   - Clears `is_running` and `is_waiting_for_permission`
   - Notifies Ring0 of state transition
   - Sets `session.adapter = None`
   - Broadcasts `cli_disconnected` to browsers

### Relaunch

When a browser connects to a session whose backend is dead, WsBridge triggers `_on_cli_relaunch_needed`. The launcher respawns the process and re-attaches the adapter. The adapter should be fully recreated (not reused).

---

## Message Protocol

The adapter translates backend-specific events into these browser message types. All messages are dicts passed to the `on_browser_message` callback.

### Messages the Adapter Must Emit

| Type | When | Key Fields |
|------|------|------------|
| `session_init` | After backend initialization | `session`: dict with `model`, `cwd`, `tools`, `permissionMode`, etc. |
| `session_update` | On state changes (context usage, etc.) | `session`: partial dict of changed fields |
| `assistant` | When the backend produces a complete response chunk | `message.content`: list of `TextBlock`, `ToolUseBlock`, `ToolResultBlock`, `ThinkingBlock` |
| `result` | When a turn completes | `data.subtype`, `data.is_error`, `data.total_cost_usd`, `data.num_turns`, `data.duration_ms` |
| `permission_request` | When the backend needs user approval | `request.request_id`, `request.tool_name`, `request.input`, `request.tool_use_id` |
| `stream_event` | For real-time streaming (deltas, block starts/stops) | `event.type`: `message_start`, `content_block_start`, `content_block_delta`, `content_block_stop`, `message_stop` |
| `status_change` | On compacting or other status transitions | `status`: `"compacting"` or `null` |

### Content Block Types

Assistant messages carry `message.content`, a list of typed blocks:

```python
# Text output
{"type": "text", "text": "Hello world"}

# Tool invocation
{"type": "tool_use", "id": "tu-1", "name": "Bash", "input": {"command": "ls"}}

# Tool result (follows a tool_use in the same or next assistant message)
{"type": "tool_result", "tool_use_id": "tu-1", "content": "file.txt\ndir/"}

# Extended thinking
{"type": "thinking", "thinking": "Let me consider..."}
```

### Messages the Adapter Must Accept (via `send_browser_message`)

| Type | Purpose | Key Fields |
|------|---------|------------|
| `user_message` | User input | `content` (str), optional `images` |
| `permission_response` | Allow/deny a pending request | `request_id`, `behavior` (`"allow"` or `"deny"`) |
| `interrupt` | Cancel current turn | (none) |
| `set_model` | Change model | `model` (str) |
| `set_permission_mode` | Change permission mode | `mode` (str) |

---

## State Tracking (WsBridge Responsibilities)

WsBridge's `attach_adapter` callback handles state management that the adapter does NOT need to implement. The adapter just emits messages; WsBridge reacts to them:

| Message Type | WsBridge Action |
|---|---|
| `assistant` | Sets `is_running = True`, notifies Ring0 (`idle→running`), auto-clears stale permissions |
| `result` | Sets `is_running = False`, notifies Ring0 (`running→idle`), extracts `total_cost_usd` / `num_turns`, schedules pen release, trims history at 500 messages |
| `permission_request` | Sets `is_waiting_for_permission = True`, notifies Ring0 (`running→waiting_for_permission`), stores in `pending_permissions` |
| `session_init` | Merges into `session.state`, persists |
| `session_update` | Merges into `session.state`, persists |
| `status_change` | Tracks `is_compacting`, persists |

The adapter does NOT need to:
- Track `is_running` or `is_waiting_for_permission`
- Send Ring0 notifications
- Push to native clients
- Manage the pen system
- Persist session state

All of this is handled by WsBridge once the adapter emits the correct message types.

---

## Error Handling

### Init Errors

If initialization fails (bad credentials, missing binary, protocol mismatch):
1. Call `_init_error_cb(error_message)` if registered
2. Set `_connected = False`
3. Call `_disconnect_cb()` to trigger cleanup

### Runtime Errors

- Malformed backend messages: log and skip (don't crash the adapter loop)
- Backend process crash: detect via stdout EOF or process exit, trigger disconnect
- Timeout on init handshake: use `asyncio.wait_for()` with a reasonable timeout (e.g., 30s)

### Backend Connectivity Check

WsBridge calls `adapter.is_connected()` during `handle_browser_open()`. If this raises an exception, it's caught and treated as disconnected. But your implementation should never raise — just return a bool.

---

## Tool Use Deduplication

Backends may emit `item/started` and `item/completed` for the same tool invocation. The adapter must track emitted tool use IDs (`_emitted_tool_use_ids`) to avoid sending duplicate `tool_use` blocks to the browser.

- On `item/started` for a tool: emit the `tool_use` block, add the ID to `_emitted_tool_use_ids`
- On `item/completed` for a tool: check if the ID is in `_emitted_tool_use_ids`. If not, emit a backfill `tool_use` block first (the start event may have been missed)
- On turn completion: **clear `_emitted_tool_use_ids`** so the next turn starts fresh

Forgetting to clear on turn completion causes tools to be silently skipped in subsequent turns.

---

## Session Metadata

When the adapter learns the backend's session/thread ID, model, or working directory, call `_session_meta_cb` with a dict:

```python
self._session_meta_cb({
    "cliSessionId": thread_id,   # Used for session persistence and relaunch
    "model": model_name,
    "cwd": working_directory,
})
```

The `cliSessionId` is important — CliLauncher persists it and uses it for session recovery.

---

## Testing Checklist

When writing tests for a new adapter, cover:

### Interface Compliance
- [ ] `is_connected()` method exists and returns bool
- [ ] `connected` property exists and matches `is_connected()`
- [ ] All four callbacks (`on_browser_message`, `on_session_meta`, `on_disconnect`, `on_init_error`) can be registered
- [ ] `send_browser_message()` returns True/False correctly
- [ ] `disconnect()` terminates the backend and sets connected to False

### Message Queuing
- [ ] `user_message` is queued when adapter is not yet initialized
- [ ] `permission_response` is queued when adapter is not yet initialized
- [ ] Non-queueable messages (`set_model`, etc.) are dropped and return False when not initialized
- [ ] Queued messages are flushed after initialization completes

### Message Translation (Backend → Browser)
- [ ] Backend text responses → `assistant` messages with `TextBlock` content
- [ ] Backend tool invocations → `assistant` messages with `ToolUseBlock` content
- [ ] Backend tool results → `assistant` messages with `ToolResultBlock` content
- [ ] Backend thinking/reasoning → `assistant` messages with `ThinkingBlock` content
- [ ] Turn completion → `result` message with `is_error`, `subtype`, cost data
- [ ] Turn failure → `result` message with `is_error: True` and error text
- [ ] Backend streaming → `stream_event` messages with correct event types
- [ ] Token/context usage → `session_update` with `context_used_percent`
- [ ] Tool use dedup: no duplicate `tool_use` blocks for same ID
- [ ] `_emitted_tool_use_ids` cleared on turn completion

### Permission Flow
- [ ] Backend approval request → `permission_request` with `request_id`, `tool_name`, `input`
- [ ] Browser allow → backend accept response
- [ ] Browser deny → backend decline response

### WsBridge Integration
- [ ] `attach_adapter` sets session properties correctly
- [ ] `is_cli_connected()` returns True for attached adapter
- [ ] `assistant` message sets `is_running = True` and triggers Ring0 notification
- [ ] `result` message sets `is_running = False` and triggers Ring0 notification
- [ ] `permission_request` sets `is_waiting_for_permission = True`
- [ ] `assistant` auto-clears stale pending permissions
- [ ] `result` auto-clears stale pending permissions
- [ ] Messages appended to `session.message_history`
- [ ] `session_init` updates `session.state`
- [ ] Disconnect clears adapter, resets running state, broadcasts `cli_disconnected`
- [ ] Queued messages flushed on attach

See `server/tests/test_codex_adapter.py` for a complete example (49 tests).

---

## Reference Adapter: Hermes (ACP)

`hermes_adapter.py` translates the Agent Client Protocol (ACP, v0.11.2)
JSON-RPC over stdio to vibr8's browser message format. The wire transport
is structurally identical to Codex's, but the method names and event
shapes are different.

### ACP subset used

| Direction | Method | Purpose |
|---|---|---|
| → server | `initialize` | Handshake with `protocolVersion: 1` + `clientInfo`. |
| ← server | `notifications/initialized` | Acked silently. |
| → server | `session/new` | Create a fresh session. Takes `cwd` and `mcpServers`. Returns `sessionId` and optional `models.current`. |
| → server | `session/load` | Resume an existing session by id. Used by `_spawn_hermes` when `info.cliSessionId` is set. |
| → server | `session/prompt` | Submit a turn. `prompt` is an array of content parts (text + base64 images). Returns `{stopReason}`. |
| → server | `session/cancel` (notification) | Interrupt the current turn. |
| → server | `session/set_model` | Switch the active model mid-session. Called after init when an explicit `options.model` differs from `result.models.current`, and on outgoing `set_model` browser messages. |
| ← server | `session/update` notifications | Streaming + state, discriminated by `sessionUpdate`. Handled: `agent_message_chunk`, `agent_thought_chunk`, `tool_call`, `tool_call_update`, `usage_update`, `available_commands_update`, `current_mode_update`. Silently ignored: `user_message_chunk`, `session_info_update`, `plan`, `config_option_update`. |
| ← server | `session/request_permission` (request) | Tool approval. Adapter records `rpc_id` under a synthesized `request_id`, emits `permission_request` with `_acp_options` so the response handler can pick the right `optionId`. |

### Key differences from the Codex adapter

- **No per-turn cost.** Codex emits per-turn token usage in `turn/completed`; Hermes only sends cumulative `usage_update` notifications. The adapter accumulates `_total_cost_usd` and reports the same total in every `result` message.
- **Session resume uses `session/load`** rather than a thread-id parameter on the create call. `_spawn_hermes` passes `info.cliSessionId` as `session_id_to_resume`; `_monitor_exit_hermes` clears it if the process dies within 5s (treated as a failed load).
- **Streaming bracketing is synthesised.** Hermes emits `agent_message_chunk` / `agent_thought_chunk` only — no item-start/stop events. The adapter tracks `_open_block_type` / `_open_block_index` to emit matching `content_block_start` / `content_block_stop` events around runs of deltas. Blocks close on tool_use, on type change, and at turn end.
- **Tool calls are emitted as full assistant messages** (`tool_use` + `tool_result` blocks). For non-final statuses (`in_progress`, `pending`) the adapter additionally emits a deduped `tool_use_progress` `stream_event` so the UI can render running indicators between start and result.
- **Permission options are ACP-shaped.** The browser-side `permission_request` carries an `_acp_options` array; the response handler looks for the first option with `kind` in `{"allow", "allow_always"}` and sends `{"outcome": {"outcome": "selected", "optionId": ...}}` (or `{"outcome": "cancelled"}` for deny).
- **`available_commands_update`** is mapped to `session_update(slash_commands=[...])`; **`current_mode_update`** is mapped to `session_update(permissionMode=...)`. Both accept either an object (`{name: "plan"}`) or a plain string.

### Known limitations

- **`set_permission_mode` is ack-only.** ACP 0.11.2 has no client→server mode-switch RPC — mode changes come from Hermes via `current_mode_update`. The outgoing handler acknowledges (`returns True`) and broadcasts a `session_update` with the requested mode so the UI badge doesn't get stuck, but the agent's behaviour does not change.
- **Per-session model selection requires a follow-up call.** `session/new` has no `model` parameter, so the adapter calls `session/set_model` post-init when an explicit model was requested. If that call fails, the adapter falls back to whatever `models.current` Hermes returned.
- **`plan` updates are ignored.** Mapping ACP's plan structure into vibr8's UI is non-trivial; currently dropped.

### Testing

`server/tests/test_hermes_adapter.py` (50 tests) covers the JSON-RPC transport,
interface compliance, both init paths, streaming/bracketing, tool calls,
session updates, permission flow, and the outgoing dispatch table.
