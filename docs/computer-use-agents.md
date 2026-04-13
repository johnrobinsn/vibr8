# Computer-Use Agent Developer Guide

How to build a new computer-use agent for vibr8. Covers the protocols, message types, frame streaming, agent registry, and session lifecycle.

## Architecture Overview

```
Browser UI
    ↕ WebSocket (NDJSON)
WsBridge (message router)
    ↕ on_message callback
ComputerUseAgent (your agent)
    ↕ get_frame / inject / on_frame
AgentTarget (DesktopTarget or AdbTarget)
    ↕ WebRTC or scrcpy
Remote desktop / Android device
```

The agent sits between the WsBridge (which handles browser communication) and an AgentTarget (which controls a device). Your agent receives user commands from WsBridge, decides what to do, and sends input events through the target.

## Core Protocols

### ComputerUseAgent (`server/computer_use_agent.py`)

The interface every computer-use agent must implement. It's a `typing.Protocol` with `@runtime_checkable`, so you don't need to inherit from it — just implement the methods.

```python
class ComputerUseAgent(Protocol):
    session_id: str

    @property
    def model_name(self) -> str: ...

    # Lifecycle
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def on_message(self, cb: Callable[[dict], Awaitable[None]]) -> None: ...

    # Act mode
    def submit_task(self, task: str, mode: ExecutionMode = ExecutionMode.AUTO) -> None: ...
    def interrupt(self) -> None: ...
    def approve(self) -> None: ...
    def reject(self) -> None: ...

    # Watch mode
    def watch_start(self, prompt: str | None = None, interval: float = 5.0) -> None: ...
    def watch_stop(self) -> None: ...
```

#### Properties

| Property | Type | Description |
|----------|------|-------------|
| `session_id` | `str` | Session ID this agent is bound to |
| `model_name` | `str` | Human-readable model name for display (e.g. `"UI-TARS-7B-DPO"`) |

#### Lifecycle Methods

**`start()`** — Initialize the agent. Called after the factory creates it but before `register_computer_use_agent()`. Start the target, load models, set up state.

**`stop()`** — Release all resources. Stop background tasks, close the target connection, free GPU memory.

**`on_message(cb)`** — Register the callback WsBridge uses to receive outgoing messages. Called once, right after construction. Store the callback and use it to emit messages to browsers.

#### Act Mode Methods

**`submit_task(task, mode)`** — Start executing a goal. The `task` is a natural-language instruction from the user. `mode` controls action gating:

| ExecutionMode | Behavior |
|---------------|----------|
| `AUTO` | Execute actions immediately, no confirmation |
| `CONFIRM` | Always ask the user before executing |
| `GATED` | Auto-execute if the action parsed cleanly; confirm otherwise |

Should interrupt any running task or watch before starting the new one.

**`interrupt()`** — Cancel the current task loop immediately.

**`approve()`** — User approved a pending action (in CONFIRM or GATED mode). Unblock the confirmation gate.

**`reject()`** — User rejected a pending action. Unblock the gate and stop the loop.

#### Watch Mode Methods

**`watch_start(prompt, interval)`** — Start periodic observation. The agent captures frames and describes what it sees, but takes no actions. `prompt` is the observation instruction (default: describe the screen). `interval` is seconds between observations.

**`watch_stop()`** — Stop watch mode.

### AgentTarget (`server/agent_registry.py`)

The interface for controlling a device. Two implementations exist: `DesktopTarget` (WebRTC) and `AdbTarget` (scrcpy/Android).

```python
class AgentTarget(Protocol):
    async def start(self) -> Any: ...
    async def stop(self) -> None: ...
    async def get_frame(self) -> av.VideoFrame | None: ...
    async def inject(self, event: dict[str, Any]) -> None: ...
```

**`get_frame()`** — Returns the most recent video frame (cached from the continuous internal stream). Returns `None` if no frame is available yet. Fine for agents that sample at low rates (e.g. 1 fps).

