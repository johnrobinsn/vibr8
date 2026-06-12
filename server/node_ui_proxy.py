"""UI-vending proxy (contract `ui/v1`, docs/hub-node-contract-v1.md §A3).

Maps the hub URL space onto a node's loopback server through the node's
existing outbound tunnel:

    /nodes/{node_id}/ui/{p}   →  http_request  path=/ui/{p}
    /nodes/{node_id}/api/{p}  →  http_request  path=/api/{p}
    /nodes/{node_id}/ws/{p}   →  ws_open/ws_data/ws_close  path=/ws/{p}

The hub never dials the node — requests are wrapped as tunnel messages down
the node-initiated WebSocket and answered by the node's 127.0.0.1-bound
local server. `node_id` may be the literal "local" to target the self-node.
"""

from __future__ import annotations

import base64
import logging
import secrets

from aiohttp import WSMsgType, web

logger = logging.getLogger(__name__)

# Browser ws ↔ tunnel channel map, keyed by hub-generated channelId.
WS_CHANNELS_KEY = "node_ui_ws_channels"

# Headers never forwarded in either direction (hop-by-hop, or invalidated
# by the b64 re-encode: the node returns decompressed bodies).
_DROP_HEADERS = {
    "connection", "keep-alive", "transfer-encoding", "upgrade",
    "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "content-encoding", "content-length", "host", "cookie",
    "authorization",
}

# Request headers worth passing through to the node's local server.
_PASS_REQUEST_HEADERS = {
    "content-type", "accept", "if-none-match", "if-modified-since",
    "range",
}


def _resolve_node(request: web.Request, node_id: str):
    """Resolve a node with a connected tunnel, or None. "local" → self-node."""
    registry = request.app.get("node_registry")
    if registry is None:
        return None
    if node_id == "local":
        node = registry.get_node_by_name("self")
    else:
        node = registry.get_node(node_id)
    if not node or not node.tunnel or not getattr(node.tunnel, "connected", False):
        return None
    return node


async def handle_node_http_proxy(request: web.Request) -> web.StreamResponse:
    """Proxy one browser HTTP request to the node via `http_request`."""
    node_id = request.match_info["node_id"]
    node = _resolve_node(request, node_id)
    if node is None:
        return web.json_response(
            {"error": f"Node {node_id} is not connected"}, status=502,
        )

    kind = request.match_info["kind"]  # "ui" or "api"
    tail = request.match_info.get("tail", "")
    path = f"/{kind}{tail}"

    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() in _PASS_REQUEST_HEADERS
    }
    body = await request.read() if request.can_read_body else b""

    cmd: dict = {
        "type": "http_request",
        "method": request.method,
        "path": path,
        "query": dict(request.query),
        "headers": headers,
    }
    if body:
        cmd["bodyB64"] = base64.b64encode(body).decode()

    data = await node.tunnel.send_command(cmd, timeout=60.0)
    if not isinstance(data, dict) or "status" not in data:
        err = data.get("error", "no response") if isinstance(data, dict) else "bad response"
        status = 504 if err == "timeout" else 502
        return web.json_response({"error": f"Node proxy failed: {err}"}, status=status)

    resp_headers = {
        k: v for k, v in (data.get("headers") or {}).items()
        if k.lower() not in _DROP_HEADERS
    }
    resp_body = base64.b64decode(data.get("bodyB64", "") or "")
    return web.Response(status=int(data["status"]), headers=resp_headers, body=resp_body)


async def handle_node_ws_proxy(request: web.Request) -> web.WebSocketResponse:
    """Bridge a browser WebSocket onto a node-local WS via tunnel channels."""
    node_id = request.match_info["node_id"]
    node = _resolve_node(request, node_id)
    if node is None:
        raise web.HTTPBadGateway(text=f"Node {node_id} is not connected")

    ws = web.WebSocketResponse(heartbeat=45)
    await ws.prepare(request)

    channel_id = secrets.token_hex(8)
    channels: dict[str, web.WebSocketResponse] = request.app[WS_CHANNELS_KEY]
    channels[channel_id] = ws

    tail = request.match_info.get("tail", "")
    await node.tunnel.send_fire_and_forget({
        "type": "ws_open",
        "channelId": channel_id,
        "path": f"/ws{tail}",
        "query": dict(request.query),
    })

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await node.tunnel.send_fire_and_forget({
                    "type": "ws_data", "channelId": channel_id, "text": msg.data,
                })
            elif msg.type == WSMsgType.BINARY:
                await node.tunnel.send_fire_and_forget({
                    "type": "ws_data", "channelId": channel_id,
                    "dataB64": base64.b64encode(msg.data).decode(),
                })
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        if channels.pop(channel_id, None) is not None:
            await node.tunnel.send_fire_and_forget({
                "type": "ws_close", "channelId": channel_id,
            })

    return ws


async def dispatch_channel_message(app: web.Application, msg: dict) -> None:
    """Handle node-initiated ws_data/ws_close for a proxied browser WS."""
    channels: dict[str, web.WebSocketResponse] = app[WS_CHANNELS_KEY]
    channel_id = msg.get("channelId", "")
    msg_type = msg.get("type", "")

    if msg_type == "ws_data":
        ws = channels.get(channel_id)
        if ws is None or ws.closed:
            return
        try:
            if msg.get("text") is not None:
                await ws.send_str(msg["text"])
            elif msg.get("dataB64"):
                await ws.send_bytes(base64.b64decode(msg["dataB64"]))
        except Exception:
            logger.warning("[node-ui] send to browser failed (channel %s)", channel_id)
    elif msg_type == "ws_close":
        ws = channels.pop(channel_id, None)
        if ws is not None and not ws.closed:
            await ws.close()


def register_node_ui_routes(app: web.Application) -> None:
    app[WS_CHANNELS_KEY] = {}
    app.router.add_route(
        "*", r"/nodes/{node_id}/{kind:(ui|api)}{tail:.*}", handle_node_http_proxy,
    )
    app.router.add_get(r"/nodes/{node_id}/ws{tail:.*}", handle_node_ws_proxy)
