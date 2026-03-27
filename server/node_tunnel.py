"""Node Tunnel — bidirectional NDJSON command channel over WebSocket.

Wraps a persistent WebSocket connection between the hub and a remote node,
providing request/response correlation and message dispatch.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any, Callable, Coroutine

from aiohttp import web

logger = logging.getLogger(__name__)


class NodeTunnel:
    """Command channel for a single node's WebSocket tunnel."""

    def __init__(self, node_id: str, node_name: str, ws: web.WebSocketResponse) -> None:
        self.node_id = node_id
        self.node_name = node_name
        self._ws = ws
        self._pending: dict[str, asyncio.Future] = {}  # requestId → Future
        self._on_message: Callable[..., Coroutine] | None = None

    @property
    def connected(self) -> bool:
        return not self._ws.closed

    def set_message_handler(
        self, handler: Callable[[str, dict], Coroutine]
    ) -> None:
        """Set callback for node-initiated messages: handler(node_id, msg)."""
        self._on_message = handler

    async def send_command(
        self, cmd: dict[str, Any], timeout: float = 30.0
    ) -> dict[str, Any]:
        """Send a command and wait for the correlated response."""
        request_id = secrets.token_hex(8)
        cmd["requestId"] = request_id

        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[request_id] = future

        try:
            await self._ws.send_str(json.dumps(cmd) + "\n")
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "[tunnel] Command %s timed out for node %s (type=%s)",
                request_id, self.node_name, cmd.get("type"),
            )
            return {"error": "timeout"}
        finally:
            self._pending.pop(request_id, None)

    async def send_fire_and_forget(self, cmd: dict[str, Any]) -> None:
        """Send a command without waiting for a response."""
        if self._ws.closed:
            return
        try:
            await self._ws.send_str(json.dumps(cmd) + "\n")
        except Exception:
            logger.warning("[tunnel] Failed to send to node %s", self.node_name)

    async def handle_incoming(self, raw: str) -> None:
        """Process an incoming NDJSON line from the node."""
        for line in raw.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("[tunnel] Invalid JSON from node %s", self.node_name)
                continue

            msg_type = msg.get("type", "")

            # Response to a pending command
            if msg_type == "response":
                request_id = msg.get("requestId")
                future = self._pending.get(request_id)
                if future and not future.done():
                    future.set_result(msg.get("data", {}))
                continue

            # Node-initiated message — dispatch to handler
            if self._on_message:
                try:
                    await self._on_message(self.node_id, msg)
                except Exception:
                    logger.exception(
                        "[tunnel] Error handling message from node %s (type=%s)",
                        self.node_name, msg_type,
                    )

    def close(self) -> None:
        """Cancel all pending futures when the tunnel disconnects."""
        for request_id, future in self._pending.items():
            if not future.done():
                future.set_exception(
                    ConnectionError(f"Node {self.node_name} disconnected")
                )
        self._pending.clear()