**`inject(event)`** — Send an input event to the device. Event format is a dict matching the WebRTC data channel protocol (see [Input Events](#input-events) below).

Both `DesktopTarget` and `AdbTarget` also expose `on_frame(callback)` for push-based frame delivery at full rate (~30fps). This is not part of the protocol — it's an implementation detail used by `FrameStream`.

## Message Types

### Messages Your Agent Sends (via `on_message` callback)

#### `status_change`
Notify browsers of agent state transitions.

```python
{"type": "status_change", "status": "running"}
```

Valid statuses: `"idle"`, `"running"`, `"watching"`, `"confirming"`, `"paused"`

WsBridge uses status changes to:
- Track `session.state["is_running"]` for Ring0 notifications
- Trigger `idle→running` and `running→idle` Ring0 events

#### `assistant`
Display a message in the chat. Mimics the Claude CLI assistant message format so the frontend renders it correctly.

```python
{
    "type": "assistant",
    "message": {
        "id": "agent_<unique_hex>",
        "role": "assistant",
        "model": "your-model-name",
        "content": [{"type": "text", "text": "**Thought:** I see a login form..."}],
        "stop_reason": "end_turn",
        "type": "message",
    },
    "parent_tool_use_id": None,
    "timestamp": 1712345678000,  # ms since epoch
    "iteration": 3,             # optional, act mode step number
}
```

- `content` is a list of content blocks (matching Anthropic API format). Use `{"type": "text", "text": "..."}` for text.
- `model` should be your agent's model name.
- `timestamp` is milliseconds since Unix epoch.
- WsBridge persists `assistant` messages to `session.message_history`.

#### `result`
Signal that the task is done.

```python
{
    "type": "result",
    "content": "Task completed",         # human-readable summary
    "iterations": 5,                      # how many steps were taken
    "timestamp": 1712345678000,
}
```

WsBridge persists `result` messages to history.

#### `observation`
Emit a watch-mode observation (what the agent sees on screen).

```python
{
    "type": "observation",
    "text": "The screen shows a file manager with three folders...",
    "timestamp": 1712345678000,
}
```

WsBridge persists `observation` messages to history.

#### `confirm`
Ask the user to approve/reject a pending action (CONFIRM or GATED mode).

```python
{
    "type": "confirm",
    "step": 3,
    "action_type": "click",
    "action_summary": "click(512, 340)",
    "thought": "I need to click the Submit button",
}
```

The frontend shows an approve/reject UI. The user's response arrives via `approve()` or `reject()`.

### Messages Your Agent Receives (via protocol methods)

These are routed by WsBridge from browser WebSocket messages:

| Browser Message | Agent Method Called | Notes |
|----------------|-------------------|-------|
| `{"type": "user_message", "content": "...", "executionMode": "auto"}` | `submit_task(content, mode)` | `executionMode` is optional, defaults to `"auto"` |
| `{"type": "interrupt"}` | `interrupt()` | |
| `{"type": "approve"}` | `approve()` | |
| `{"type": "reject"}` | `reject()` | |
| `{"type": "pause"}` | `pause()` | Only if your agent implements it |
| `{"type": "resume"}` | `resume()` | Only if your agent implements it |
| `{"type": "watch_start", "prompt": "...", "interval": 5.0}` | `watch_start(prompt, interval)` | |
| `{"type": "watch_stop"}` | `watch_stop()` | |

Note: `pause()` and `resume()` are not in the `ComputerUseAgent` protocol but WsBridge will call them if they exist on your agent. Optional to implement.

## Input Events

Events sent via `target.inject(event)`. These are JSON dicts sent over the WebRTC data channel (desktop) or translated to scrcpy commands (Android).

### Mouse Events

```python
# Mouse move
{"type": "mousemove", "x": 0.5, "y": 0.3}

# Click (left button)
{"type": "mousedown", "x": 0.5, "y": 0.3, "button": 0}
{"type": "mouseup",   "x": 0.5, "y": 0.3, "button": 0}

# Right click
{"type": "mousedown", "x": 0.5, "y": 0.3, "button": 2}
{"type": "mouseup",   "x": 0.5, "y": 0.3, "button": 2}

# Scroll
{"type": "wheel", "x": 0.5, "y": 0.3, "deltaY": -120}  # negative = up
```

Coordinates are **0.0–1.0 fractions** of the screen dimensions. The target converts to absolute pixels. If your model outputs 1000x1000 normalized coords, divide by 1000.

### Keyboard Events

```python
# Key press
{"type": "keydown", "key": "Enter"}
{"type": "keyup",   "key": "Enter"}

# Typing text
{"type": "keydown", "key": "a"}
{"type": "keyup",   "key": "a"}
```

Key names follow the Web `KeyboardEvent.key` spec (e.g. `"Enter"`, `"Backspace"`, `"Tab"`, `"Escape"`, `"ArrowUp"`, `"Control"`, `"Shift"`, `"Alt"`, `"Meta"`).

### Drag Events

```python
# Drag from (sx,sy) to (ex,ey)
{"type": "mousedown", "x": 0.2, "y": 0.3, "button": 0}
{"type": "mousemove", "x": 0.8, "y": 0.7}
{"type": "mouseup",   "x": 0.8, "y": 0.7, "button": 0}
```

## FrameStream (`server/frame_stream.py`)

For agents that need continuous video frames (not just single snapshots), `FrameStream` provides a ring-buffered stream with push and pull interfaces.

```python
from server.frame_stream import FrameStream

stream = FrameStream(target, max_buffer=300, target_fps=10)
await stream.start()

# Pull: get the last 3 seconds of frames
frames = stream.recent_seconds(3.0)   # -> list[TimestampedFrame]

# Pull: get the last 10 frames
frames = stream.recent(10)            # -> list[TimestampedFrame]

# Pull: get the most recent frame
frame = stream.latest()               # -> av.VideoFrame | None

# Push: subscribe to every new frame
unsub = stream.subscribe(my_callback) # callback(TimestampedFrame)

# Stats
stream.fps           # measured frames per second
stream.buffer_size   # current frame count

await stream.stop()
```

### How it Works

- If the target has `on_frame()` (DesktopTarget, ScrcpyClient do), FrameStream subscribes for push delivery — zero-overhead, every frame captured.
- Otherwise, falls back to polling `get_frame()` at `target_fps` in a background task.
- `target_fps` downsampling: if set, frames arriving faster than this rate are dropped before buffering. Useful for API-based agents where sending 30fps would be wasteful.
- Ring buffer caps memory. At 30fps with 1080p frames (~6MB YUV420P each), 300 frames = ~1.8GB.

### TimestampedFrame

```python
class TimestampedFrame:
    frame: av.VideoFrame    # the raw video frame
    timestamp: float        # time.monotonic() when captured
```

## Agent Registry (`server/agent_registry.py`)

Agents register themselves at import time. The session creation flow looks up the factory by type ID.

### Registering Your Agent

At the bottom of your agent module:

```python
from server.agent_registry import register_agent_type, AgentTypeInfo

register_agent_type(AgentTypeInfo(
    type_id="my-agent",                    # unique identifier
    display_name="My Agent (API)",         # shown in UI
    factory=create_my_agent,               # async factory function
    resource_type="api",                   # "local-gpu" | "api" | "hybrid"
    config_schema={                        # JSON Schema for config
        "type": "object",
        "properties": {
            "fps": {"type": "number", "default": 5},
        },
    },
    default_config={"fps": 5},
))
```

### Factory Function Signature

```python
async def create_my_agent(
    session_id: str,
    target: AgentTarget,
    config: dict[str, Any],
    status_cb: StatusCallback | None = None,
) -> MyAgent:
```

- `session_id`: the vibr8 session ID
- `target`: the device target (DesktopTarget or AdbTarget)
- `config`: user-provided config merged with `default_config`
- `status_cb`: optional callback to send status messages to browsers during initialization (e.g. "Loading model..."). Signature: `async (dict) -> None`

### Making Your Agent Discoverable

Add an import in `server/main.py` alongside the existing UI-TARS import:

```python
import server.ui_tars_agent    # noqa: F401
import server.my_agent         # noqa: F401  — registers "my-agent" type
```

The import triggers module-level `register_agent_type()`, making your agent available in `GET /api/agents` and session creation.

## Session Lifecycle

### 1. Session Creation

Browser sends `POST /api/sessions/create` with `backendType: "computer-use"`, optional `agentType` and `agentConfig`:

```json
{
  "backendType": "computer-use",
  "agentType": "my-agent",
  "agentConfig": {"fps": 10},
  "nodeId": "local"
}
```

### 2. Agent Initialization (`main.py:on_computer_use_created`)

1. Look up agent type from registry
2. Pre-create WsBridge session with `backend_type="computer-use"`
3. Create the target (DesktopTarget or AdbTarget based on nodeId)
4. Call factory: `agent = await factory(session_id, target, config, status_cb)`
5. Start agent: `await agent.start()`
6. Register with WsBridge: `ws_bridge.register_computer_use_agent(session_id, agent)`

### 3. Registration (`ws_bridge.py:register_computer_use_agent`)

1. Assert agent implements `ComputerUseAgent`
2. Wire up `on_message` callback that:
   - Persists `assistant`, `result`, `observation` messages to history
   - Tracks `is_running` state from `status_change` messages
   - Notifies Ring0 on state transitions
   - Broadcasts all messages to connected browsers
3. Send `session_init` to browsers with backend type and model name
4. Replay any messages queued while the agent was initializing

### 4. Runtime

WsBridge routes browser messages to agent methods. Agent sends messages back through the `on_message` callback. The agent loop runs autonomously — capture frame, infer, act, repeat.

### 5. Shutdown

`agent.stop()` is called when the session is destroyed. Clean up background tasks, release the target.

## Quick Reference: What Goes Where

| You Need To... | Where |
|----------------|-------|
| Define agent class | `server/my_agent.py` |
| Define factory function | `server/my_agent.py` (bottom of file) |
| Register agent type | `server/my_agent.py` (module-level `register_agent_type()`) |
| Make it discoverable | `server/main.py` (add `import server.my_agent  # noqa: F401`) |
| Use video frames | Create `FrameStream` in your factory, pass to agent |
| Send chat messages | `await self._emit({"type": "assistant", ...})` |
| Send status updates | `await self._emit({"type": "status_change", "status": "running"})` |
| Control the device | `await self._target.inject({"type": "mousedown", ...})` |
| Get a screenshot | `frame = await self._target.get_frame()` |

## Reference Implementation

See `server/ui_tars_agent.py` for a complete working agent. Key patterns:

- `_run_loop()` — act mode: screenshot → infer → parse → execute → repeat
- `_watch_loop()` — watch mode: screenshot → observe → emit → sleep
- `_gate_execution()` — CONFIRM/GATED action gating with asyncio.Event
- `_emit_*()` helpers — emit messages in the correct format
- Factory + registration at module bottom
- VLM singleton cache with lazy loading

See `server/skeleton_agent.py` for a minimal starter template.
