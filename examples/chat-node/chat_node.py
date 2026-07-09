"""chat-node — a vibr8 node that runs an LLM chat and speaks its answers.

Contract flags: `ui/v1` (vend a UI) + `events/v1` (voice). No Ring0,
no sessions, no CLI orchestration — just a text turn loop where you
type or speak, an OpenAI-compatible chat API answers, and the hub
plays that answer as TTS.

Environment:
  LLM_URL     — chat completions endpoint (default: OpenAI)
  LLM_API_KEY — bearer token
  LLM_MODEL   — model name (default: gpt-4o-mini)

Compatible with any OpenAI-shaped endpoint: OpenAI, Anthropic's
`/v1/chat/completions`, Ollama, LM Studio, vLLM, Groq, together.ai, …

Read `hello_node.py` first — the ui/v1 plumbing is identical. This
file layers `events/v1` (transcript in, speak/busy out) on top.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import platform
import ssl
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("chat-node")


# ─── LLM call — one function, no SDK ─────────────────────────────────────────

async def llm_reply(history: list[dict]) -> str:
    """POST a chat/completions call to any OpenAI-compatible endpoint.

    Kept dead-simple on purpose: swap the endpoint to point wherever
    you like. `history` is the OpenAI chat-messages array
    ([{role, content}, ...]) — the full transcript so far.
    """
    url = os.environ.get("LLM_URL", "https://api.openai.com/v1/chat/completions")
    key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {"model": model, "messages": history, "stream": False}
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=body, headers=headers,
                          timeout=aiohttp.ClientTimeout(total=120)) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.warning("LLM call failed: %d %s", resp.status, text[:200])
                return f"[LLM error {resp.status}]"
            data = await resp.json()
            try:
                return data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                return f"[unexpected LLM response: {json.dumps(data)[:200]}]"


# ─── The node ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are a friendly voice-first assistant embedded in a vibr8 hub. "
    "Answers reach the user through TTS, so keep them short — one or two "
    "sentences — unless they ask for detail."
)


class ChatNode:
    def __init__(self, *, hub_url: str, api_key: str, name: str, port: int) -> None:
        self.hub_url = hub_url.rstrip("/")
        self.api_key = api_key
        self.name = name
        self.port = port
        self.node_id: str = ""
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ssl_ctx: ssl.SSLContext | bool | None = None
        if "localhost" in hub_url or "127.0.0.1" in hub_url:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._ssl_ctx = ctx

        self._channels: dict[str, aiohttp.ClientWebSocketResponse] = {}
        self._channel_tasks: dict[str, asyncio.Task] = {}

        # Conversation history — the entire chat log lives in memory here.
        # A real node would persist per-user history somewhere.
        self._history: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        # Every connected browser gets pushed each turn on this WS.
        self._chat_ws: set[web.WebSocketResponse] = set()

    # ── Registration + tunnel (identical to hello-node) ──────────────────

    def _hub_http(self) -> str:
        return self.hub_url.replace("wss://", "https://").replace("ws://", "http://")

    async def _register(self) -> bool:
        capabilities = {
            "protocolVersion": 1,
            # Two flags this time: we vend a UI AND we consume voice.
            "contract": ["ui/v1", "events/v1"],
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
                max_msg_size=64 * 1024 * 1024,
            ) as ws:
                self._ws = ws
                logger.info("Tunnel connected")
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._on_hub_line(ws, msg.data)
                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                        break
                self._ws = None

    async def _on_hub_line(self, ws: aiohttp.ClientWebSocketResponse, raw: str) -> None:
        for line in raw.strip().split("\n"):
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = msg.get("type", "")
            rid = msg.get("requestId", "")

            # events/v1 — the transcript arrives here every time the user
            # speaks (post guard-word). Handle it exactly like typed input.
            if t == "transcript":
                asyncio.create_task(
                    self._turn(user_text=str(msg.get("text", "")))
                )
                continue

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
                    continue
            except Exception as e:
                logger.exception("Handler for %s crashed", t)
                data = {"error": str(e)}
            if rid:
                await ws.send_str(json.dumps({"type": "response", "requestId": rid, "data": data}) + "\n")

    # ── events/v1 emit — speak / busy ────────────────────────────────────

    async def _fire(self, payload: dict) -> None:
        if self._ws and not self._ws.closed:
            try:
                await self._ws.send_str(json.dumps(payload) + "\n")
            except Exception:
                pass

    async def _speak(self, text: str) -> None:
        """Ask the hub to say this. TTS is a hub service — we never
        touch audio."""
        await self._fire({"type": "speak", "text": text})

    async def _busy(self, is_busy: bool) -> None:
        """Signal to the hub that we're mid-turn (drives thinking-indicator
        affordances in the shell)."""
        await self._fire({"type": "busy", "busy": bool(is_busy)})

    # ── One conversational turn ──────────────────────────────────────────

    async def _turn(self, user_text: str) -> None:
        """The core loop: append user msg → call LLM → push both to browser
        + hub. Runs the same whether the input came from the browser
        (POST /api/chat) or from a voice `transcript` event."""
        user_text = user_text.strip()
        if not user_text:
            return

        self._history.append({"role": "user", "content": user_text})
        await self._push_to_browser({"role": "user", "content": user_text})

        await self._busy(True)
        try:
            reply = await llm_reply(self._history)
        finally:
            await self._busy(False)

        self._history.append({"role": "assistant", "content": reply})
        await self._push_to_browser({"role": "assistant", "content": reply})
        await self._speak(reply)   # ← hub does TTS

    async def _push_to_browser(self, msg: dict) -> None:
        """Send one chat message to every /ws/chat browser."""
        blob = json.dumps({"type": "message", **msg})
        for ws in list(self._chat_ws):
            try:
                await ws.send_str(blob)
            except Exception:
                self._chat_ws.discard(ws)

    # ── ui/v1 plumbing (identical to hello-node) ─────────────────────────

    async def _on_http_request(self, msg: dict) -> dict:
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
                timeout=aiohttp.ClientTimeout(total=120),
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
            await self._fire({"type": "ws_close", "channelId": channel_id, "code": 1011})
            return {"ok": False}
        self._channels[channel_id] = local_ws

        async def pump() -> None:
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
        channel_id = str(msg.get("channelId", ""))
        ws = self._channels.pop(channel_id, None)
        task = self._channel_tasks.pop(channel_id, None)
        if ws is not None:
            await ws.close()
        if task is not None:
            task.cancel()
        return {"ok": True}

    # ── Local aiohttp server ─────────────────────────────────────────────

    async def _run_local_server(self) -> None:
        app = web.Application()
        ui_dir = Path(__file__).parent / "ui"

        async def serve_ui(request: web.Request) -> web.StreamResponse:
            tail = request.match_info.get("tail", "") or "index.html"
            f = ui_dir / tail
            if not f.is_file() or ".." in tail:
                f = ui_dir / "index.html"
            return web.FileResponse(f)

        app.router.add_get("/ui/", serve_ui)
        app.router.add_get("/ui/{tail:.*}", serve_ui)

        # POST /api/chat — the browser sends a typed message here.
        async def chat_send(request: web.Request) -> web.Response:
            body = await request.json()
            text = str(body.get("text", ""))
            asyncio.create_task(self._turn(user_text=text))
            return web.json_response({"ok": True})

        app.router.add_post("/api/chat", chat_send)

        # GET /api/history — replay for reloads.
        async def chat_history(_: web.Request) -> web.Response:
            visible = [m for m in self._history if m.get("role") != "system"]
            return web.json_response({"messages": visible})

        app.router.add_get("/api/history", chat_history)

        # /ws/chat — live push channel to every open browser.
        async def chat_ws(request: web.Request) -> web.WebSocketResponse:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            self._chat_ws.add(ws)
            for m in self._history:
                if m.get("role") == "system":
                    continue
                await ws.send_str(json.dumps({"type": "message", **m}))
            try:
                async for _ in ws:
                    pass
            finally:
                self._chat_ws.discard(ws)
            return ws

        app.router.add_get("/ws/chat", chat_ws)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", self.port)
        await site.start()
        logger.info("Local server on http://127.0.0.1:%d", self.port)

    async def run(self) -> None:
        if not await self._register():
            return
        await self._run_local_server()
        await self._tunnel_forever()


def main() -> None:
    p = argparse.ArgumentParser(description="chat-node — LLM chat with voice via events/v1")
    p.add_argument("--hub", required=True)
    p.add_argument("--api-key", required=True)
    p.add_argument("--name", default="chat")
    p.add_argument("--port", type=int, default=4471)
    args = p.parse_args()
    node = ChatNode(hub_url=args.hub, api_key=args.api_key, name=args.name, port=args.port)
    try:
        asyncio.run(node.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
