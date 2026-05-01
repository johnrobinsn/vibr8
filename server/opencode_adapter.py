"""OpenCode Server Adapter

Translates between OpenCode's HTTP/SSE server protocol and vibr8's
BrowserIncomingMessage/BrowserOutgoingMessage types.

OpenCode runs as a persistent subprocess (``opencode serve``). This adapter
communicates via REST for session management and message sending, and
subscribes to the SSE event stream (``GET /event``) for real-time streaming
of assistant responses, tool calls, permissions, and errors.

This allows the browser to be completely unaware of which backend is running --
it sees the same message types regardless of whether Claude Code, Codex, or
OpenCode is the backend.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from aiohttp import ClientSession, BasicAuth, ClientTimeout

from server.session_types import (
    BrowserOutgoingMessage,
    CLIResultMessage,
    PermissionRequest,
    SessionState,
)

logger = logging.getLogger(__name__)


@dataclass
class OpenCodeAdapterOptions:
    model: Optional[str] = None
    cwd: Optional[str] = None
    server_url: str = ""
    password: str = ""
    session_id: Optional[str] = None
    approval_mode: Optional[str] = None


class OpenCodeAdapter:
    """Orchestrates an OpenCode session, translating between the HTTP/SSE
    protocol and BrowserIncomingMessage / BrowserOutgoingMessage types.
    """

    def __init__(
        self,
        session_id: str,
        options: Optional[OpenCodeAdapterOptions] = None,
    ) -> None:
        self._session_id = session_id
        self._options = options or OpenCodeAdapterOptions()

        # Callbacks (same interface as CodexAdapter)
        self._browser_message_cb: Optional[Callable[[dict], None]] = None
        self._session_meta_cb: Optional[Callable[[Dict[str, Optional[str]]], None]] = None
        self._disconnect_cb: Optional[Callable[[], Any]] = None
        self._init_error_cb: Optional[Callable[[str], None]] = None

        # State
        self._opencode_session_id: Optional[str] = None
        self._connected: bool = False
        self._initialized: bool = False

        # Streaming accumulator
        self._streaming_text: str = ""
        self._msg_counter: int = 0

        # Part type tracking for delta disambiguation
        self._part_types: Dict[str, str] = {}

        # Tool dedup (same pattern as CodexAdapter)
        self._emitted_tool_use_ids: set[str] = set()

        # Pending permissions: vibr8 request_id → opencode permission ID
        self._pending_approvals: Dict[str, str] = {}

        # Queue messages received before initialization completes
        self._pending_outgoing: List[BrowserOutgoingMessage] = []

        # HTTP session
        self._http: Optional[ClientSession] = None
        self._sse_task: Optional[asyncio.Task[None]] = None

        # Start initialization
        self._init_task: asyncio.Task[None] = asyncio.create_task(
            self._initialize()
        )

    # ---- Public API ----------------------------------------------------------

    def send_browser_message(self, msg: BrowserOutgoingMessage) -> bool:
        if not self._initialized or not self._opencode_session_id:
            msg_type = msg.get("type")  # type: ignore[union-attr]
            if msg_type in ("user_message", "permission_response"):
                logger.info("[opencode] Queuing %s -- not yet initialized", msg_type)
                self._pending_outgoing.append(msg)
                return True
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
            logger.warning("[opencode] Runtime model switching not yet supported")
            return False
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
        if self._sse_task and not self._sse_task.done():
            self._sse_task.cancel()
        if self._http and not self._http.closed:
            await self._http.close()
        if self._disconnect_cb is not None:
            result = self._disconnect_cb()
            if asyncio.iscoroutine(result):
                await result

    # ---- HTTP helpers --------------------------------------------------------

    def _auth(self) -> BasicAuth:
        return BasicAuth("opencode", self._options.password)

    def _url(self, path: str) -> str:
        return f"{self._options.server_url}{path}"

    async def _get(self, path: str) -> Any:
        assert self._http
        async with self._http.get(self._url(path), auth=self._auth()) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"GET {path} failed ({resp.status}): {text[:200]}")
            return await resp.json()

    async def _post(self, path: str, data: Any = None, expect_json: bool = True) -> Any:
        assert self._http
        async with self._http.post(self._url(path), json=data, auth=self._auth()) as resp:
            if resp.status == 204:
                return None
            if resp.status not in (200, 201):
                text = await resp.text()
                raise RuntimeError(f"POST {path} failed ({resp.status}): {text[:200]}")
            if expect_json:
                return await resp.json()
            return await resp.text()

    # ---- Initialization ------------------------------------------------------

    async def _initialize(self) -> None:
        try:
            self._http = ClientSession(timeout=ClientTimeout(total=300))

            # Wait for server to be ready
            for attempt in range(30):
                try:
                    await self._get("/health")
                    break
                except Exception:
                    if attempt == 29:
                        raise RuntimeError("OpenCode server did not start within 30s")
                    await asyncio.sleep(1)

            # Create a new session
            session_info = await self._post("/session", {"title": f"vibr8-{self._session_id[:8]}"})
            self._opencode_session_id = session_info["id"]

            self._connected = True
            self._initialized = True

            # Start SSE subscription
            self._sse_task = asyncio.create_task(self._subscribe_sse())

            # Notify session metadata
            if self._session_meta_cb is not None:
                self._session_meta_cb({
                    "cliSessionId": self._opencode_session_id,
                    "model": self._options.model,
                    "cwd": self._options.cwd,
                })

            # Emit session_init to browser
            state: SessionState = {
                "session_id": self._session_id,
                "backend_type": "opencode",
                "model": self._options.model or "",
                "cwd": self._options.cwd or "",
                "tools": [],
                "permissionMode": self._options.approval_mode or "bypassPermissions",
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

            # Flush queued messages
            if self._pending_outgoing:
                logger.info("[opencode] Flushing %d queued message(s)", len(self._pending_outgoing))
                queued = self._pending_outgoing[:]
                self._pending_outgoing.clear()
                for queued_msg in queued:
                    self._dispatch_outgoing(queued_msg)

        except Exception as exc:
            error_msg = f"OpenCode initialization failed: {exc}"
            logger.error("[opencode] %s", error_msg)
            self._emit({"type": "error", "message": error_msg})
            self._connected = False
            if self._init_error_cb is not None:
                self._init_error_cb(error_msg)

    # ---- Outgoing message handlers -------------------------------------------

    async def _handle_outgoing_user_message(self, msg: dict) -> None:
        if not self._opencode_session_id:
            self._emit({"type": "error", "message": "No OpenCode session started yet"})
            return

        parts: List[Dict[str, Any]] = []

        # Add images
        images = msg.get("images")
        if images:
            for img in images:
                data_url = f"data:{img['media_type']};base64,{img['data']}"
                parts.append({"type": "file", "mime": img["media_type"], "url": data_url})

        # Add text
        parts.append({"type": "text", "text": msg["content"]})

        body: Dict[str, Any] = {"parts": parts}

        # Model override
        if self._options.model and "/" in self._options.model:
            provider_id, model_id = self._options.model.split("/", 1)
            body["model"] = {"providerID": provider_id, "modelID": model_id}

        try:
            # Use prompt_async for non-blocking send — streaming comes via SSE
            await self._post(
                f"/session/{self._opencode_session_id}/prompt_async",
                body,
                expect_json=False,
            )
        except Exception as exc:
            self._emit({"type": "error", "message": f"Failed to send prompt: {exc}"})

    async def _handle_outgoing_permission_response(self, msg: dict) -> None:
        request_id: str = msg["request_id"]
        opencode_perm_id = self._pending_approvals.get(request_id)
        if opencode_perm_id is None:
            logger.warning("[opencode] No pending approval for request_id=%s", request_id)
            return

        del self._pending_approvals[request_id]

        reply = "once" if msg["behavior"] == "allow" else "reject"
        try:
            await self._post(f"/permission/{opencode_perm_id}/reply", {"reply": reply})
        except Exception as exc:
            logger.warning("[opencode] Permission reply failed: %s", exc)

    async def _handle_outgoing_interrupt(self) -> None:
        if not self._opencode_session_id:
            return
        try:
            await self._post(f"/session/{self._opencode_session_id}/abort", {})
        except Exception as exc:
            logger.warning("[opencode] Interrupt/abort failed: %s", exc)

    # ---- SSE subscription ----------------------------------------------------

    async def _subscribe_sse(self) -> None:
        url = self._url("/event")
        try:
            assert self._http
            async with self._http.get(url, auth=self._auth(), timeout=ClientTimeout(total=0)) as resp:
                if resp.status != 200:
                    logger.error("[opencode] SSE connection failed: %d", resp.status)
                    return

                buffer = ""
                async for chunk in resp.content.iter_any():
                    if not self._connected:
                        return
                    buffer += chunk.decode("utf-8", errors="replace")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if not data_str:
                            continue
                        try:
                            event = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        self._dispatch_sse_event(event)

        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error("[opencode] SSE stream error: %s", exc)
        finally:
            if self._connected:
                self._connected = False
                if self._disconnect_cb is not None:
                    result = self._disconnect_cb()
                    if asyncio.iscoroutine(result):
                        await result

    def _dispatch_sse_event(self, event: dict) -> None:
        event_type = event.get("type", "")
        props = event.get("properties", {})

        # Filter to our session
        session_id = props.get("sessionID")
        if session_id and session_id != self._opencode_session_id:
            return

        try:
            if event_type == "message.part.delta":
                self._handle_part_delta(props)
            elif event_type == "message.part.updated":
                self._handle_part_updated(props)
            elif event_type == "message.updated":
                self._handle_message_updated(props)
            elif event_type == "permission.asked":
                self._handle_permission_asked(props)
            elif event_type == "session.status":
                self._handle_session_status(props)
            elif event_type == "session.error":
                self._handle_session_error(props)
            elif event_type == "server.instance.disposed":
                asyncio.create_task(self.disconnect())
            # server.heartbeat, server.connected: no action needed
        except Exception as exc:
            logger.error("[opencode] Error handling SSE event %s: %s", event_type, exc)

    # ---- SSE event handlers --------------------------------------------------

    def _handle_part_delta(self, props: dict) -> None:
        part_id = props.get("partID", "")
        delta = props.get("delta", "")
        if not delta:
            return

        part_type = self._part_types.get(part_id, "text")

        if part_type == "reasoning":
            self._emit({
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": delta},
                },
                "parent_tool_use_id": None,
            })
        else:
            self._streaming_text += delta
            self._emit({
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": delta},
                },
                "parent_tool_use_id": None,
            })

    def _handle_part_updated(self, props: dict) -> None:
        part = props.get("part", {})
        part_type = part.get("type", "")
        part_id = part.get("id", "")

        # Track part types for delta disambiguation
        if part_id:
            self._part_types[part_id] = part_type

        if part_type == "tool":
            state = part.get("state", {})
            status = state.get("status", "")
            call_id = part.get("callID", "")
            tool_name = part.get("tool", "")
            tool_input = state.get("input", {})

            if status in ("pending", "running"):
                self._emit_tool_use_start(call_id, tool_name, tool_input)
            elif status == "completed":
                self._ensure_tool_use_emitted(call_id, tool_name, tool_input)
                output = state.get("output", "")
                self._emit_tool_result(call_id, output, False)
            elif status == "error":
                self._ensure_tool_use_emitted(call_id, tool_name, tool_input)
                error = state.get("error", "Tool execution failed")
                self._emit_tool_result(call_id, error, True)

        elif part_type == "text":
            # Text part finalized — if it has text and we haven't been streaming, emit it
            pass  # Streaming is handled via message.part.delta

        elif part_type == "reasoning":
            text = part.get("text", "")
            if text:
                self._emit({
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "thinking", "thinking": text},
                    },
                    "parent_tool_use_id": None,
                })

        elif part_type == "step-finish":
            cost = part.get("cost", 0)
            tokens = part.get("tokens", {})
            updates: Dict[str, Any] = {}
            if cost:
                updates["total_cost_usd"] = cost
            if updates:
                self._emit({"type": "session_update", "session": updates})

        elif part_type == "compaction":
            self._emit({"type": "status_change", "status": "compacting"})

    def _handle_message_updated(self, props: dict) -> None:
        info = props.get("info", {})
        role = info.get("role", "")

        if role == "assistant":
            # Turn is complete
            text = self._streaming_text or ""

            if text:
                # Emit content_block_stop
                self._emit({
                    "type": "stream_event",
                    "event": {"type": "content_block_stop", "index": 0},
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

                # Emit full assistant message
                self._msg_counter += 1
                self._emit({
                    "type": "assistant",
                    "message": {
                        "id": f"opencode-msg-{self._msg_counter}",
                        "type": "message",
                        "role": "assistant",
                        "model": self._options.model or "",
                        "content": [{"type": "text", "text": text}],
                        "stop_reason": "end_turn",
                        "usage": {
                            "input_tokens": info.get("tokens", {}).get("input", 0),
                            "output_tokens": info.get("tokens", {}).get("output", 0),
                            "cache_creation_input_tokens": info.get("tokens", {}).get("cache", {}).get("write", 0),
                            "cache_read_input_tokens": info.get("tokens", {}).get("cache", {}).get("read", 0),
                        },
                    },
                    "parent_tool_use_id": None,
                })

            # Emit result (turn complete)
            error = info.get("error")
            is_error = error is not None and not (
                isinstance(error, dict) and error.get("name") == "MessageAbortedError"
            )

            result: CLIResultMessage = {
                "type": "result",
                "subtype": "error_during_execution" if is_error else "success",
                "is_error": is_error,
                "duration_ms": 0,
                "duration_api_ms": 0,
                "num_turns": 1,
                "total_cost_usd": info.get("cost", 0),
                "stop_reason": info.get("finish", "end_turn") or "end_turn",
                "usage": {
                    "input_tokens": info.get("tokens", {}).get("input", 0),
                    "output_tokens": info.get("tokens", {}).get("output", 0),
                    "cache_creation_input_tokens": info.get("tokens", {}).get("cache", {}).get("write", 0),
                    "cache_read_input_tokens": info.get("tokens", {}).get("cache", {}).get("read", 0),
                },
                "uuid": str(uuid.uuid4()),
                "session_id": self._session_id,
            }

            if error and isinstance(error, dict):
                error_data = error.get("data", {})
                result["result"] = error_data.get("message", str(error))

            self._emit({"type": "result", "data": result})

            # Reset streaming state
            self._streaming_text = ""
            self._part_types.clear()
            self._emitted_tool_use_ids.clear()

    def _handle_permission_asked(self, props: dict) -> None:
        perm_id = props.get("id", "")
        request_id = f"opencode-perm-{uuid.uuid4()}"
        self._pending_approvals[request_id] = perm_id

        permission = props.get("permission", "")
        metadata = props.get("metadata", {})
        patterns = props.get("patterns", [])
        tool_info = props.get("tool", {})

        # Build description from metadata or patterns
        if isinstance(metadata, dict):
            description = metadata.get("command") or metadata.get("path") or str(metadata)
        elif patterns:
            description = str(patterns[0])
        else:
            description = f"Permission request: {permission}"

        perm: PermissionRequest = {
            "request_id": request_id,
            "tool_name": permission,
            "input": metadata if isinstance(metadata, dict) else {"value": str(metadata)},
            "description": description,
            "tool_use_id": tool_info.get("callID", perm_id) if isinstance(tool_info, dict) else perm_id,
            "timestamp": time.time() * 1000,
        }

        self._emit({"type": "permission_request", "request": perm})

    def _handle_session_status(self, props: dict) -> None:
        status = props.get("status", {})
        status_type = status.get("type", "") if isinstance(status, dict) else ""

        if status_type == "busy":
            # Emit message_start
            self._msg_counter += 1
            self._emit({
                "type": "stream_event",
                "event": {
                    "type": "message_start",
                    "message": {
                        "id": f"opencode-msg-{self._msg_counter}",
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
            # Also emit content_block_start for text
            self._emit({
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                "parent_tool_use_id": None,
            })

    def _handle_session_error(self, props: dict) -> None:
        error = props.get("error")
        if error and isinstance(error, dict):
            error_data = error.get("data", {})
            message = error_data.get("message", str(error))
        else:
            message = str(error) if error else "Unknown error"

        self._emit({"type": "error", "message": message})

    # ---- Helpers (same pattern as CodexAdapter) ------------------------------

    def _emit(self, msg: dict) -> None:
        if self._browser_message_cb is not None:
            result = self._browser_message_cb(msg)
            if asyncio.iscoroutine(result):
                asyncio.ensure_future(result)

    def _emit_tool_use(self, tool_use_id: str, tool_name: str, input_data: Dict[str, Any]) -> None:
        self._msg_counter += 1
        self._emit({
            "type": "assistant",
            "message": {
                "id": f"opencode-msg-{self._msg_counter}",
                "type": "message",
                "role": "assistant",
                "model": self._options.model or "",
                "content": [{
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": tool_name,
                    "input": input_data,
                }],
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

    def _emit_tool_use_tracked(self, tool_use_id: str, tool_name: str, input_data: Dict[str, Any]) -> None:
        self._emitted_tool_use_ids.add(tool_use_id)
        self._emit_tool_use(tool_use_id, tool_name, input_data)

    def _emit_tool_use_start(self, tool_use_id: str, tool_name: str, input_data: Dict[str, Any]) -> None:
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

    def _ensure_tool_use_emitted(self, tool_use_id: str, tool_name: str, input_data: Dict[str, Any]) -> None:
        if tool_use_id not in self._emitted_tool_use_ids:
            self._emit_tool_use_tracked(tool_use_id, tool_name, input_data)

    def _emit_tool_result(self, tool_use_id: str, content: str, is_error: bool) -> None:
        self._msg_counter += 1
        self._emit({
            "type": "assistant",
            "message": {
                "id": f"opencode-msg-{self._msg_counter}",
                "type": "message",
                "role": "assistant",
                "model": self._options.model or "",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                    "is_error": is_error,
                }],
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
