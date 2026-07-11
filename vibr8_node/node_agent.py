"""Node Agent — connects to the hub and manages local sessions."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import platform
import re
import secrets
import ssl
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

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
    ) -> None:
        self.hub_url = hub_url.rstrip("/")
        self.api_key = api_key
        self.name = name
        self.port = port
        self.work_dir = work_dir
        self.ring0_config = ring0_config or {}
        self.default_backend = default_backend
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
        # ui/v1 proxied browser-WS channels: channelId → local client WS.
        self._ui_channels: dict[str, aiohttp.ClientWebSocketResponse] = {}
        self._ui_channel_tasks: dict[str, asyncio.Task] = {}

    async def run(self) -> None:
        """Main entry point — register, start local server, connect tunnel."""
        # Per-node data dir. VIBR8_NODE_DATA_DIR is the source of truth
        # (set by __main__.py before vibr8_core import so the module-
        # level path constants in vibr8_core/* see the right value).
        from vibr8_core.data_paths import NODE_DATA_DIR
        node_dir = NODE_DATA_DIR
        session_dir = str(node_dir / "sessions")
        ring0_config_path = node_dir / "ring0.json"
        ring0_work_dir = node_dir / "ring0"
        self._store = SessionStore(directory=session_dir)
        self._bridge = WsBridge()
        # Node's local server is plain HTTP, so the CLI must use ws:// (not wss://).
        self._launcher = CliLauncher(self.port, scheme="ws")
        self._ring0 = Ring0Manager(
            self.port,
            config_path=ring0_config_path,
            work_dir=ring0_work_dir,
            scheme="http",
        )
        from vibr8_core.worktree_tracker import WorktreeTracker
        self._worktree_tracker = WorktreeTracker()
        # Per-node TaskScheduler — scheduled Ring0 tasks run on this node.
        from vibr8_core.ring0_scheduler import TaskScheduler
        self._scheduler = TaskScheduler()
        self._scheduler.set_dependencies(self._launcher, self._bridge)

        self._bridge.set_store(self._store)
        self._launcher.set_store(self._store)

        # Wire up callbacks
        def on_cli_session_id(session_id: str, cli_session_id: str) -> None:
            self._launcher.set_cli_session_id(session_id, cli_session_id)
            if self._ring0 and session_id == self._ring0.session_id:
                self._ring0.on_cli_session_id(cli_session_id)

        self._bridge.on_cli_session_id_received(on_cli_session_id)

        def on_cli_relaunch_needed(session_id: str) -> None:
            if not self._launcher.can_relaunch(session_id):
                return
            logger.info("Auto-relaunching CLI for session %s", session_id[:8])
            asyncio.ensure_future(self._launcher.relaunch(session_id))

        self._bridge.on_cli_relaunch_needed_callback(on_cli_relaunch_needed)

        def on_adapter_created(session_id: str, adapter: object, backend_type: str = "codex") -> None:
            self._bridge.attach_adapter(session_id, adapter, backend_type)

        self._launcher.on_codex_adapter_created(on_adapter_created)

        # CLI output broadcasts to node-local browser sockets — browsers
        # reach this node through the hub's ui/v1 WS channel proxy, so no
        # hub-side forwarding hook is needed (or wanted: the hook replaces
        # local broadcasting entirely).
        # Contract events/v1 (docs/hub-node-contract-v1.md §B): Ring0 speech
        # and status leave this node as explicit events; the hub owns audio.
        self._bridge._speak_hook = self._emit_speak
        self._bridge._busy_hook = self._emit_busy
        self._bridge._attention_hook = self._emit_attention

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
            task_scheduler=self._scheduler,
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

        # Start the per-node TaskScheduler (scheduled Ring0 background tasks).
        await self._scheduler.start()

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
            "protocolVersion": 1,
            "contract": ["ui/v1", "events/v1", "desktop/v1"],
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
            # max_msg_size raised for ui/v1 (large proxied request bodies).
            async with session.ws_connect(
                ws_url, heartbeat=45, ssl=self._ssl_ctx,
                max_msg_size=64 * 1024 * 1024,
            ) as ws:
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
        # Contract plumbing (ui/v1) handled by the agent itself — these are
        # transport-level, not node operations.
        if cmd_type == "http_request":
            return await self._handle_http_request(msg)
        if cmd_type == "ws_open":
            return await self._handle_ws_open(msg)
        if cmd_type == "ws_data":
            return await self._handle_ws_data(msg)
        if cmd_type == "ws_close":
            return await self._handle_ws_close(msg)
        if not self._ops:
            return {"error": "Node not ready"}
        if cmd_type == "transcript":
            # Contract §B: voice/typed input for this node's Ring0.
            return await self._ops.ring0_input(
                text=msg.get("text", ""),
                source_client_id=msg.get("clientId", ""),
            )
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

    # ── Contract events/v1: speak / busy / attention (§B) ────────────────

    async def _emit_speak(self, text: str) -> None:
        await self._send_to_hub({"type": "speak", "text": text})

    async def _emit_busy(self, busy: bool) -> None:
        await self._send_to_hub({"type": "busy", "busy": bool(busy)})

    async def _emit_attention(self, reason: str) -> None:
        await self._send_to_hub({"type": "attention", "reason": reason})

    async def _emit_title(self, text: str) -> None:
        """Fire-and-forget: tell the hub what to show in the shell strip.
        Additive to contract §A2 — hubs that don't understand it drop it."""
        await self._send_to_hub({"type": "title", "text": text})

    # ── Contract ui/v1: HTTP + WS proxying (docs/hub-node-contract-v1.md §A3)

    async def _send_to_hub(self, payload: dict) -> None:
        """Send a fire-and-forget NDJSON message up the tunnel."""
        if self._ws and not self._ws.closed:
            try:
                await self._ws.send_str(json.dumps(payload) + "\n")
            except Exception:
                logger.warning("Failed to send %s to hub", payload.get("type"))

    async def _handle_http_request(self, msg: dict) -> dict:
        """Serve a hub-proxied browser HTTP request from the local server."""
        method = str(msg.get("method", "GET")).upper()
        path = str(msg.get("path", "/"))
        if not path.startswith("/") or ".." in path:
            return {"error": f"Bad path: {path!r}"}
        url = f"http://127.0.0.1:{self.port}{path}"
        headers = {
            k: v for k, v in (msg.get("headers") or {}).items()
            if k.lower() in ("content-type", "accept", "if-none-match",
                             "if-modified-since", "range")
        }
        body = base64.b64decode(msg["bodyB64"]) if msg.get("bodyB64") else None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method, url,
                    params=msg.get("query") or None,
                    headers=headers,
                    data=body,
                    timeout=aiohttp.ClientTimeout(total=55),
                    allow_redirects=False,
                ) as resp:
                    raw = await resp.read()
                    out_headers = {
                        k: resp.headers[k]
                        for k in ("Content-Type", "Cache-Control", "ETag",
                                  "Last-Modified", "Location")
                        if k in resp.headers
                    }
                    return {
                        "status": resp.status,
                        "headers": out_headers,
                        "bodyB64": base64.b64encode(raw).decode(),
                    }
        except Exception as e:
            logger.warning("http_request %s %s failed: %s", method, path, e)
            return {"error": str(e)}

    async def _handle_ws_open(self, msg: dict) -> dict:
        """Open a local WS for a hub-proxied browser WebSocket channel."""
        channel_id = str(msg.get("channelId", ""))
        path = str(msg.get("path", "/"))
        if not channel_id or not path.startswith("/"):
            return {"error": "Bad ws_open"}
        qs = urlencode(msg.get("query") or {})
        url = f"ws://127.0.0.1:{self.port}{path}" + (f"?{qs}" if qs else "")
        session = aiohttp.ClientSession()
        try:
            local_ws = await session.ws_connect(
                url, max_msg_size=64 * 1024 * 1024,
            )
        except Exception as e:
            await session.close()
            await self._send_to_hub({"type": "ws_close", "channelId": channel_id})
            return {"error": f"ws_open failed: {e}"}
        self._ui_channels[channel_id] = local_ws
        self._ui_channel_tasks[channel_id] = asyncio.ensure_future(
            self._pump_ui_channel(channel_id, session, local_ws)
        )
        return {"ok": True}

    async def _pump_ui_channel(
        self,
        channel_id: str,
        session: aiohttp.ClientSession,
        local_ws: aiohttp.ClientWebSocketResponse,
    ) -> None:
        """Forward local-server WS messages up to the hub until close."""
        try:
            async for m in local_ws:
                if m.type == aiohttp.WSMsgType.TEXT:
                    await self._send_to_hub({
                        "type": "ws_data", "channelId": channel_id, "text": m.data,
                    })
                elif m.type == aiohttp.WSMsgType.BINARY:
                    await self._send_to_hub({
                        "type": "ws_data", "channelId": channel_id,
                        "dataB64": base64.b64encode(m.data).decode(),
                    })
                elif m.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
        except Exception:
            logger.debug("ui channel %s pump error", channel_id, exc_info=True)
        finally:
            self._ui_channels.pop(channel_id, None)
            self._ui_channel_tasks.pop(channel_id, None)
            try:
                await local_ws.close()
            finally:
                await session.close()
            await self._send_to_hub({"type": "ws_close", "channelId": channel_id})

    async def _handle_ws_data(self, msg: dict) -> dict:
        ws = self._ui_channels.get(str(msg.get("channelId", "")))
        if ws is None or ws.closed:
            return {"error": "No such channel"}
        if msg.get("text") is not None:
            await ws.send_str(msg["text"])
        elif msg.get("dataB64"):
            await ws.send_bytes(base64.b64decode(msg["dataB64"]))
        return {"ok": True}

    async def _handle_ws_close(self, msg: dict) -> dict:
        channel_id = str(msg.get("channelId", ""))
        ws = self._ui_channels.pop(channel_id, None)
        if ws is not None and not ws.closed:
            # The pump task notices the close and finishes cleanup.
            await ws.close()
        return {"ok": True}

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

        # Routes proxied to the hub: hub-only state (browser clients,
        # second-screen pairings, artifacts, node registry, voice/UI
        # switching). Per-node Ring0 is fully internal — session-resolving
        # routes (send-message, interrupt, respond-permission, …) run
        # locally; NodeOperations._expand_session_id handles the 8-char
        # prefixes Ring0 passes from list_sessions output.
        _HUB_ONLY_PREFIXES = (
            "/api/clients",
            "/api/nodes",
            "/api/ring0/query-client",
            "/api/ring0/switch-ui",
            "/api/ring0/switch-audio",
            "/api/ring0/clients",
            "/api/ring0/prompt-context",
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
        # Hub-only routes (e.g. /api/clients, /api/ring0/query-client) must be
        # proxied to the hub even from the self-node — they read state that
        # only exists on the hub's WsBridge (browser ws connections,
        # client_metadata, etc.). The self-node has its own WsBridge instance
        # which is empty for these. The hub runs on a different port so there
        # is no loop.
        if self.hub_service_token:
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

        # Browser WS — reached via the hub's ui/v1 channel proxy. This
        # node's WsBridge owns real session state, so the handler mirrors
        # the hub's handle_browser_ws.
        async def handle_browser_ws(request: web.Request) -> web.WebSocketResponse:
            ws = web.WebSocketResponse(heartbeat=45)
            await ws.prepare(request)
            # Expand an 8-char session prefix (e.g. one a mirroring second
            # screen got from Ring0's list_sessions) to the full session id.
            session_id = self._ops._expand_session_id(request.match_info["session_id"])
            client_id = request.rel_url.query.get("clientId", "")
            role = request.rel_url.query.get("role", "primary")
            mirror = request.rel_url.query.get("mirror", "") == "true"
            await bridge.handle_browser_open(
                ws, session_id, client_id, role=role, mirror=mirror,
            )
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await bridge.handle_browser_message(ws, msg.data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        break
            finally:
                await bridge.handle_browser_close(ws)
            return ws

        app.router.add_get("/ws/browser/{session_id}", handle_browser_ws)

        # Node-vended UI (contract ui/v1): serve the built frontend at /ui/.
        # The build ships with the node, so UI and backend are always the
        # same commit — version skew with the hub is impossible here.
        dist_dir = Path(__file__).resolve().parent.parent / "web" / "dist"

        async def handle_ui(request: web.Request) -> web.StreamResponse:
            if not dist_dir.is_dir():
                return web.json_response(
                    {"error": "Node UI not built (web/dist missing)"}, status=503,
                )
            rel = request.match_info.get("path", "")
            if rel:
                candidate = (dist_dir / rel).resolve()
                if candidate.is_file() and dist_dir in candidate.parents:
                    return web.FileResponse(candidate)
            return web.FileResponse(dist_dir / "index.html")

        app.router.add_get("/ui", handle_ui)
        app.router.add_get("/ui/{path:.*}", handle_ui)

        # POST /api/_title — the node's own UI publishes the currently
        # displayed title (session name, etc.) here; the node relays it
        # to the hub as a fire-and-forget `title` tunnel event so the
        # shell can render it in the strip. Registered before
        # create_routes so it wins any name collision.
        async def set_title(request: web.Request) -> web.Response:
            try:
                body = await request.json()
            except Exception:
                return web.json_response({"error": "Invalid JSON"}, status=400)
            text = str((body or {}).get("text", "")).strip()
            await self._emit_title(text)
            return web.json_response({"ok": True})

        app.router.add_post("/api/_title", set_title)

        # Ring0 MCP routes (reuse from server.routes)
        from server.routes import create_routes
        ring0 = self._ring0
        api_routes = create_routes(
            launcher, bridge, self._store,
            worktree_tracker=self._worktree_tracker,
            ring0_manager=ring0,
            local_node_ops=self._ops,
        )
        app.router.add_routes(api_routes)

        self._app = app
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        # Hardcoded 127.0.0.1 is load-bearing: the node's local HTTP server
        # has no auth of its own (it relies on the hub for browser-facing
        # auth and on the tunnel API key for hub-side calls). The hub's
        # server/main.py auth guard does NOT extend here. If you ever make
        # this bind configurable, mirror server/main.resolve_bind_host so a
        # public bind requires explicit VIBR8_ALLOW_PUBLIC_NO_AUTH.
        site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        await site.start()
        logger.info("Local server running on http://127.0.0.1:%d", self.port)

    async def _shutdown(self) -> None:
        """Clean shutdown."""
        for task in list(self._ui_channel_tasks.values()):
            task.cancel()
        self._ui_channel_tasks.clear()
        self._ui_channels.clear()
        if self._desktop_webrtc:
            await self._desktop_webrtc.close_all()
        if self._scheduler:
            try:
                await self._scheduler.stop()
            except Exception:
                logger.exception("Scheduler stop failed")
        if self._bridge:
            self._bridge.flush_to_disk()
        if self._launcher:
            await self._launcher.kill_all()
        if self._runner:
            await self._runner.cleanup()
        logger.info("Shutdown complete")
