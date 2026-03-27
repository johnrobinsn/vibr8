"""Node Agent — connects to the hub and manages local sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import secrets
import time
from typing import Any

import aiohttp
from aiohttp import web

from server.cli_launcher import CliLauncher, LaunchOptions
from server.ws_bridge import WsBridge
from server.session_store import SessionStore
from server.ring0 import Ring0Manager
from server import session_names

logger = logging.getLogger("vibr8-node")


class NodeAgent:
    """Remote node agent that connects to the vibr8 hub."""

    def __init__(
        self,
        hub_url: str,
        api_key: str,
        name: str,
        port: int = 3457,
        work_dir: str = "",
        ring0_config: dict | None = None,
    ) -> None:
        self.hub_url = hub_url.rstrip("/")
        self.api_key = api_key
        self.name = name
        self.port = port
        self.work_dir = work_dir
        self.ring0_config = ring0_config or {}
        self.node_id: str = ""

        # Local managers (initialized in run())
        self._launcher: CliLauncher | None = None
        self._bridge: WsBridge | None = None
        self._store: SessionStore | None = None
        self._ring0: Ring0Manager | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._shutdown_event = asyncio.Event()

    async def run(self) -> None:
        """Main entry point — register, start local server, connect tunnel."""
        # Initialize local managers
        self._store = SessionStore()
        self._bridge = WsBridge()
        self._launcher = CliLauncher(self.port)
        self._ring0 = Ring0Manager(self.port)

        self._bridge.set_store(self._store)
        self._launcher.set_store(self._store)

        # Wire up callbacks
        def on_cli_session_id(session_id: str, cli_session_id: str) -> None:
            self._launcher.set_cli_session_id(session_id, cli_session_id)
            if self._ring0 and session_id == self._ring0.session_id:
                self._ring0.on_cli_session_id(cli_session_id)

        self._bridge.on_cli_session_id_received(on_cli_session_id)

        def on_cli_relaunch_needed(session_id: str) -> None:
            info = self._launcher.get_session(session_id)
            if info and not info.archived and info.state != "starting":
                logger.info("Auto-relaunching CLI for session %s", session_id[:8])
                asyncio.ensure_future(self._launcher.relaunch(session_id))

        self._bridge.on_cli_relaunch_needed_callback(on_cli_relaunch_needed)

        # Set broadcast hook so CLI output goes to the hub tunnel
        self._bridge._broadcast_hook = self._forward_to_hub

        # Restore persisted state
        self._launcher.restore_from_disk()
        self._bridge.restore_from_disk()

        # Start minimal local aiohttp server for CLI WebSocket + Ring0 MCP
        await self._start_local_server()

        # Auto-launch Ring0 session if enabled
        if self._ring0 and self._ring0.is_enabled:
            logger.info("Ring0 enabled — auto-launching session")
            await self._ring0.ensure_session(self._launcher, self._bridge)

        # Register with hub
        registered = await self._register()
        if not registered:
            logger.error("Failed to register with hub — exiting")
            return

        # Connect tunnel with auto-reconnect
        try:
            await self._tunnel_loop()
        finally:
            await self._shutdown()

    async def _register(self) -> bool:
        """Register this node with the hub via REST API."""
        # Convert hub WS URL to HTTP
        http_url = self.hub_url.replace("wss://", "https://").replace("ws://", "http://")
        url = f"{http_url}/api/nodes/register"

        capabilities = {
            "hostname": platform.node(),
            "platform": platform.system().lower(),
            "arch": platform.machine(),
            "ring0Enabled": self._ring0.is_enabled if self._ring0 else False,
            "sessionCount": len(self._launcher.list_sessions()) if self._launcher else 0,
            "version": "0.1.0",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json={
                    "name": self.name,
                    "apiKey": self.api_key,
                    "capabilities": capabilities,
                }) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.error("Registration failed (%d): %s", resp.status, text)
                        return False
                    data = await resp.json()
                    self.node_id = data.get("nodeId", "")
                    logger.info("Registered as node %s (id=%s)", self.name, self.node_id[:8])
                    return True
        except Exception:
            logger.exception("Failed to connect to hub for registration")
            return False

    async def _tunnel_loop(self) -> None:
        """Connect to hub WS tunnel with auto-reconnect."""
        backoff = 2.0
        max_backoff = 30.0

        while not self._shutdown_event.is_set():
            try:
                await self._connect_tunnel()
                backoff = 2.0  # Reset on successful connection
            except Exception:
                logger.exception("Tunnel connection error")

            if self._shutdown_event.is_set():
                break

            logger.info("Reconnecting in %.0fs...", backoff)
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(), timeout=backoff
                )
                break  # Shutdown requested during backoff
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, max_backoff)

    async def _connect_tunnel(self) -> None:
        """Establish and maintain the WS tunnel to the hub."""
        ws_url = f"{self.hub_url}/ws/node/{self.node_id}?apiKey={self.api_key}"
        logger.info("Connecting tunnel to %s", self.hub_url)

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url, heartbeat=45) as ws:
                self._ws = ws
                logger.info("Tunnel connected")

                # Start heartbeat task
                heartbeat_task = asyncio.ensure_future(self._heartbeat_loop(ws))
                # Send initial session list
                await self._send_sessions_update(ws)

                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_hub_message(ws, msg.data)
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                            break
                finally:
                    heartbeat_task.cancel()
                    self._ws = None
                    logger.info("Tunnel disconnected")

    async def _heartbeat_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Send periodic heartbeats."""
        while True:
            await asyncio.sleep(30)
            try:
                msg = {
                    "type": "heartbeat",
                    "sessionCount": len(self._launcher.list_sessions()) if self._launcher else 0,
                    "ring0Enabled": self._ring0.is_enabled if self._ring0 else False,
                }
                await ws.send_str(json.dumps(msg) + "\n")
            except Exception:
                break

    async def _send_sessions_update(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Send current session list to hub."""
        if not self._launcher:
            return
        sessions = []
        names = session_names.get_all_names()
        for s in self._launcher.list_sessions():
            s_dict = s.to_dict() if hasattr(s, "to_dict") else s.__dict__
            sid = s_dict.get("sessionId", "")
            s_dict["name"] = names.get(sid, s_dict.get("name"))
            if self._ring0 and sid == self._ring0.session_id:
                s_dict["isRing0"] = True
            sessions.append(s_dict)
        await ws.send_str(json.dumps({
            "type": "sessions_update",
            "sessions": sessions,
        }) + "\n")

    async def _handle_hub_message(self, ws: aiohttp.ClientWebSocketResponse, raw: str) -> None:
        """Dispatch incoming commands from the hub."""
        for line in raw.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")
            request_id = msg.get("requestId", "")

            try:
                result = await self._dispatch_command(msg_type, msg)
                if request_id:
                    await ws.send_str(json.dumps({
                        "type": "response",
                        "requestId": request_id,
                        "data": result,
                    }) + "\n")
            except Exception as e:
                logger.exception("Error handling hub command: %s", msg_type)
                if request_id:
                    await ws.send_str(json.dumps({
                        "type": "response",
                        "requestId": request_id,
                        "data": {"error": str(e)},
                    }) + "\n")

    async def _dispatch_command(self, cmd_type: str, msg: dict) -> dict:
        """Handle a specific hub command."""
        if cmd_type == "list_sessions":
            return await self._cmd_list_sessions()
        elif cmd_type == "create_session":
            return await self._cmd_create_session(msg.get("options", {}))
        elif cmd_type == "submit_message":
            return await self._cmd_submit_message(msg)
        elif cmd_type == "cli_input":
            return await self._cmd_cli_input(msg)
        elif cmd_type == "interrupt":
            return await self._cmd_interrupt(msg)
        elif cmd_type == "browser_message":
            return await self._cmd_browser_message(msg)
        elif cmd_type == "get_session_output":
            return await self._cmd_get_session_output(msg)
        elif cmd_type == "set_permission_mode":
            return await self._cmd_set_permission_mode(msg)
        elif cmd_type == "respond_permission":
            return await self._cmd_respond_permission(msg)
        elif cmd_type == "ring0_input":
            return await self._cmd_ring0_input(msg)
        elif cmd_type == "kill_session":
            return await self._cmd_kill_session(msg)
        elif cmd_type == "relaunch_session":
            return await self._cmd_relaunch_session(msg)
        elif cmd_type == "delete_session":
            return await self._cmd_delete_session(msg)
        elif cmd_type == "archive_session":
            return await self._cmd_archive_session(msg)
        elif cmd_type == "unarchive_session":
            return await self._cmd_unarchive_session(msg)
        elif cmd_type == "rename_session":
            return await self._cmd_rename_session(msg)
        else:
            logger.warning("Unknown hub command: %s", cmd_type)
            return {"error": f"Unknown command: {cmd_type}"}

    async def _cmd_list_sessions(self) -> dict:
        if not self._launcher:
            return {"sessions": []}
        sessions = []
        names = session_names.get_all_names()
        for s in self._launcher.list_sessions():
            s_dict = s.to_dict() if hasattr(s, "to_dict") else s.__dict__
            sid = s_dict.get("sessionId", "")
            s_dict["name"] = names.get(sid, s_dict.get("name"))
            if self._bridge:
                lpa = self._bridge.get_last_prompted_at(sid)
                if lpa:
                    s_dict["lastPromptedAt"] = lpa
            if self._ring0 and sid == self._ring0.session_id:
                s_dict["isRing0"] = True
            sessions.append(s_dict)
        return {"sessions": sessions}

    async def _cmd_create_session(self, options: dict) -> dict:
        if not self._launcher:
            return {"error": "Launcher not available"}
        opts = LaunchOptions(
            model=options.get("model"),
            permissionMode=options.get("permissionMode"),
            cwd=options.get("cwd") or self.work_dir or None,
            backendType=options.get("backend", "claude"),
        )
        info = self._launcher.launch(opts)
        result = info.to_dict() if hasattr(info, "to_dict") else info.__dict__
        # Notify hub of updated session list
        if self._ws:
            await self._send_sessions_update(self._ws)
        return result

    async def _cmd_submit_message(self, msg: dict) -> dict:
        if not self._bridge:
            return {"error": "Bridge not available"}
        session_id = msg.get("sessionId", "")
        content = msg.get("content", "")
        source = msg.get("sourceClientId", "")
        err = await self._bridge.submit_user_message(session_id, content, source_client_id=source)
        if err:
            return {"error": err}
        return {"ok": True}

    async def _cmd_cli_input(self, msg: dict) -> dict:
        """Forward raw CLI input (NDJSON message) to local session."""
        if not self._bridge:
            return {"error": "Bridge not available"}
        session_id = msg.get("sessionId", "")
        message = msg.get("message", {})
        session = self._bridge._sessions.get(session_id)
        if not session:
            return {"error": f"Session {session_id} not found"}
        ndjson = json.dumps(message)
        self._bridge._send_to_cli(session, ndjson)
        return {"ok": True}

    async def _cmd_interrupt(self, msg: dict) -> dict:
        if not self._bridge:
            return {"error": "Bridge not available"}
        session_id = msg.get("sessionId", "")
        ok = self._bridge.interrupt_session(session_id)
        return {"ok": ok}

    async def _cmd_browser_message(self, msg: dict) -> dict:
        """Handle a browser message forwarded via the hub."""
        if not self._bridge:
            return {"error": "Bridge not available"}
        session_id = msg.get("sessionId", "")
        message = msg.get("message", {})
        source_client_id = msg.get("sourceClientId", "")
        session = self._bridge._sessions.get(session_id)
        if not session:
            return {"error": f"Session {session_id} not found"}
        # Route as if it came from a local browser
        await self._bridge._route_browser_message(session, message, ws=None)
        return {"ok": True}

    async def _cmd_get_session_output(self, msg: dict) -> dict:
        if not self._bridge:
            return {"error": "Bridge not available"}
        session_id = msg.get("sessionId", "")
        session = self._bridge._sessions.get(session_id)
        if not session:
            return {"error": f"Session {session_id} not found"}
        return {"messages": session.message_history[-500:]}

    async def _cmd_kill_session(self, msg: dict) -> dict:
        if not self._launcher:
            return {"error": "Launcher not available"}
        sid = msg.get("sessionId", "")
        killed = await self._launcher.kill(sid)
        if self._ws:
            await self._send_sessions_update(self._ws)
        return {"ok": killed}

    async def _cmd_relaunch_session(self, msg: dict) -> dict:
        if not self._launcher:
            return {"error": "Launcher not available"}
        sid = msg.get("sessionId", "")
        ok = await self._launcher.relaunch(sid)
        if self._ws:
            await self._send_sessions_update(self._ws)
        return {"ok": ok}

    async def _cmd_delete_session(self, msg: dict) -> dict:
        if not self._launcher:
            return {"error": "Launcher not available"}
        sid = msg.get("sessionId", "")
        await self._launcher.kill(sid)
        self._launcher.remove_session(sid)
        if self._bridge:
            await self._bridge.close_session(sid)
        if self._ws:
            await self._send_sessions_update(self._ws)
        return {"ok": True}

    async def _cmd_archive_session(self, msg: dict) -> dict:
        if not self._launcher:
            return {"error": "Launcher not available"}
        sid = msg.get("sessionId", "")
        await self._launcher.kill(sid)
        self._launcher.set_archived(sid, True)
        if self._ws:
            await self._send_sessions_update(self._ws)
        return {"ok": True}

    async def _cmd_unarchive_session(self, msg: dict) -> dict:
        if not self._launcher:
            return {"error": "Launcher not available"}
        sid = msg.get("sessionId", "")
        self._launcher.set_archived(sid, False)
        if self._ws:
            await self._send_sessions_update(self._ws)
        return {"ok": True}

    async def _cmd_rename_session(self, msg: dict) -> dict:
        sid = msg.get("sessionId", "")
        name = msg.get("name", "").strip()
        if not name:
            return {"error": "name is required"}
        session_names.set_name(sid, name, unique=False)
        if self._ws:
            await self._send_sessions_update(self._ws)
        return {"ok": True, "name": name}

    async def _cmd_set_permission_mode(self, msg: dict) -> dict:
        if not self._bridge:
            return {"error": "Bridge not available"}
        session_id = msg.get("sessionId", "")
        mode = msg.get("mode", "")
        session = self._bridge._sessions.get(session_id)
        if not session:
            return {"error": f"Session {session_id} not found"}
        self._bridge._handle_set_permission_mode(session, mode)
        return {"ok": True}

    async def _cmd_respond_permission(self, msg: dict) -> dict:
        if not self._bridge:
            return {"error": "Bridge not available"}
        session_id = msg.get("sessionId", "")
        session = self._bridge._sessions.get(session_id)
        if not session:
            return {"error": f"Session {session_id} not found"}
        await self._bridge._handle_permission_response(session, msg)
        return {"ok": True}

    async def _cmd_ring0_input(self, msg: dict) -> dict:
        """Handle voice input forwarded from hub to this node's Ring0."""
        if not self._ring0 or not self._ring0.is_enabled:
            return {"error": "Ring0 not enabled on this node"}
        text = msg.get("text", "")
        if not text:
            return {"error": "Empty text"}
        r0sid = self._ring0.session_id
        if not r0sid and self._launcher and self._bridge:
            r0sid = await self._ring0.ensure_session(self._launcher, self._bridge)
        if not r0sid:
            return {"error": "Ring0 session not available"}
        source_client_id = msg.get("sourceClientId", "")
        await self._bridge.submit_user_message(r0sid, text, source_client_id=source_client_id)
        return {"ok": True}

    # ── Broadcast hook ────────────────────────────────────────────────────

    async def _forward_to_hub(self, session_id: str, msg: dict) -> None:
        """Forward CLI output to hub via WS tunnel (broadcast hook)."""
        if self._ws and not self._ws.closed:
            await self._ws.send_str(json.dumps({
                "type": "session_message",
                "sessionId": session_id,
                "message": msg,
            }) + "\n")

    # ── Local server ──────────────────────────────────────────────────────

    async def _start_local_server(self) -> None:
        """Start a minimal aiohttp server for CLI WebSocket + Ring0 MCP."""
        app = web.Application()
        bridge = self._bridge
        launcher = self._launcher

        async def handle_cli_ws(request: web.Request) -> web.WebSocketResponse:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            session_id = request.match_info["session_id"]

            bridge.handle_cli_open(ws, session_id)
            launcher.mark_connected(session_id)

            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await bridge.handle_cli_message(ws, msg.data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        break
            finally:
                await bridge.handle_cli_close(ws)
            return ws

        app.router.add_get("/ws/cli/{session_id}", handle_cli_ws)

        # Ring0 MCP routes (reuse from server.routes)
        from server.routes import create_routes
        from server.worktree_tracker import WorktreeTracker
        ring0 = self._ring0
        api_routes = create_routes(
            launcher, bridge, self._store,
            worktree_tracker=WorktreeTracker(),
            ring0_manager=ring0,
        )
        app.router.add_routes(api_routes)

        self._app = app
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        await site.start()
        logger.info("Local server running on http://127.0.0.1:%d", self.port)

    async def _shutdown(self) -> None:
        """Clean shutdown."""
        if self._bridge:
            self._bridge.flush_to_disk()
        if self._launcher:
            await self._launcher.kill_all()
        if self._runner:
            await self._runner.cleanup()
        logger.info("Shutdown complete")
