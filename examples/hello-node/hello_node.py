"""hello-node — the minimum viable vibr8 node.

Implements ONE contract flag (`ui/v1`) so it can vend a static HTML/JS
UI through the hub's `/nodes/{id}/{ui,api,ws}/*` proxy. It has no
Ring0, no session persistence, no CLIs — just a REST ping and a
WebSocket counter to demonstrate that both round-trips work end to end.

Read the file top-to-bottom: registration → tunnel loop → four tiny
message handlers (http_request / ws_open / ws_data / ws_close) → local
aiohttp server serving /ui, /api, /ws. That's it. Any additional
features are up to you — the hub does not know or care.

The contract lives in ../../docs/hub-node-contract-v1.md.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import platform
import ssl
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("hello-node")


# ─── The node ────────────────────────────────────────────────────────────────

class HelloNode:
    """One process = one node. Instantiate, .run(), done."""

    def __init__(self, *, hub_url: str, api_key: str, name: str, port: int) -> None:
        self.hub_url = hub_url.rstrip("/")
        self.api_key = api_key
        self.name = name
        self.port = port
        self.node_id: str = ""
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        # Loopback TLS to a local dev hub is fine to skip cert-verify on.
        self._ssl_ctx: ssl.SSLContext | bool | None = None
        if "localhost" in hub_url or "127.0.0.1" in hub_url:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._ssl_ctx = ctx

        # Proxied browser WS channels the hub has opened on us
        # (channelId → the aiohttp ClientWebSocketResponse that's talking
        # to our own local /ws/* endpoint).
        self._channels: dict[str, aiohttp.ClientWebSocketResponse] = {}
        self._channel_tasks: dict[str, asyncio.Task] = {}

        # Local demo state — a counter shared by any connected browser.
        # These live on the aiohttp app so /ws/counter can reach them.
        self._local_app: web.Application | None = None

    # ── Registration + tunnel loop ────────────────────────────────────────

    def _hub_http(self) -> str:
        return self.hub_url.replace("wss://", "https://").replace("ws://", "http://")

    async def _register(self) -> bool:
        """POST /api/nodes/register — see contract §A1."""
        capabilities = {
            "protocolVersion": 1,
            "contract": ["ui/v1"],  # the only flag this example needs
            "hostname": platform.node(),
            "platform": platform.system().lower(),
            "arch": platform.machine(),
            "version": "0.1.0",
        }
        conn = aiohttp.TCPConnector(ssl=self._ssl_ctx) if self._ssl_ctx else None
        async with aiohttp.ClientSession(connector=conn) as s:
            async with s.post(
                f"{self._hub_http()}/api/nodes/register",
                json={"name": self.name, "apiKey": self.api_key, "capabilities": capabilities},
            ) as resp:
                if resp.status != 200:
                    logger.error("Registration failed: %d %s", resp.status, await resp.text())
                    return False
                data = await resp.json()
                self.node_id = data["nodeId"]
                logger.info("Registered as node %s (id=%s)", self.name, self.node_id[:8])
                return True

    async def _tunnel_forever(self) -> None:
        """Reconnect loop with exponential backoff — the contract wants
        this to be the node's job."""
        backoff = 2.0
        while True:
            try:
                await self._tunnel_once()
                backoff = 2.0
            except Exception:
                logger.exception("Tunnel error; will retry")
            logger.info("Reconnecting in %.0fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _tunnel_once(self) -> None:
        ws_url = f"{self.hub_url}/ws/node/{self.node_id}?apiKey={self.api_key}"
        conn = aiohttp.TCPConnector(ssl=self._ssl_ctx) if self._ssl_ctx else None
        async with aiohttp.ClientSession(connector=conn) as s:
            async with s.ws_connect(
                ws_url, heartbeat=45, ssl=self._ssl_ctx,
                max_msg_size=64 * 1024 * 1024,   # ui/v1 bodies can be big
            ) as ws:
                self._ws = ws
                logger.info("Tunnel connected")
                hb = asyncio.create_task(self._heartbeat_loop(ws))
                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._on_hub_line(ws, msg.data)
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                            break
                finally:
                    hb.cancel()
                    self._ws = None
                    logger.info("Tunnel closed")

    async def _heartbeat_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Contract §A2: every 30s. Without this the hub marks the node
        offline after 90s of silence and greys it out in the picker."""
        while not ws.closed:
            await asyncio.sleep(30)
            try:
                await ws.send_str(json.dumps({"type": "heartbeat"}) + "\n")
            except Exception:
                break

    async def _on_hub_line(self, ws: aiohttp.ClientWebSocketResponse, raw: str) -> None:
        """NDJSON dispatcher. Every hub → node line lands here."""
        for line in raw.strip().split("\n"):
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = msg.get("type", "")
            rid = msg.get("requestId", "")
            try:
                if t == "http_request":
                    data = await self._on_http_request(msg)
                elif t == "ws_open":
                    data = await self._on_ws_open(msg)
                elif t == "ws_data":
                    data = await self._on_ws_data(msg)
                elif t == "ws_close":
                    data = await self._on_ws_close(msg)
                else:
                    # Silently ignore unknown types per contract §Versioning.
                    continue
            except Exception as e:
                logger.exception("Handler for %s crashed", t)
                data = {"error": str(e)}
            if rid:
                await ws.send_str(json.dumps({"type": "response", "requestId": rid, "data": data}) + "\n")

    # ── ui/v1 plumbing — proxy hub-side requests to our local server ─────

    async def _on_http_request(self, msg: dict) -> dict:
        """A browser hit /nodes/{id}/{ui|api}/... — replay that request
        against our own aiohttp server on 127.0.0.1:{port} and return
        the response."""
        method = str(msg.get("method", "GET")).upper()
        path = str(msg.get("path", "/"))
        if not path.startswith("/") or ".." in path:
            return {"error": "Bad path"}
        url = f"http://127.0.0.1:{self.port}{path}"
        headers = {k: v for k, v in (msg.get("headers") or {}).items()
                   if k.lower() in ("content-type", "accept", "range")}
        body = base64.b64decode(msg["bodyB64"]) if msg.get("bodyB64") else None
        async with aiohttp.ClientSession() as s:
            async with s.request(
                method, url, params=msg.get("query") or None,
                headers=headers, data=body, allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=55),
            ) as resp:
                raw = await resp.read()
                return {
                    "status": resp.status,
                    "headers": {k: resp.headers[k]
                                for k in ("Content-Type", "Cache-Control", "ETag")
                                if k in resp.headers},
                    "bodyB64": base64.b64encode(raw).decode(),
                }

    async def _on_ws_open(self, msg: dict) -> dict:
        """Browser opened /nodes/{id}/ws/... — hold that as one of our
        channels and pipe it to a local WS on our own aiohttp server."""
        channel_id = str(msg.get("channelId", ""))
        path = str(msg.get("path", "/"))
        if not channel_id or not path.startswith("/"):
            return {"error": "Bad ws_open"}
        qs = urlencode(msg.get("query") or {})
        url = f"ws://127.0.0.1:{self.port}{path}" + (f"?{qs}" if qs else "")

        session = aiohttp.ClientSession()
        try:
            local_ws = await session.ws_connect(url, max_msg_size=64 * 1024 * 1024)
        except Exception:
            await session.close()
            # Signal failure so the browser sees the close.
            await self._fire({"type": "ws_close", "channelId": channel_id, "code": 1011})
            return {"ok": False}
        self._channels[channel_id] = local_ws

        async def pump() -> None:
            """Forward every frame from our local WS up to the hub as ws_data."""
            try:
                async for m in local_ws:
                    if m.type == aiohttp.WSMsgType.TEXT:
                        await self._fire({"type": "ws_data", "channelId": channel_id, "text": m.data})
                    elif m.type == aiohttp.WSMsgType.BINARY:
                        await self._fire({"type": "ws_data", "channelId": channel_id,
                                          "dataB64": base64.b64encode(m.data).decode()})
                    elif m.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                        break
            finally:
                await self._fire({"type": "ws_close", "channelId": channel_id, "code": local_ws.close_code or 1000})
                self._channels.pop(channel_id, None)
                await session.close()

        self._channel_tasks[channel_id] = asyncio.create_task(pump())
        return {"ok": True}

    async def _on_ws_data(self, msg: dict) -> dict:
        """A browser sent a frame on an open channel; forward to our local WS."""
        channel_id = str(msg.get("channelId", ""))
        ws = self._channels.get(channel_id)
        if ws is None or ws.closed:
            return {"error": "unknown channel"}
        if "text" in msg:
            await ws.send_str(str(msg["text"]))
        elif "dataB64" in msg:
            await ws.send_bytes(base64.b64decode(msg["dataB64"]))
        return {"ok": True}

    async def _on_ws_close(self, msg: dict) -> dict:
        """Browser closed. Tear down our end of the channel."""
        channel_id = str(msg.get("channelId", ""))
        ws = self._channels.pop(channel_id, None)
        task = self._channel_tasks.pop(channel_id, None)
        if ws is not None:
            await ws.close()
        if task is not None:
            task.cancel()
        return {"ok": True}

    async def _fire(self, payload: dict) -> None:
        """Fire-and-forget push to the hub."""
        if self._ws and not self._ws.closed:
            try:
                await self._ws.send_str(json.dumps(payload) + "\n")
            except Exception:
                pass

    # ── Local aiohttp server — this is *your* app ────────────────────────

    async def _run_local_server(self) -> None:
        """Serve /ui/, /api/*, /ws/* on 127.0.0.1:{port}. The hub proxies
        into this via the tunnel — the browser never dials it directly."""
        app = web.Application()
        self._local_app = app
        app["counter"] = 0
        app["counter_ws"] = set()  # currently-connected browsers via /ws/counter

        # Static SPA at /ui/*  — everything falls back to index.html.
        ui_dir = Path(__file__).parent / "ui"

        async def serve_ui(request: web.Request) -> web.StreamResponse:
            tail = request.match_info.get("tail", "") or "index.html"
            f = ui_dir / tail
            if not f.is_file() or ".." in tail:
                f = ui_dir / "index.html"
            return web.FileResponse(f)

        app.router.add_get("/ui/", serve_ui)
        app.router.add_get("/ui/{tail:.*}", serve_ui)

        # A trivial REST endpoint.
        async def ping(_: web.Request) -> web.Response:
            return web.json_response({"message": "pong from hello-node", "node": self.name})

        app.router.add_get("/api/ping", ping)

        # A shared counter over WebSocket. Any connected browser can
        # increment; every browser sees the update.
        async def counter_ws(request: web.Request) -> web.WebSocketResponse:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            app["counter_ws"].add(ws)
            await ws.send_str(json.dumps({"type": "state", "value": app["counter"]}))
            try:
                async for m in ws:
                    if m.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data = json.loads(m.data)
                        except json.JSONDecodeError:
                            continue
                        if data.get("type") == "bump":
                            app["counter"] += 1
                            broadcast = json.dumps({"type": "state", "value": app["counter"]})
                            for peer in list(app["counter_ws"]):
                                try:
                                    await peer.send_str(broadcast)
                                except Exception:
                                    app["counter_ws"].discard(peer)
            finally:
                app["counter_ws"].discard(ws)
            return ws

        app.router.add_get("/ws/counter", counter_ws)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", self.port)
        await site.start()
        logger.info("Local server on http://127.0.0.1:%d", self.port)

    # ── Entry point ──────────────────────────────────────────────────────

    async def run(self) -> None:
        if not await self._register():
            return
        await self._run_local_server()
        await self._tunnel_forever()


def main() -> None:
    p = argparse.ArgumentParser(description="hello-node — minimum viable vibr8 node")
    p.add_argument("--hub", required=True, help="Hub URL (ws://... or wss://...)")
    p.add_argument("--api-key", required=True, help="API key issued by the hub")
    p.add_argument("--name", default="hello", help="Display name (default: hello)")
    p.add_argument("--port", type=int, default=4470, help="Local loopback port")
    args = p.parse_args()
    node = HelloNode(hub_url=args.hub, api_key=args.api_key, name=args.name, port=args.port)
    try:
        asyncio.run(node.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
