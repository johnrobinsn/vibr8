"""Codex App-Server Adapter

Translates between the Codex app-server JSON-RPC protocol (stdin/stdout)
and vibr8's BrowserIncomingMessage/BrowserOutgoingMessage types.

This allows the browser to be completely unaware of which backend is running --
it sees the same message types regardless of whether Claude Code or Codex is
the backend.

Originally ported from The Vibe Companion (codex-adapter.ts).
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


# ---- Codex JSON-RPC Types (internal) ----------------------------------------

# These mirror the TypeScript interfaces but are plain dicts at runtime.
# We use type aliases for documentation purposes only.

JsonRpcRequest = Dict[str, Any]  # {method, id, params}
JsonRpcNotification = Dict[str, Any]  # {method, params}
JsonRpcResponse = Dict[str, Any]  # {id, result?, error?}
JsonRpcMessage = Dict[str, Any]

# Codex item types are also plain dicts.
CodexItem = Dict[str, Any]


# ---- Adapter Options ---------------------------------------------------------

@dataclass
class CodexAdapterOptions:
    model: Optional[str] = None
    cwd: Optional[str] = None
    approval_mode: Optional[str] = None
    thread_id: Optional[str] = None  # If provided, resume an existing thread


# ---- JSON-RPC Transport ------------------------------------------------------

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
        self._notification_handler: Optional[
            Callable[[str, Dict[str, Any]], None]
        ] = None
        self._request_handler: Optional[
            Callable[[str, int, Dict[str, Any]], None]
        ] = None
        self._connected: bool = True
        self._buffer: str = ""

        # Start the background reader
        self._reader_task: asyncio.Task[None] = asyncio.create_task(
            self._read_stdout()
        )

    # -- Reading ---------------------------------------------------------------

    async def _read_stdout(self) -> None:
        try:
            while True:
                data = await self._stdout.read(65536)
                if not data:
                    break
                self._buffer += data.decode("utf-8", errors="replace")
                self._process_buffer()
        except Exception as exc:
            logger.error("[codex-adapter] stdout reader error: %s", exc)
        finally:
            self._connected = False

    def _process_buffer(self) -> None:
        lines = self._buffer.split("\n")
        # Keep the last incomplete line in the buffer
        self._buffer = lines.pop() if lines else ""

        for line in lines:
            trimmed = line.strip()
            if not trimmed:
                continue

            try:
                msg: JsonRpcMessage = json.loads(trimmed)
            except json.JSONDecodeError:
                logger.warning(
                    "[codex-adapter] Failed to parse JSON-RPC: %s",
                    trimmed[:200],
                )
                continue

            self._dispatch(msg)

    def _dispatch(self, msg: JsonRpcMessage) -> None:
        if "id" in msg and msg["id"] is not None:
            if "method" in msg and msg["method"]:
                # This is a request FROM the server (e.g., approval request)
                if self._request_handler is not None:
                    self._request_handler(
                        msg["method"],
                        msg["id"],
                        msg.get("params", {}),
                    )
            else:
                # This is a response to one of our requests
                rpc_id: int = msg["id"]
                future = self._pending.pop(rpc_id, None)
                if future is not None and not future.done():
                    error = msg.get("error")
                    if error:
                        future.set_exception(
                            RuntimeError(error.get("message", "Unknown RPC error"))
                        )
                    else:
                        future.set_result(msg.get("result"))
        elif "method" in msg:
            # Notification (no id)
            if self._notification_handler is not None:
                self._notification_handler(msg["method"], msg.get("params", {}))

    # -- Writing ---------------------------------------------------------------

    async def call(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Send a request and wait for the matching response."""
        rpc_id = self._next_id
        self._next_id += 1

        # Register the future BEFORE writing so that if the response arrives
        # during the write await, the dispatch handler can resolve it.
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[rpc_id] = future

        request = json.dumps({"method": method, "id": rpc_id, "params": params or {}})
        await self._write_raw(request + "\n")
        return await future

    async def notify(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Send a notification (no response expected)."""
        notification = json.dumps({"method": method, "params": params or {}})
        await self._write_raw(notification + "\n")

    async def respond(self, rpc_id: int, result: Any) -> None:
        """Respond to a request from the server (e.g., approval)."""
        response = json.dumps({"id": rpc_id, "result": result})
        await self._write_raw(response + "\n")

    # -- Handler registration --------------------------------------------------

    def on_notification(
        self,
        handler: Callable[[str, Dict[str, Any]], None],
    ) -> None:
        """Register handler for server-initiated notifications."""
        self._notification_handler = handler

    def on_request(
        self,
        handler: Callable[[str, int, Dict[str, Any]], None],
    ) -> None:
        """Register handler for server-initiated requests (need a response)."""
        self._request_handler = handler

    @property
    def connected(self) -> bool:
        return self._connected

    # -- Internal --------------------------------------------------------------

    async def _write_raw(self, data: str) -> None:
        self._stdin.write(data.encode("utf-8"))
        await self._stdin.drain()


# ---- Codex Adapter -----------------------------------------------------------

class CodexAdapter:
    """Orchestrates a Codex session, translating between the JSON-RPC
    protocol and BrowserIncomingMessage / BrowserOutgoingMessage types.
    """

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        session_id: str,
        options: Optional[CodexAdapterOptions] = None,
    ) -> None:
        self._proc = proc
        self._session_id = session_id
        self._options = options or CodexAdapterOptions()

        # Callbacks
        self._browser_message_cb: Optional[Callable[[dict], None]] = None
        self._session_meta_cb: Optional[
            Callable[[Dict[str, Optional[str]]], None]
        ] = None
        self._disconnect_cb: Optional[Callable[[], Any]] = None
        self._init_error_cb: Optional[Callable[[str], None]] = None

        # State
        self._thread_id: Optional[str] = None
        self._current_turn_id: Optional[str] = None
        self._connected: bool = False
        self._initialized: bool = False

        # Streaming accumulator for agent messages
        self._streaming_text: str = ""
        self._streaming_item_id: Optional[str] = None

        # Synthesized message counter
        self._msg_counter: int = 0

        # Accumulate reasoning text by item ID
        self._reasoning_text_by_item_id: Dict[str, str] = {}

        # Track which item IDs we have already emitted a tool_use block for.
        self._emitted_tool_use_ids: set[str] = set()

        # Queue messages received before initialization completes
        self._pending_outgoing: List[BrowserOutgoingMessage] = []

        # Pending approval requests (request_id -> JSON-RPC id)
        self._pending_approvals: Dict[str, int] = {}

        # Validate stdio pipes
        if proc.stdout is None or proc.stdin is None:
            raise RuntimeError("Codex process must have stdio pipes")

        self._transport = JsonRpcTransport(proc.stdin, proc.stdout)
        self._transport.on_notification(self._handle_notification)
        self._transport.on_request(self._handle_request)

        # Monitor process exit in the background
        self._exit_task: asyncio.Task[None] = asyncio.create_task(
            self._monitor_exit()
        )

        # Start initialization
        self._init_task: asyncio.Task[None] = asyncio.create_task(
            self._initialize()
        )

    async def _monitor_exit(self) -> None:
        await self._proc.wait()
        self._connected = False
        if self._disconnect_cb is not None:
            result = self._disconnect_cb()
            if asyncio.iscoroutine(result):
                await result

    # ---- Public API ----------------------------------------------------------

    def send_browser_message(self, msg: BrowserOutgoingMessage) -> bool:
        """Handle an outgoing browser message. Returns True if accepted."""
        # Queue messages if not yet initialized (init is async)
        if not self._initialized or not self._thread_id:
            msg_type = msg.get("type")  # type: ignore[union-attr]
            if msg_type in ("user_message", "permission_response"):
                logger.info(
                    "[codex-adapter] Queuing %s -- adapter not yet initialized",
                    msg_type,
                )
                self._pending_outgoing.append(msg)
                return True  # accepted, will be sent after init
            # Non-queueable messages are dropped if not connected
            if not self._connected:
                return False

        return self._dispatch_outgoing(msg)

    def _dispatch_outgoing(self, msg: BrowserOutgoingMessage) -> bool:
        msg_type = msg.get("type")  # type: ignore[union-attr]
        if msg_type == "user_message":
            asyncio.create_task(self._handle_outgoing_user_message(msg))  # type: ignore[arg-type]
            return True
        elif msg_type == "permission_response":
            asyncio.create_task(self._handle_outgoing_permission_response(msg))  # type: ignore[arg-type]
            return True
        elif msg_type == "interrupt":
            asyncio.create_task(self._handle_outgoing_interrupt())
            return True
        elif msg_type == "set_model":
            logger.warning(
                "[codex-adapter] Runtime model switching not supported by Codex"
            )
            return False
        elif msg_type == "set_permission_mode":
            logger.warning(
                "[codex-adapter] Runtime permission mode switching not supported by Codex"
            )
            return False
        else:
            return False

    def on_browser_message(self, cb: Callable[[dict], None]) -> None:
        self._browser_message_cb = cb

    def on_session_meta(
        self,
        cb: Callable[[Dict[str, Optional[str]]], None],
    ) -> None:
        self._session_meta_cb = cb

    def on_disconnect(self, cb: Callable[[], None]) -> None:
        self._disconnect_cb = cb

    def on_init_error(self, cb: Callable[[str], None]) -> None:
        self._init_error_cb = cb

    @property
    def connected(self) -> bool:
        return self._connected

    async def disconnect(self) -> None:
        self._connected = False
        try:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._proc.kill()
        except Exception:
            pass

    @property
    def thread_id(self) -> Optional[str]:
        return self._thread_id

    # ---- Initialization ------------------------------------------------------

    async def _initialize(self) -> None:
        try:
            # Step 1: Send initialize request
            result = await self._transport.call("initialize", {
                "clientInfo": {
                    "name": "vibr8",
                    "title": "vibr8",
                    "version": "1.0.0",
                },
                "capabilities": {
                    "experimentalApi": False,
                },
            })

            # Step 2: Send initialized notification
            await self._transport.notify("initialized", {})

            self._connected = True
            self._initialized = True

            # Step 3: Start or resume a thread
            if self._options.thread_id:
                # Resume an existing thread
                resume_result = await self._transport.call("thread/resume", {
                    "threadId": self._options.thread_id,
                    "model": self._options.model,
                    "cwd": self._options.cwd,
                    "approvalPolicy": self._map_approval_policy(
                        self._options.approval_mode
                    ),
                    "sandbox": "workspace-write",
                })
                self._thread_id = resume_result["thread"]["id"]
            else:
                # Start a new thread
                thread_result = await self._transport.call("thread/start", {
                    "model": self._options.model,
                    "cwd": self._options.cwd,
                    "approvalPolicy": self._map_approval_policy(
                        self._options.approval_mode
                    ),
                    "sandbox": "workspace-write",
                })
                self._thread_id = thread_result["thread"]["id"]

            # Notify session metadata
            if self._session_meta_cb is not None:
                self._session_meta_cb({
                    "cliSessionId": self._thread_id,
                    "model": self._options.model,
                    "cwd": self._options.cwd,
                })

            # Send session_init to browser
            state: SessionState = {
                "session_id": self._session_id,
                "backend_type": "codex",
                "model": self._options.model or "",
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

            # Flush any messages that were queued during initialization
            if self._pending_outgoing:
                logger.info(
                    "[codex-adapter] Flushing %d queued message(s)",
                    len(self._pending_outgoing),
                )
                queued = self._pending_outgoing[:]
                self._pending_outgoing.clear()
                for queued_msg in queued:
                    self._dispatch_outgoing(queued_msg)

        except Exception as exc:
            error_msg = f"Codex initialization failed: {exc}"
            logger.error("[codex-adapter] %s", error_msg)
            self._emit({"type": "error", "message": error_msg})
            self._connected = False
            if self._init_error_cb is not None:
                self._init_error_cb(error_msg)

    # ---- Outgoing message handlers -------------------------------------------

    async def _handle_outgoing_user_message(self, msg: dict) -> None:
        if not self._thread_id:
            self._emit({"type": "error", "message": "No Codex thread started yet"})
            return

        input_parts: List[Dict[str, Any]] = []

        # Add images if present
        images = msg.get("images")
        if images:
            for img in images:
                input_parts.append({
                    "type": "image",
                    "url": f"data:{img['media_type']};base64,{img['data']}",
                })

        # Add text
        input_parts.append({"type": "text", "text": msg["content"]})

        try:
            result = await self._transport.call("turn/start", {
                "threadId": self._thread_id,
                "input": input_parts,
                "cwd": self._options.cwd,
            })
            self._current_turn_id = result["turn"]["id"]
        except Exception as exc:
            self._emit({"type": "error", "message": f"Failed to start turn: {exc}"})

    async def _handle_outgoing_permission_response(self, msg: dict) -> None:
        request_id: str = msg["request_id"]
        json_rpc_id = self._pending_approvals.get(request_id)
        if json_rpc_id is None:
            logger.warning(
                "[codex-adapter] No pending approval for request_id=%s",
                request_id,
            )
            return

        del self._pending_approvals[request_id]

        decision = "accept" if msg["behavior"] == "allow" else "decline"
        await self._transport.respond(json_rpc_id, {"decision": decision})

    async def _handle_outgoing_interrupt(self) -> None:
        if not self._thread_id or not self._current_turn_id:
            return

        try:
            await self._transport.call("turn/interrupt", {
                "threadId": self._thread_id,
                "turnId": self._current_turn_id,
            })
        except Exception as exc:
            logger.warning("[codex-adapter] Interrupt failed: %s", exc)

    # ---- Incoming notification handlers --------------------------------------

    def _handle_notification(
        self,
        method: str,
        params: Dict[str, Any],
    ) -> None:
        # Debug: log all significant notifications
        if (
            method.startswith("item/")
            or method.startswith("turn/")
            or method.startswith("thread/")
        ):
            item = params.get("item")
            if item and isinstance(item, dict):
                logger.info(
                    "[codex-adapter] <- %s type=%s id=%s",
                    method,
                    item.get("type"),
                    item.get("id"),
                )
            elif params:
                logger.info(
                    "[codex-adapter] <- %s keys=[%s]",
                    method,
                    ",".join(params.keys()),
                )
            else:
                logger.info("[codex-adapter] <- %s", method)

        try:
            if method == "item/started":
                self._handle_item_started(params)
            elif method == "item/agentMessage/delta":
                self._handle_agent_message_delta(params)
            elif method == "item/commandExecution/outputDelta":
                # Streaming command output -- not critical for rendering
                pass
            elif method == "item/fileChange/outputDelta":
                # Streaming file change output
                pass
            elif method in (
                "item/reasoning/textDelta",
                "item/reasoning/summaryTextDelta",
                "item/reasoning/summaryPartAdded",
            ):
                self._handle_reasoning_delta(params)
            elif method == "item/mcpToolCall/progress":
                # MCP tool call progress
                pass
            elif method == "item/plan/delta":
                # Plan updates
                pass
            elif method == "item/updated":
                self._handle_item_updated(params)
            elif method == "item/completed":
                self._handle_item_completed(params)
            elif method == "rawResponseItem/completed":
                # Raw model response -- internal
                pass
            elif method == "turn/started":
                # Turn started, nothing to emit
                pass
            elif method == "turn/completed":
                self._handle_turn_completed(params)
            elif method == "turn/plan/updated":
                pass
            elif method == "turn/diff/updated":
                pass
            elif method == "thread/started":
                pass
            elif method == "thread/tokenUsage/updated":
                self._handle_token_usage_updated(params)
            elif method in ("account/updated", "account/login/completed"):
                pass
            elif method in ("error", "warning"):
                logger.warning(
                    "[codex-adapter] %s: %s", method, params
                )
            else:
                if not method.startswith("account/") and not method.startswith(
                    "codex/event/"
                ):
                    logger.info(
                        "[codex-adapter] Unhandled notification: %s params=%s",
                        method, params,
                    )
        except Exception as exc:
            logger.error(
                "[codex-adapter] Error handling notification %s: %s",
                method,
                exc,
            )

    # ---- Incoming request handlers (approval requests) -----------------------

    def _handle_request(
        self,
        method: str,
        rpc_id: int,
        params: Dict[str, Any],
    ) -> None:
        try:
            if method == "item/commandExecution/requestApproval":
                self._handle_command_approval(rpc_id, params)
            elif method == "item/fileChange/requestApproval":
                self._handle_file_change_approval(rpc_id, params)
            elif method == "item/mcpToolCall/requestApproval":
                self._handle_mcp_tool_call_approval(rpc_id, params)
            elif method == "mcpServer/elicitation/request":
                self._handle_mcp_elicitation(rpc_id, params)
            else:
                logger.info("[codex-adapter] Unhandled request: %s", method)
                asyncio.create_task(
                    self._transport.respond(rpc_id, {"decision": "accept"})
                )
        except Exception as exc:
            logger.error(
                "[codex-adapter] Error handling request %s: %s",
                method,
                exc,
            )

    def _handle_command_approval(
        self,
        json_rpc_id: int,
        params: Dict[str, Any],
    ) -> None:
        request_id = f"codex-approval-{uuid.uuid4()}"
        self._pending_approvals[request_id] = json_rpc_id

        command = params.get("command")
        parsed_cmd = params.get("parsedCmd", "")
        if parsed_cmd:
            command_str = str(parsed_cmd)
        elif isinstance(command, list):
            command_str = " ".join(command)
        elif command is not None:
            command_str = str(command)
        else:
            command_str = ""

        perm: PermissionRequest = {
            "request_id": request_id,
            "tool_name": "Bash",
            "input": {
                "command": command_str,
                "cwd": params.get("cwd") or self._options.cwd or "",
            },
            "description": params.get("reason") or f"Execute: {command_str}",
            "tool_use_id": params.get("itemId") or request_id,
            "timestamp": time.time() * 1000,  # ms like Date.now()
        }

        self._emit({"type": "permission_request", "request": perm})

    def _handle_file_change_approval(
        self,
        json_rpc_id: int,
        params: Dict[str, Any],
    ) -> None:
        request_id = f"codex-approval-{uuid.uuid4()}"
        self._pending_approvals[request_id] = json_rpc_id

        # Extract file paths from changes array if available
        changes = params.get("changes") or []
        file_paths = [c.get("path") for c in changes if c.get("path")]
        file_list = ", ".join(file_paths) if file_paths else None

        input_data: Dict[str, Any] = {
            "description": params.get("reason") or "File changes pending approval",
        }
        if file_paths:
            input_data["file_paths"] = file_paths
        if changes:
            input_data["changes"] = changes

        description = params.get("reason")
        if not description:
            if file_list:
                description = f"Codex wants to modify: {file_list}"
            else:
                description = "Codex wants to modify files"

        perm: PermissionRequest = {
            "request_id": request_id,
            "tool_name": "Edit",
            "input": input_data,
            "description": description,
            "tool_use_id": params.get("itemId") or request_id,
            "timestamp": time.time() * 1000,
        }

        self._emit({"type": "permission_request", "request": perm})

    def _handle_mcp_tool_call_approval(
        self,
        json_rpc_id: int,
        params: Dict[str, Any],
    ) -> None:
        request_id = f"codex-approval-{uuid.uuid4()}"
        self._pending_approvals[request_id] = json_rpc_id

        server = params.get("server") or "unknown"
        tool = params.get("tool") or "unknown"
        args = params.get("arguments") or {}

        perm: PermissionRequest = {
            "request_id": request_id,
            "tool_name": f"mcp:{server}:{tool}",
            "input": args,
            "description": (
                params.get("reason") or f"MCP tool call: {server}/{tool}"
            ),
            "tool_use_id": params.get("itemId") or request_id,
            "timestamp": time.time() * 1000,
        }

        self._emit({"type": "permission_request", "request": perm})

    def _handle_mcp_elicitation(
        self,
        json_rpc_id: int,
        params: Dict[str, Any],
    ) -> None:
        logger.info(
            "[codex-adapter] Auto-approving MCP elicitation: message=%s",
            str(params.get("message", ""))[:100],
        )
        asyncio.create_task(
            self._transport.respond(json_rpc_id, {"action": "accept"})
        )

    # ---- Item event handlers -------------------------------------------------

    def _handle_item_started(self, params: Dict[str, Any]) -> None:
        item: Optional[CodexItem] = params.get("item")
        if not item:
            return

        item_type = item.get("type")
        item_id = item.get("id", "")

        if item_type == "agentMessage":
            # Start streaming accumulation
            self._streaming_item_id = item_id
            self._streaming_text = ""
            # Emit message_start stream event
            self._msg_counter += 1
            self._emit({
                "type": "stream_event",
                "event": {
                    "type": "message_start",
                    "message": {
                        "id": f"codex-msg-{self._msg_counter}",
                        "type": "message",
                        "role": "assistant",
                        "model": self._options.model or "",
                        "content": [],
                        "stop_reason": None,
                        "usage": {
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                        },
                    },
                },
                "parent_tool_use_id": None,
            })
            # Also emit content_block_start
            self._emit({
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                "parent_tool_use_id": None,
            })

        elif item_type == "commandExecution":
            command = item.get("command", "")
            if isinstance(command, list):
                command_str = " ".join(command)
            else:
                command_str = str(command) if command else ""
            self._emit_tool_use_start(item_id, "Bash", {"command": command_str})

        elif item_type == "fileChange":
            changes = item.get("changes") or []
            first_change = changes[0] if changes else {}
            tool_name = "Write" if first_change.get("kind") == "create" else "Edit"
            tool_input: Dict[str, Any] = {
                "file_path": first_change.get("path", ""),
                "changes": [
                    {"path": c.get("path", ""), "kind": c.get("kind", "")}
                    for c in changes
                ],
            }
            self._emit_tool_use_start(item_id, tool_name, tool_input)

        elif item_type == "mcpToolCall":
            server = item.get("server", "")
            tool = item.get("tool", "")
            self._emit_tool_use_start(
                item_id,
                f"mcp:{server}:{tool}",
                item.get("arguments") or {},
            )

        elif item_type == "webSearch":
            self._emit_tool_use_start(
                item_id,
                "WebSearch",
                {"query": item.get("query", "")},
            )

        elif item_type == "reasoning":
            summary = item.get("summary") or item.get("content") or ""
            self._reasoning_text_by_item_id[item_id] = summary
            # Emit as thinking content block
            if summary:
                self._emit({
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "thinking", "thinking": summary},
                    },
                    "parent_tool_use_id": None,
                })

        elif item_type == "contextCompaction":
            self._emit({"type": "status_change", "status": "compacting"})

        else:
            # userMessage is an echo of browser input and not needed in UI.
            if item_type != "userMessage":
                logger.info(
                    "[codex-adapter] Unhandled item/started type: %s %s",
                    item_type,
                    json.dumps(item)[:300],
                )

    def _handle_reasoning_delta(self, params: Dict[str, Any]) -> None:
        item_id = params.get("itemId")
        if not item_id:
            return

        if item_id not in self._reasoning_text_by_item_id:
            self._reasoning_text_by_item_id[item_id] = ""

        delta = params.get("delta")
        if delta:
            self._reasoning_text_by_item_id[item_id] += str(delta)

    def _handle_agent_message_delta(self, params: Dict[str, Any]) -> None:
        delta = params.get("delta")
        if not delta:
            return

        delta_str = str(delta)
        self._streaming_text += delta_str

        # Emit as content_block_delta (matches Claude's streaming format)
        self._emit({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": delta_str},
            },
            "parent_tool_use_id": None,
        })

    def _handle_item_updated(self, params: Dict[str, Any]) -> None:
        # item/updated is a general update -- currently handled via specific delta events
        pass

    def _handle_item_completed(self, params: Dict[str, Any]) -> None:
        item: Optional[CodexItem] = params.get("item")
        if not item:
            return

        item_type = item.get("type")
        item_id = item.get("id", "")

        if item_type == "agentMessage":
            text = item.get("text") or self._streaming_text

            # Emit message_stop for streaming
            self._emit({
                "type": "stream_event",
                "event": {
                    "type": "content_block_stop",
                    "index": 0,
                },
                "parent_tool_use_id": None,
            })
            self._emit({
                "type": "stream_event",
                "event": {
                    "type": "message_delta",
                    "delta": {"stop_reason": None},
                    "usage": {"output_tokens": 0},
                },
                "parent_tool_use_id": None,
            })

            # Emit the full assistant message
            self._msg_counter += 1
            self._emit({
                "type": "assistant",
                "message": {
                    "id": f"codex-msg-{self._msg_counter}",
                    "type": "message",
                    "role": "assistant",
                    "model": self._options.model or "",
                    "content": [{"type": "text", "text": text}],
                    "stop_reason": "end_turn",
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
                "parent_tool_use_id": None,
            })

            # Reset streaming state
            self._streaming_text = ""
            self._streaming_item_id = None

        elif item_type == "commandExecution":
            command = item.get("command", "")
            if isinstance(command, list):
                command_str = " ".join(command)
            else:
                command_str = str(command) if command else ""
            # Ensure tool_use was emitted (may be skipped when auto-approved)
            self._ensure_tool_use_emitted(item_id, "Bash", {"command": command_str})
            # Emit tool result
            stdout_text = item.get("stdout", "") or ""
            stderr_text = item.get("stderr", "") or ""
            combined_output = "\n".join(
                part for part in [stdout_text, stderr_text] if part
            ).strip()
            exit_code = item.get("exitCode")
            exit_code = exit_code if isinstance(exit_code, int) else 0
            status = item.get("status", "")
            failed = status in ("failed", "declined") or exit_code != 0

            # Avoid noisy placeholder output for successful commands with no stdout/stderr.
            if not combined_output and not failed:
                return

            if not combined_output:
                result_text = f"Exit code: {exit_code}"
            elif exit_code != 0:
                result_text = f"{combined_output}\nExit code: {exit_code}"
            else:
                result_text = combined_output

            self._emit_tool_result(item_id, result_text, failed)

        elif item_type == "fileChange":
            changes = item.get("changes") or []
            first_change = changes[0] if changes else {}
            tool_name = "Write" if first_change.get("kind") == "create" else "Edit"
            # Ensure tool_use was emitted
            self._ensure_tool_use_emitted(item_id, tool_name, {
                "file_path": first_change.get("path", ""),
                "changes": [
                    {"path": c.get("path", ""), "kind": c.get("kind", "")}
                    for c in changes
                ],
            })
            summary = "\n".join(
                f"{c.get('kind', '')}: {c.get('path', '')}" for c in changes
            )
            self._emit_tool_result(
                item_id,
                summary or "File changes applied",
                item.get("status") == "failed",
            )

        elif item_type == "mcpToolCall":
            server = item.get("server", "")
            tool = item.get("tool", "")
            # Ensure tool_use was emitted
            self._ensure_tool_use_emitted(
                item_id,
                f"mcp:{server}:{tool}",
                item.get("arguments") or {},
            )
            result_content = (
                item.get("result")
                or item.get("error")
                or "MCP tool call completed"
            )
            self._emit_tool_result(
                item_id,
                result_content,
                item.get("status") == "failed",
            )

        elif item_type == "webSearch":
            # Ensure tool_use was emitted
            self._ensure_tool_use_emitted(
                item_id,
                "WebSearch",
                {"query": item.get("query", "")},
            )
            action = item.get("action") or {}
            result_content = (
                action.get("url")
                or item.get("query")
                or "Web search completed"
            )
            self._emit_tool_result(item_id, result_content, False)

        elif item_type == "reasoning":
            thinking_text = (
                self._reasoning_text_by_item_id.get(item_id, "")
                or item.get("summary", "")
                or item.get("content", "")
            ).strip()

            if thinking_text:
                self._msg_counter += 1
                self._emit({
                    "type": "assistant",
                    "message": {
                        "id": f"codex-msg-{self._msg_counter}",
                        "type": "message",
                        "role": "assistant",
                        "model": self._options.model or "",
                        "content": [{"type": "thinking", "thinking": thinking_text}],
                        "stop_reason": None,
                        "usage": {
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                        },
                    },
                    "parent_tool_use_id": None,
                })

            self._reasoning_text_by_item_id.pop(item_id, None)

            # Close the thinking content block that was opened in _handle_item_started
            self._emit({
                "type": "stream_event",
                "event": {
                    "type": "content_block_stop",
                    "index": 0,
                },
                "parent_tool_use_id": None,
            })

        elif item_type == "contextCompaction":
            self._emit({"type": "status_change", "status": None})

        else:
            if item_type != "userMessage":
                logger.info(
                    "[codex-adapter] Unhandled item/completed type: %s %s",
                    item_type,
                    json.dumps(item)[:300],
                )

    def _handle_turn_completed(self, params: Dict[str, Any]) -> None:
        turn = params.get("turn") or {}

        # Synthesize a CLIResultMessage-like structure
        turn_status = turn.get("status", "")
        turn_error = turn.get("error", {})

        result: CLIResultMessage = {
            "type": "result",
            "subtype": (
                "success" if turn_status == "completed" else "error_during_execution"
            ),
            "is_error": turn_status != "completed",
            "duration_ms": 0,
            "duration_api_ms": 0,
            "num_turns": 1,
            "total_cost_usd": 0,
            "stop_reason": turn_status or "end_turn",
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            "uuid": str(uuid.uuid4()),
            "session_id": self._session_id,
        }

        # Add error result text if present
        error_message = turn_error.get("message") if isinstance(turn_error, dict) else None
        if error_message:
            result["result"] = error_message

        self._emit({"type": "result", "data": result})
        self._current_turn_id = None

    def _handle_token_usage_updated(self, params: Dict[str, Any]) -> None:
        token_usage = params.get("tokenUsage")
        if not token_usage or not isinstance(token_usage, dict):
            return

        total = token_usage.get("total")
        context_window = token_usage.get("modelContextWindow")

        updates: Dict[str, Any] = {}

        if (
            isinstance(total, dict)
            and isinstance(context_window, (int, float))
            and context_window > 0
        ):
            used = (total.get("inputTokens", 0) or 0) + (
                total.get("outputTokens", 0) or 0
            )
            pct = round((used / context_window) * 100)
            updates["context_used_percent"] = max(0, min(pct, 100))

        if updates:
            self._emit({
                "type": "session_update",
                "session": updates,
            })

    # ---- Helpers -------------------------------------------------------------

    def _emit(self, msg: dict) -> None:
        if self._browser_message_cb is not None:
            result = self._browser_message_cb(msg)
            if asyncio.iscoroutine(result):
                asyncio.ensure_future(result)

    def _emit_tool_use(
        self,
        tool_use_id: str,
        tool_name: str,
        input_data: Dict[str, Any],
    ) -> None:
        """Emit an assistant message with a tool_use content block (no tracking)."""
        logger.info(
            "[codex-adapter] Emitting tool_use: %s id=%s", tool_name, tool_use_id
        )
        self._msg_counter += 1
        self._emit({
            "type": "assistant",
            "message": {
                "id": f"codex-msg-{self._msg_counter}",
                "type": "message",
                "role": "assistant",
                "model": self._options.model or "",
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_use_id,
                        "name": tool_name,
                        "input": input_data,
                    },
                ],
                "stop_reason": None,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
            "parent_tool_use_id": None,
        })

    def _emit_tool_use_tracked(
        self,
        tool_use_id: str,
        tool_name: str,
        input_data: Dict[str, Any],
    ) -> None:
        """Emit tool_use and track the ID so we don't double-emit."""
        self._emitted_tool_use_ids.add(tool_use_id)
        self._emit_tool_use(tool_use_id, tool_name, input_data)

    def _emit_tool_use_start(
        self,
        tool_use_id: str,
        tool_name: str,
        input_data: Dict[str, Any],
    ) -> None:
        """Emit a tool_use start sequence: stream_event content_block_start + assistant message.

        This matches Claude Code's streaming pattern and ensures the frontend sees
        the tool block even during active streaming.
        """
        # Emit stream event for tool_use start (matches Claude Code pattern)
        self._emit({
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": tool_name,
                    "input": {},
                },
            },
            "parent_tool_use_id": None,
        })
        self._emit_tool_use_tracked(tool_use_id, tool_name, input_data)

    def _ensure_tool_use_emitted(
        self,
        tool_use_id: str,
        tool_name: str,
        input_data: Dict[str, Any],
    ) -> None:
        """Emit tool_use only if item/started was never received for this ID."""
        if tool_use_id not in self._emitted_tool_use_ids:
            logger.info(
                "[codex-adapter] Backfilling tool_use for %s (id=%s) -- item/started was missing",
                tool_name,
                tool_use_id,
            )
            self._emit_tool_use_tracked(tool_use_id, tool_name, input_data)

    def _emit_tool_result(
        self,
        tool_use_id: str,
        content: str,
        is_error: bool,
    ) -> None:
        """Emit an assistant message with a tool_result content block."""
        self._msg_counter += 1
        self._emit({
            "type": "assistant",
            "message": {
                "id": f"codex-msg-{self._msg_counter}",
                "type": "message",
                "role": "assistant",
                "model": self._options.model or "",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": content,
                        "is_error": is_error,
                    },
                ],
                "stop_reason": None,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
            "parent_tool_use_id": None,
        })

    @staticmethod
    def _map_approval_policy(mode: Optional[str]) -> str:
        if mode == "bypassPermissions":
            return "never"
        # "plan", "acceptEdits", "default", or anything else
        return "unless-trusted"
