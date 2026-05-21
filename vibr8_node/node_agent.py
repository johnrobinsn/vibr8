"""Node Agent — connects to the hub and manages local sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import re
import secrets
import ssl
import time
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from vibr8_core.cli_launcher import CliLauncher, LaunchOptions
from vibr8_core.ws_bridge import WsBridge
from vibr8_core.session_store import SessionStore
from vibr8_core.ring0 import Ring0Manager
from vibr8_core.node_operations import NodeOperations, payload_to_kwargs
from vibr8_core import session_names
from vibr8_node.desktop_webrtc import DesktopWebRTCManager

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
        default_backend: str = "claude",
        self_mode: bool = False,
    ) -> None:
        self.hub_url = hub_url.rstrip("/")
        self.api_key = api_key
        self.name = name
        self.port = port
        self.work_dir = work_dir
        self.ring0_config = ring0_config or {}
        self.default_backend = default_backend
        self.self_mode = self_mode
        self.node_id: str = ""
        # Service token issued by the hub at registration. Used by Ring0 MCP
        # to authenticate against hub-side client / second-screen / artifact
        # endpoints over HTTP. Empty when the hub has auth disabled.
        self.hub_service_token: str = ""
        self._ssl_ctx: ssl.SSLContext | bool | None = None
        if "localhost" in hub_url or "127.0.0.1" in hub_url:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._ssl_ctx = ctx

        # Local managers (initialized in run())
        self._launcher: CliLauncher | None = None
        self._bridge: WsBridge | None = None
        self._store: SessionStore | None = None
        self._ring0: Ring0Manager | None = None
        self._desktop_webrtc = DesktopWebRTCManager()
        self._ops: NodeOperations | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._shutdown_event = asyncio.Event()

    async def run(self) -> None:
        """Main entry point — register, start local server, connect tunnel."""
        if self.self_mode:
            # Default to ~/.vibr8-self/ so a self-mode subprocess can run
            # safely alongside the hub's in-process managers (Phase 4a/b
            # behavior). The hub opts into the consolidated layout by
            # spawning us with VIBR8_SELF_NODE_DATA_DIR=~/.vibr8 (and only
            # when VIBR8_USE_SELF_NODE=1, which also makes the hub skip
            # restore_from_disk so we're the sole owner of that dir).
            import os as _os
            override = _os.environ.get("VIBR8_SELF_NODE_DATA_DIR", "").strip()
            node_dir = Path(override).expanduser() if override else (Path.home() / ".vibr8-self")
            session_dir = str(node_dir / "sessions")
            ring0_config_path = node_dir / "ring0.json"
            ring0_work_dir = node_dir / "ring0"
        else:
            # Use an isolated session directory per node to avoid sharing state with the hub
            safe_name = re.sub(r"[^\w-]", "_", self.name.lower())
            node_dir = Path.home() / ".vibr8-node" / safe_name
            session_dir = str(node_dir / "sessions")
            ring0_config_path = node_dir / "ring0.json"
            ring0_work_dir = node_dir / "ring0"
        self._store = SessionStore(directory=session_dir)
        self._bridge = WsBridge()
        self._launcher = CliLauncher(self.port)
        self._ring0 = Ring0Manager(
            self.port,
            config_path=ring0_config_path,
            work_dir=ring0_work_dir,
            scheme="http",
        )
        from vibr8_core.worktree_tracker import WorktreeTracker
        self._worktree_tracker = WorktreeTracker()

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

        def on_adapter_created(session_id: str, adapter: object, backend_type: str = "codex") -> None:
            self._bridge.attach_adapter(session_id, adapter, backend_type)

        self._launcher.on_codex_adapter_created(on_adapter_created)

        # Set broadcast hook so CLI output goes to the hub tunnel
        self._bridge._broadcast_hook = self._forward_to_hub

        async def _on_sessions_changed() -> None:
            if self._ws and not self._ws.closed:
                await self._send_sessions_update(self._ws)

        self._ops = NodeOperations(
            launcher=self._launcher,
            bridge=self._bridge,
            store=self._store,
            ring0=self._ring0,
            desktop_webrtc=self._desktop_webrtc,
            default_backend=self.default_backend,
            work_dir=self.work_dir,
            worktree_tracker=self._worktree_tracker,
            on_sessions_changed=_on_sessions_changed,
        )

        # Restore persisted state
        self._launcher.restore_from_disk()
        self._bridge.restore_from_disk()

        # Register first — _register sets hub_service_token, which the hub
        # proxy middleware needs at app-construction time, and the hub
        # endpoint on Ring0 so its MCP subprocess (spawned by the auto-launch
        # below) starts with VIBR8_HUB_URL / VIBR8_HUB_TOKEN already in env.
        registered = await self._register()
        if not registered:
            logger.error("Failed to register with hub — exiting")
            return

        # Start minimal local aiohttp server for CLI WebSocket + Ring0 MCP
        await self._start_local_server()

        # Auto-launch Ring0 session if enabled
        if self._ring0 and self._ring0.is_enabled:
            self._bridge.set_ring0_manager(self._ring0)
            logger.info("Ring0 enabled — auto-launching session (backend=%s)", self.default_backend)
            await self._ring0.ensure_session(self._launcher, self._bridge, backend_type=self.default_backend)

        # Connect tunnel with auto-reconnect
        try:
            await self._tunnel_loop()
        finally:
            await self._shutdown()

    def _hub_http_url(self) -> str:
        """Hub URL with the http(s) scheme (input is ws:// or wss://)."""
        return self.hub_url.replace("wss://", "https://").replace("ws://", "http://")

    async def _register(self) -> bool:
        """Register this node with the hub via REST API."""
        http_url = self._hub_http_url()
        url = f"{http_url}/api/nodes/register"

        capabilities = {
            "hostname": platform.node(),
            "platform": platform.system().lower(),
            "arch": platform.machine(),
            "ring0Enabled": self._ring0.is_enabled if self._ring0 else False,
            "sessionCount": len(self._launcher.list_sessions()) if self._launcher else 0,
            "defaultBackend": self.default_backend,
            "version": "0.1.0",
        }

        try:
            conn = aiohttp.TCPConnector(ssl=self._ssl_ctx) if self._ssl_ctx else None
            async with aiohttp.ClientSession(connector=conn) as session:
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
                    self.hub_service_token = data.get("serviceToken", "")
                    # Update Ring0 with the hub HTTP URL + token so its MCP
                    # tools can reach the hub. Safe to call multiple times
                    # on reconnect (just updates the in-memory pointers).
                    if self._ring0:
                        self._ring0.set_hub_endpoint(
                            self._hub_http_url(), self.hub_service_token,
                        )
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

        conn = aiohttp.TCPConnector(ssl=self._ssl_ctx) if self._ssl_ctx else None
        async with aiohttp.ClientSession(connector=conn) as session:
            async with session.ws_connect(ws_url, heartbeat=45, ssl=self._ssl_ctx) as ws:
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
                    "defaultBackend": self.default_backend,
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
        """Generic dispatch: look up the operation by name on NodeOperations."""
        if not self._ops:
            return {"error": "Node not ready"}
        method = getattr(self._ops, cmd_type, None)
        if method is None or not callable(method) or cmd_type.startswith("_"):
            logger.warning("Unknown hub command: %s", cmd_type)
            return {"error": f"Unknown command: {cmd_type}"}
        kwargs = payload_to_kwargs(msg)
        try:
            return await method(**kwargs)
        except TypeError as e:
            logger.exception("Bad payload for %s", cmd_type)
            return {"error": f"Bad payload for {cmd_type}: {e}"}

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

    def _make_hub_proxy_middleware(self):
        """Create aiohttp middleware that proxies hub-only routes to the hub.

        Routes like /api/clients, /api/ring0/query-client, /api/ring0/switch-ui,
        /api/second-screen/*, and /api/artifacts* operate on browser-side state
        that only exists on the hub. On a node, these are forwarded.
        """
        hub_http_url = self._hub_http_url()
        hub_token = self.hub_service_token
        hub_verify_ssl = not (hub_http_url.startswith("https://localhost") or hub_http_url.startswith("https://127.0.0.1"))

        _HUB_ONLY_PREFIXES = (
            "/api/clients",
            "/api/nodes",
            "/api/ring0/query-client",
            "/api/ring0/switch-ui",
            "/api/ring0/switch-audio",
            "/api/ring0/clients",
            "/api/second-screen/",
            "/api/artifacts",
        )

        node_bridge = self._bridge

        @web.middleware
        async def hub_proxy(request: web.Request, handler):
            path = request.path
            if not any(path.startswith(p) or path == p.rstrip("/") for p in _HUB_ONLY_PREFIXES):
                return await handler(request)

            target_url = f"{hub_http_url}{path}"
            headers = {}
            if hub_token:
                headers["Authorization"] = f"Bearer {hub_token}"
            if request.content_type:
                headers["Content-Type"] = request.content_type

            try:
                body = await request.read() if request.can_read_body else None
                _wants_client_id = (
                    path in ("/api/ring0/switch-ui", "/api/ring0/switch-audio")
                    or (path.startswith("/api/nodes/") and path.endswith("/activate"))
                )
                if body and _wants_client_id and node_bridge:
                    import json as _json
                    try:
                        body_dict = _json.loads(body)
                        if not body_dict.get("clientId"):
                            prompt_client = node_bridge.get_ring0_prompt_client()
                            if prompt_client:
                                body_dict["clientId"] = prompt_client
                                body = _json.dumps(body_dict).encode()
                    except (ValueError, KeyError):
                        pass
                ssl_ctx = False if not hub_verify_ssl else None
                async with aiohttp.ClientSession() as session:
                    async with session.request(
                        request.method, target_url,
                        headers=headers,
                        data=body,
                        params=request.query,
                        ssl=ssl_ctx,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        resp_body = await resp.read()
                        return web.Response(
                            status=resp.status,
                            body=resp_body,
                            content_type=resp.content_type,
                        )
            except Exception as e:
                logger.error("[node] Hub proxy error for %s: %s", path, e)
                return web.json_response({"error": f"Hub proxy failed: {e}"}, status=502)

        return hub_proxy

    async def _start_local_server(self) -> None:
        """Start a minimal aiohttp server for CLI WebSocket + Ring0 MCP."""
        middlewares = []
        # Self-mode is co-located with the hub, so no proxy needed (proxying
        # to the same host's hub would just loop).
        if self.hub_service_token and not self.self_mode:
            middlewares.append(self._make_hub_proxy_middleware())
        app = web.Application(middlewares=middlewares)
        bridge = self._bridge
        launcher = self._launcher

        async def handle_cli_ws(request: web.Request) -> web.WebSocketResponse:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            session_id = request.match_info["session_id"]

            await bridge.handle_cli_open(ws, session_id)
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
        ring0 = self._ring0
        api_routes = create_routes(
            launcher, bridge, self._store,
            worktree_tracker=self._worktree_tracker,
            ring0_manager=ring0,
            self_node_name=self.name,
            local_node_ops=self._ops,
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
        if self._desktop_webrtc:
            await self._desktop_webrtc.close_all()
        if self._bridge:
            self._bridge.flush_to_disk()
        if self._launcher:
            await self._launcher.kill_all()
        if self._runner:
            await self._runner.cleanup()
        logger.info("Shutdown complete")
