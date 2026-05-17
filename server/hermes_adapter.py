"""Hermes Agent ACP Adapter

Translates between the Hermes ACP (Agent Client Protocol) JSON-RPC protocol
over stdio and vibr8's BrowserIncomingMessage/BrowserOutgoingMessage types.

Hermes ACP is nearly identical to Codex's JSON-RPC transport — both use
NDJSON on stdin/stdout. The key differences are the method names (ACP uses
`session/new`, `session/prompt`, `session/cancel` vs. Codex's `thread/start`,
`turn/start`, etc.) and the notification format (`session/update` with a
`sessionUpdate` discriminator field).

Wire protocol reference: agent-client-protocol v0.11.2
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from server.session_types import (
    BrowserIncomingMessage,
    BrowserOutgoingMessage,
    CLIResultMessage,
    PermissionRequest,
    SessionState,
)

logger = logging.getLogger(__name__)

JsonRpcMessage = Dict[str, Any]


@dataclass
class HermesAdapterOptions:
    model: Optional[str] = None
    cwd: Optional[str] = None
    approval_mode: Optional[str] = None
    session_id_to_resume: Optional[str] = None
    mcp_servers: Optional[List[Dict[str, Any]]] = None


class JsonRpcTransport:
    """Reads NDJSON from proc.stdout and writes JSON-RPC to proc.stdin."""

    def __init__(
        self,
        stdin: asyncio.StreamWriter,
        stdout: asyncio.StreamReader,
    ) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._next_id: int = 1
        self._pending: Dict[int, asyncio.Future[Any]] = {}
        self._notification_handler: Optional[Callable[[str, Dict[str, Any]], None]] = None
        self._request_handler: Optional[Callable[[str, int, Dict[str, Any]], None]] = None
        self._connected: bool = True
        self._buffer: str = ""
        self._reader_task: asyncio.Task[None] = asyncio.create_task(self._read_stdout())

    async def _read_stdout(self) -> None:
        try:
            while True:
                data = await self._stdout.read(65536)
                if not data:
                    break
                self._buffer += data.decode("utf-8", errors="replace")
                self._process_buffer()
        except Exception as exc:
            logger.error("[hermes-adapter] stdout reader error: %s", exc)
        finally:
            self._connected = False

    def _process_buffer(self) -> None:
        lines = self._buffer.split("\n")
        self._buffer = lines.pop() if lines else ""
        for line in lines:
            trimmed = line.strip()
            if not trimmed:
                continue
            try:
                msg: JsonRpcMessage = json.loads(trimmed)
            except json.JSONDecodeError:
                logger.warning("[hermes-adapter] Failed to parse JSON-RPC: %s", trimmed[:200])
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: JsonRpcMessage) -> None:
        try:
            self._dispatch_inner(msg)
        except Exception as exc:
            logger.error("[hermes-adapter] dispatch error for %s: %s", msg.get("method", "?"), exc)

    def _dispatch_inner(self, msg: JsonRpcMessage) -> None:
        if "id" in msg and msg["id"] is not None:
            if "method" in msg and msg["method"]:
                if self._request_handler is not None:
                    self._request_handler(msg["method"], msg["id"], msg.get("params", {}))
            else:
                rpc_id: int = msg["id"]
                future = self._pending.pop(rpc_id, None)
                if future is not None and not future.done():
                    error = msg.get("error")
                    if error:
                        future.set_exception(RuntimeError(error.get("message", "Unknown RPC error")))
                    else:
                        future.set_result(msg.get("result"))
        elif "method" in msg:
            if self._notification_handler is not None:
                self._notification_handler(msg["method"], msg.get("params", {}))

    async def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        rpc_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[rpc_id] = future
        request = json.dumps({"jsonrpc": "2.0", "method": method, "id": rpc_id, "params": params or {}})
        await self._write_raw(request + "\n")
        return await future

    async def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        notification = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}})
        await self._write_raw(notification + "\n")

    async def respond(self, rpc_id: Any, result: Any) -> None:
        response = json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": result})
        await self._write_raw(response + "\n")

    def on_notification(self, handler: Callable[[str, Dict[str, Any]], None]) -> None:
        self._notification_handler = handler

    def on_request(self, handler: Callable[[str, Any, Dict[str, Any]], None]) -> None:
        self._request_handler = handler

    @property
    def connected(self) -> bool:
        return self._connected

    async def _write_raw(self, data: str) -> None:
        self._stdin.write(data.encode("utf-8"))
        await self._stdin.drain()


class HermesAdapter:
    """Orchestrates a Hermes ACP session, translating between the JSON-RPC
    protocol and BrowserIncomingMessage / BrowserOutgoingMessage types."""

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        session_id: str,
        options: Optional[HermesAdapterOptions] = None,
    ) -> None:
        self._proc = proc
        self._session_id = session_id
        self._options = options or HermesAdapterOptions()

        self._browser_message_cb: Optional[Callable[[dict], None]] = None
        self._session_meta_cb: Optional[Callable[[Dict[str, Optional[str]]], None]] = None
        self._disconnect_cb: Optional[Callable[[], Any]] = None
        self._init_error_cb: Optional[Callable[[str], None]] = None

        self._hermes_session_id: Optional[str] = None
        self._connected: bool = False
        self._initialized: bool = False

        self._streaming_text: str = ""
        self._msg_counter: int = 0
        self._emitted_tool_use_ids: set[str] = set()
        # Tracks status updates we have already pushed for a given tool call,
        # so we don't spam the browser with identical progress events.
        self._tool_call_last_status: Dict[str, str] = {}
        self._pending_outgoing: List[BrowserOutgoingMessage] = []
        self._pending_approvals: Dict[str, Any] = {}

        self._is_running: bool = False
        self._turn_start_time: float = 0
        self._total_cost_usd: float = 0
        self._num_turns: int = 0

        # Streaming content-block bracketing: track whether a text/thinking
        # block is currently "open" within the current turn so we can emit
        # matching content_block_start / content_block_stop events.
        self._open_block_type: Optional[str] = None  # "text" | "thinking" | None
        self._open_block_index: int = 0

        if proc.stdout is None or proc.stdin is None:
            raise RuntimeError("Hermes process must have stdio pipes")

        self._transport = JsonRpcTransport(proc.stdin, proc.stdout)
        self._transport.on_notification(self._handle_notification)
        self._transport.on_request(self._handle_request)

        self._exit_task: asyncio.Task[None] = asyncio.create_task(self._monitor_exit())
        self._init_task: asyncio.Task[None] = asyncio.create_task(self._initialize())

    async def _monitor_exit(self) -> None:
        await self._proc.wait()
        self._connected = False
        if self._disconnect_cb is not None:
            result = self._disconnect_cb()
            if asyncio.iscoroutine(result):
                await result

    # ---- Public API ----------------------------------------------------------

    def send_browser_message(self, msg: BrowserOutgoingMessage) -> bool:
        if not self._initialized or not self._hermes_session_id:
            msg_type = msg.get("type")
            if msg_type in ("user_message", "permission_response"):
                logger.info("[hermes-adapter] Queuing %s -- adapter not yet initialized", msg_type)
                self._pending_outgoing.append(msg)
                return True
            if not self._connected:
                return False

        return self._dispatch_outgoing(msg)

    def _dispatch_outgoing(self, msg: BrowserOutgoingMessage) -> bool:
        msg_type = msg.get("type")
        if msg_type == "user_message":
            asyncio.create_task(self._handle_outgoing_user_message(msg))
            return True
        elif msg_type == "permission_response":
            asyncio.create_task(self._handle_outgoing_permission_response(msg))
            return True
        elif msg_type == "interrupt":
            asyncio.create_task(self._handle_outgoing_interrupt())
            return True
        elif msg_type == "set_model":
            asyncio.create_task(self._handle_outgoing_set_model(msg))
            return True
        elif msg_type == "set_permission_mode":
            # ACP 0.11.2 has no client→server mode-switch RPC; modes are
            # driven by Hermes itself via current_mode_update notifications.
            # Acknowledge the toggle and broadcast a session_update so the
            # UI doesn't get stuck "loading".
            requested_mode = msg.get("mode", "")
            logger.info(
                "[hermes-adapter] set_permission_mode(%s): ACP has no runtime "
                "mode-switch RPC; acknowledging without applying.",
                requested_mode,
            )
            if requested_mode:
                self._emit({"type": "session_update", "session": {
                    "permissionMode": requested_mode,
                }})
            return True
        return False

    def on_browser_message(self, cb: Callable[[dict], None]) -> None:
        self._browser_message_cb = cb

    def on_session_meta(self, cb: Callable[[Dict[str, Optional[str]]], None]) -> None:
        self._session_meta_cb = cb

    def on_disconnect(self, cb: Callable[[], None]) -> None:
        self._disconnect_cb = cb

    def on_init_error(self, cb: Callable[[str], None]) -> None:
        self._init_error_cb = cb

    def is_connected(self) -> bool:
        return self._connected

    @property
    def connected(self) -> bool:
        return self._connected

    async def disconnect(self) -> None:
        self._connected = False
        if self._init_task and not self._init_task.done():
            self._init_task.cancel()
        try:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._proc.kill()
        except Exception:
            pass

    @property
    def hermes_session_id(self) -> Optional[str]:
        return self._hermes_session_id

    # ---- Initialization ------------------------------------------------------

    async def _initialize(self) -> None:
        try:
            await self._transport.call("initialize", {
                "protocolVersion": 1,
                "clientInfo": {"name": "vibr8", "version": "1.0.0"},
                "clientCapabilities": {},
            })

            self._connected = True
            self._initialized = True

            if self._options.session_id_to_resume:
                result = await self._transport.call("session/load", {
                    "cwd": self._options.cwd or "",
                    "sessionId": self._options.session_id_to_resume,
                    "mcpServers": self._options.mcp_servers or [],
                })
                self._hermes_session_id = self._options.session_id_to_resume
            else:
                result = await self._transport.call("session/new", {
                    "cwd": self._options.cwd or "",
                    "mcpServers": self._options.mcp_servers or [],
                })
                self._hermes_session_id = result.get("sessionId") or result.get("session_id")

            current_model = ""
            models_info = result.get("models", {})
            if isinstance(models_info, dict):
                if models_info.get("current"):
                    current_model = models_info["current"]
                elif models_info.get("availableModels"):
                    for m in models_info["availableModels"]:
                        desc = m.get("description", "")
                        if "current" in desc.lower():
                            current_model = m.get("name") or m.get("modelId", "")
                            break

            # If the caller specified a model (from /api/sessions/create), tell
            # Hermes to use it. session/new has no model parameter, so we have
            # to follow up with session/set_model.
            model = self._options.model or current_model
            if (
                self._options.model
                and self._options.model != current_model
                and self._hermes_session_id
            ):
                try:
                    await self._transport.call("session/set_model", {
                        "sessionId": self._hermes_session_id,
                        "model": self._options.model,
                    })
                    model = self._options.model
                except Exception as exc:
                    logger.warning(
                        "[hermes-adapter] set_model(%s) failed during init: %s; "
                        "falling back to %s",
                        self._options.model, exc, current_model,
                    )
                    model = current_model

            if self._session_meta_cb is not None:
                self._session_meta_cb({
                    "cliSessionId": self._hermes_session_id,
                    "model": model,
                    "cwd": self._options.cwd,
                })

            state: SessionState = {
                "session_id": self._session_id,
                "backend_type": "hermes",
                "model": model,
                "cwd": self._options.cwd or "",
                "tools": [],
                "permissionMode": self._options.approval_mode or "suggest",
                "claude_code_version": "",
                "mcp_servers": [],
                "agents": [],
                "slash_commands": [],
                "skills": [],
                "total_cost_usd": 0,
                "num_turns": 0,
                "context_used_percent": 0,
                "is_compacting": False,
                "git_branch": "",
                "is_worktree": False,
                "repo_root": "",
                "git_ahead": 0,
                "git_behind": 0,
                "total_lines_added": 0,
                "total_lines_removed": 0,
            }

            self._emit({"type": "session_init", "session": state})

            if self._pending_outgoing:
                logger.info("[hermes-adapter] Flushing %d queued message(s)", len(self._pending_outgoing))
                queued = self._pending_outgoing[:]
                self._pending_outgoing.clear()
                for queued_msg in queued:
                    self._dispatch_outgoing(queued_msg)

        except Exception as exc:
            error_msg = f"Hermes initialization failed: {exc}"
            logger.error("[hermes-adapter] %s", error_msg)
            self._emit({"type": "error", "message": error_msg})
            self._connected = False
            if self._init_error_cb is not None:
                self._init_error_cb(error_msg)

    # ---- Outgoing message handlers -------------------------------------------

    async def _handle_outgoing_user_message(self, msg: dict) -> None:
        if not self._hermes_session_id:
            self._emit({"type": "error", "message": "No Hermes session started yet"})
            return

        content_blocks: List[Dict[str, Any]] = []

        images = msg.get("images")
        if images:
            for img in images:
                content_blocks.append({
                    "type": "image",
                    "data": img["data"],
                    "mimeType": img.get("media_type", "image/png"),
                })

        content_blocks.append({"type": "text", "text": msg["content"]})

        try:
            self._is_running = True
            self._turn_start_time = time.monotonic()
            self._streaming_text = ""
            self._emitted_tool_use_ids.clear()
            self._tool_call_last_status.clear()
            self._open_block_type = None
            self._open_block_index = 0

            self._emit({"type": "stream_event", "event": {"type": "message_start"}})

            result = await self._transport.call("session/prompt", {
                "sessionId": self._hermes_session_id,
                "prompt": content_blocks,
            })

            self._handle_prompt_response(result or {})
        except Exception as exc:
            logger.error("[hermes-adapter] prompt error: %s", exc)
            self._emit({"type": "error", "message": str(exc)})
            self._finish_turn(is_error=True, error_msg=str(exc))

    async def _handle_outgoing_permission_response(self, msg: dict) -> None:
        request_id = msg.get("request_id", "")
        behavior = msg.get("behavior", "deny")

        rpc_id = self._pending_approvals.pop(request_id, None)
        if rpc_id is None:
            logger.warning("[hermes-adapter] No pending approval for request_id=%s", request_id)
            return

        if behavior == "allow":
            options = msg.get("_acp_options", [])
            allow_option_id = None
            for opt in options:
                if opt.get("kind") in ("allow", "allow_always"):
                    allow_option_id = opt.get("optionId")
                    break
            if not allow_option_id and options:
                allow_option_id = options[0].get("optionId")

            await self._transport.respond(rpc_id, {
                "outcome": {
                    "outcome": "selected",
                    "optionId": allow_option_id or "allow",
                },
            })
        else:
            await self._transport.respond(rpc_id, {
                "outcome": {"outcome": "cancelled"},
            })

    async def _handle_outgoing_interrupt(self) -> None:
        if not self._hermes_session_id:
            return
        try:
            await self._transport.notify("session/cancel", {
                "sessionId": self._hermes_session_id,
            })
        except Exception as exc:
            logger.warning("[hermes-adapter] interrupt error: %s", exc)

    async def _handle_outgoing_set_model(self, msg: dict) -> None:
        if not self._hermes_session_id:
            return
        model = msg.get("model", "")
        try:
            await self._transport.call("session/set_model", {
                "sessionId": self._hermes_session_id,
                "model": model,
            })
            self._options.model = model
            if self._session_meta_cb:
                self._session_meta_cb({"model": model})
        except Exception as exc:
            logger.warning("[hermes-adapter] set_model error: %s", exc)

    # ---- Incoming notifications from Hermes ----------------------------------

    def _handle_notification(self, method: str, params: Dict[str, Any]) -> None:
        if method == "session/update":
            self._handle_session_update(params)
        elif method == "notifications/initialized":
            pass
        else:
            logger.debug("[hermes-adapter] Unhandled notification: %s", method)

    def _handle_request(self, method: str, rpc_id: Any, params: Dict[str, Any]) -> None:
        if method == "session/request_permission":
            self._handle_permission_request(rpc_id, params)
        else:
            logger.debug("[hermes-adapter] Unhandled request: %s", method)
            asyncio.create_task(self._transport.respond(rpc_id, None))

    def _handle_session_update(self, params: Dict[str, Any]) -> None:
        update = params.get("update", params)
        update_type = update.get("sessionUpdate", "")

        if update_type == "agent_message_chunk":
            self._handle_agent_message_chunk(update)
        elif update_type == "agent_thought_chunk":
            self._handle_thought_chunk(update)
        elif update_type == "tool_call":
            self._handle_tool_call_start(update)
        elif update_type == "tool_call_update":
            self._handle_tool_call_update(update)
        elif update_type == "usage_update":
            self._handle_usage_update(update)
        elif update_type == "available_commands_update":
            self._handle_available_commands(update)
        elif update_type == "current_mode_update":
            self._handle_current_mode(update)
        elif update_type in ("user_message_chunk", "session_info_update",
                             "plan", "config_option_update"):
            pass
        else:
            logger.debug("[hermes-adapter] Unhandled session update type: %s", update_type)

    def _open_block(self, block_type: str) -> None:
        """Open a streaming content block of the given type, closing any
        existing block first. Idempotent if the same block is already open."""
        if self._open_block_type == block_type:
            return
        self._close_open_block()
        self._open_block_index += 1
        if block_type == "thinking":
            content_block: Dict[str, Any] = {"type": "thinking", "thinking": ""}
        else:
            content_block = {"type": "text", "text": ""}
        self._emit({"type": "stream_event", "event": {
            "type": "content_block_start",
            "index": self._open_block_index,
            "content_block": content_block,
        }})
        self._open_block_type = block_type

    def _close_open_block(self) -> None:
        if self._open_block_type is None:
            return
        self._emit({"type": "stream_event", "event": {
            "type": "content_block_stop",
            "index": self._open_block_index,
        }})
        self._open_block_type = None

    def _handle_agent_message_chunk(self, params: Dict[str, Any]) -> None:
        content = params.get("content", {})
        if content.get("type") == "text":
            text = content.get("text", "")
            if text:
                self._open_block("text")
                self._streaming_text += text
                self._emit({"type": "stream_event", "event": {
                    "type": "content_block_delta",
                    "index": self._open_block_index,
                    "delta": {"type": "text_delta", "text": text},
                }})

    def _handle_thought_chunk(self, params: Dict[str, Any]) -> None:
        content = params.get("content", {})
        if content.get("type") == "text":
            text = content.get("text", "")
            if text:
                self._open_block("thinking")
                self._emit({"type": "stream_event", "event": {
                    "type": "content_block_delta",
                    "index": self._open_block_index,
                    "delta": {"type": "thinking_delta", "thinking": text},
                }})

    def _handle_available_commands(self, params: Dict[str, Any]) -> None:
        commands = params.get("availableCommands") or params.get("commands") or []
        names: List[str] = []
        for c in commands:
            if isinstance(c, dict):
                name = c.get("name") or c.get("id") or c.get("title")
                if isinstance(name, str) and name:
                    names.append(name)
            elif isinstance(c, str):
                names.append(c)
        self._emit({"type": "session_update", "session": {
            "slash_commands": names,
        }})

    def _handle_current_mode(self, params: Dict[str, Any]) -> None:
        mode = params.get("currentMode") or params.get("mode")
        if isinstance(mode, dict):
            mode = mode.get("name") or mode.get("id")
        if isinstance(mode, str) and mode:
            self._emit({"type": "session_update", "session": {
                "permissionMode": mode,
            }})

    def _emit_tool_use(self, tool_call_id: str, title: str, raw_input: Any) -> None:
        """Emit a tool_use assistant message for a Hermes tool_call."""
        tool_input = (
            raw_input if isinstance(raw_input, dict)
            else {"input": str(raw_input) if raw_input else ""}
        )
        self._flush_streaming_text()
        self._close_open_block()
        self._msg_counter += 1
        msg_id = f"hermes-msg-{self._msg_counter}"
        self._emit({
            "type": "assistant",
            "msg_id": msg_id,
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": tool_call_id,
                    "name": title,
                    "input": tool_input,
                }],
            },
        })

    def _handle_tool_call_start(self, params: Dict[str, Any]) -> None:
        tool_call_id = params.get("toolCallId", str(uuid.uuid4()))
        title = params.get("title", "tool_call")
        raw_input = params.get("rawInput")

        if tool_call_id in self._emitted_tool_use_ids:
            return
        self._emitted_tool_use_ids.add(tool_call_id)
        self._emit_tool_use(tool_call_id, title, raw_input)

    def _handle_tool_call_update(self, params: Dict[str, Any]) -> None:
        tool_call_id = params.get("toolCallId", "")
        status = params.get("status")
        raw_output = params.get("rawOutput")
        content_list = params.get("content", [])

        if tool_call_id not in self._emitted_tool_use_ids:
            # Backfill the tool_use block if we missed the start event.
            self._emitted_tool_use_ids.add(tool_call_id)
            title = params.get("title", "tool_call")
            self._emit_tool_use(tool_call_id, title, params.get("rawInput"))

        # Surface in-flight progress so the UI can show "running" / "pending"
        # rather than jumping from start straight to result. Dedup repeated
        # statuses for the same tool call.
        if isinstance(status, str) and status not in ("completed", "errored"):
            last = self._tool_call_last_status.get(tool_call_id)
            if status != last:
                self._tool_call_last_status[tool_call_id] = status
                self._emit({"type": "stream_event", "event": {
                    "type": "tool_use_progress",
                    "tool_use_id": tool_call_id,
                    "status": status,
                }})
            # Don't emit a result yet for in-progress updates.
            if raw_output is None and not content_list:
                return

        if status in ("completed", "errored") or raw_output is not None:
            output_text = ""
            if raw_output is not None:
                output_text = str(raw_output) if not isinstance(raw_output, str) else raw_output
            elif content_list:
                parts = []
                for c in content_list:
                    if isinstance(c, dict):
                        val = c.get("text", c.get("content", c))
                        parts.append(val if isinstance(val, str) else json.dumps(val))
                    else:
                        parts.append(str(c))
                output_text = "\n".join(parts)

            is_error = status == "errored"
            # Clear progress tracking once we have a final outcome.
            self._tool_call_last_status.pop(tool_call_id, None)

            self._msg_counter += 1
            msg_id = f"hermes-msg-{self._msg_counter}"
            self._emit({
                "type": "assistant",
                "msg_id": msg_id,
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_call_id,
                        "content": output_text,
                        "is_error": is_error,
                    }],
                },
            })

    def _handle_usage_update(self, update: Dict[str, Any]) -> None:
        usage = update.get("usage", update)
        cost = usage.get("totalCostUsd") or usage.get("cost")
        if cost is not None:
            self._total_cost_usd = float(cost)

        context_size = update.get("size", 0)
        context_used = update.get("used", 0)
        context_pct = (context_used / context_size * 100) if context_size else usage.get("contextUsedPercent", 0)

        self._emit({"type": "session_update", "session": {
            "total_cost_usd": self._total_cost_usd,
            "context_used_percent": context_pct,
        }})

    def _handle_permission_request(self, rpc_id: Any, params: Dict[str, Any]) -> None:
        tool_call = params.get("toolCall", {})
        options = params.get("options", [])

        request_id = str(uuid.uuid4())
        self._pending_approvals[request_id] = rpc_id

        tool_name = tool_call.get("title", "unknown_tool")
        raw_input = tool_call.get("rawInput", {})
        description = ""
        if isinstance(raw_input, dict):
            description = raw_input.get("command", raw_input.get("description", ""))

        perm: PermissionRequest = {
            "type": "permission_request",
            "request_id": request_id,
            "tool_name": tool_name,
            "input": raw_input if isinstance(raw_input, dict) else {"input": str(raw_input)},
            "description": description or f"Hermes wants to use: {tool_name}",
            "_acp_options": options,
        }
        self._emit(perm)

    # ---- Prompt response handling --------------------------------------------

    def _handle_prompt_response(self, result: Dict[str, Any]) -> None:
        stop_reason = result.get("stopReason", "end_turn")
        self._flush_streaming_text()
        self._finish_turn(is_error=stop_reason == "refusal")

    def _finish_turn(self, is_error: bool = False, error_msg: str = "") -> None:
        self._is_running = False
        duration_ms = int((time.monotonic() - self._turn_start_time) * 1000) if self._turn_start_time else 0
        self._num_turns += 1
        self._emitted_tool_use_ids.clear()
        self._tool_call_last_status.clear()
        # Close any text/thinking block still open from streaming deltas.
        self._close_open_block()

        result: CLIResultMessage = {
            "type": "result",
            "data": {
                "subtype": "error_max_turns" if is_error else "success",
                "is_error": is_error,
                "total_cost_usd": self._total_cost_usd,
                "num_turns": self._num_turns,
                "duration_ms": duration_ms,
                "errors": [error_msg] if error_msg else [],
            },
        }
        self._emit(result)
        self._emit({"type": "stream_event", "event": {"type": "message_stop"}})

    # ---- Helpers -------------------------------------------------------------

    def _flush_streaming_text(self) -> None:
        if self._streaming_text:
            self._msg_counter += 1
            msg_id = f"hermes-msg-{self._msg_counter}"
            self._emit({
                "type": "assistant",
                "msg_id": msg_id,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": self._streaming_text}],
                },
            })
            self._streaming_text = ""

    def _emit(self, msg: dict) -> None:
        if self._browser_message_cb is not None:
            result = self._browser_message_cb(msg)
            if asyncio.iscoroutine(result):
                asyncio.ensure_future(result)
