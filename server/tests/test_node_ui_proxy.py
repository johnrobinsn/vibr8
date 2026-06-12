"""Tests for the ui/v1 node-vended UI proxy (docs/hub-node-contract-v1.md §A3)."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from server.node_ui_proxy import (
    WS_CHANNELS_KEY,
    dispatch_channel_message,
    register_node_ui_routes,
)


def _make_node(send_command_result=None):
    node = MagicMock()
    node.tunnel = MagicMock()
    node.tunnel.connected = True
    node.tunnel.send_command = AsyncMock(return_value=send_command_result or {})
    node.tunnel.send_fire_and_forget = AsyncMock()
    return node


def _make_app(node=None, self_node=None):
    app = web.Application()
    registry = MagicMock()
    registry.get_node.return_value = node
    registry.get_node_by_name.return_value = self_node
    app["node_registry"] = registry
    register_node_ui_routes(app)
    return app


async def test_http_proxy_round_trip():
    body = b"<html>node ui</html>"
    node = _make_node({
        "status": 200,
        "headers": {"Content-Type": "text/html", "Connection": "keep-alive"},
        "bodyB64": base64.b64encode(body).decode(),
    })
    app = _make_app(node=node)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/nodes/abc123/ui/index.html?v=1")
        assert resp.status == 200
        assert await resp.read() == body
        assert resp.headers["Content-Type"] == "text/html"
        # Hop-by-hop headers never pass through
        assert "Connection" not in resp.headers or resp.headers["Connection"] != "keep-alive"

    cmd = node.tunnel.send_command.call_args[0][0]
    assert cmd["type"] == "http_request"
    assert cmd["method"] == "GET"
    assert cmd["path"] == "/ui/index.html"
    assert cmd["query"] == {"v": "1"}


async def test_http_proxy_post_body_and_api_path():
    node = _make_node({"status": 201, "headers": {}, "bodyB64": ""})
    app = _make_app(node=node)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/nodes/abc123/api/sessions", json={"a": 1})
        assert resp.status == 201

    cmd = node.tunnel.send_command.call_args[0][0]
    assert cmd["path"] == "/api/sessions"
    assert base64.b64decode(cmd["bodyB64"]) == b'{"a": 1}'
    assert cmd["headers"].get("Content-Type", "").startswith("application/json")


async def test_http_proxy_node_offline_is_502():
    app = _make_app(node=None)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/nodes/missing/ui/")
        assert resp.status == 502


async def test_http_proxy_tunnel_timeout_is_504():
    node = _make_node({"error": "timeout"})
    app = _make_app(node=node)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/nodes/abc123/ui/")
        assert resp.status == 504


async def test_local_alias_resolves_self_node():
    body = b"self"
    self_node = _make_node({
        "status": 200, "headers": {}, "bodyB64": base64.b64encode(body).decode(),
    })
    app = _make_app(node=None, self_node=self_node)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/nodes/local/ui/")
        assert resp.status == 200
        assert await resp.read() == body


async def test_ws_proxy_opens_channel_and_forwards():
    node = _make_node()
    app = _make_app(node=node)

    async with TestClient(TestServer(app)) as client:
        ws = await client.ws_connect("/nodes/abc123/ws/browser/sess-1?clientId=c1")

        # ws_open went down the tunnel with the channel id
        open_msg = node.tunnel.send_fire_and_forget.call_args_list[0][0][0]
        assert open_msg["type"] == "ws_open"
        assert open_msg["path"] == "/ws/browser/sess-1"
        assert open_msg["query"] == {"clientId": "c1"}
        channel_id = open_msg["channelId"]
        assert app[WS_CHANNELS_KEY].get(channel_id) is not None

        # Browser → node direction
        await ws.send_str('{"type":"input"}')
        # Node → browser direction
        await dispatch_channel_message(
            app, {"type": "ws_data", "channelId": channel_id, "text": "hello"},
        )
        msg = await ws.receive_str()
        assert msg == "hello"

        # Node-side close tears down the browser socket
        await dispatch_channel_message(
            app, {"type": "ws_close", "channelId": channel_id},
        )
        await ws.receive()  # close frame
        assert channel_id not in app[WS_CHANNELS_KEY]

    sent_types = [c[0][0]["type"] for c in node.tunnel.send_fire_and_forget.call_args_list]
    assert "ws_data" in sent_types


async def test_dispatch_ignores_unknown_channel():
    app = web.Application()
    app[WS_CHANNELS_KEY] = {}
    await dispatch_channel_message(
        app, {"type": "ws_data", "channelId": "nope", "text": "x"},
    )
    await dispatch_channel_message(app, {"type": "ws_close", "channelId": "nope"})


async def test_node_agent_http_request_handler():
    """Node-side http_request serves from the local loopback server."""
    from vibr8_node.node_agent import NodeAgent

    local_app = web.Application()

    async def hello(request: web.Request) -> web.Response:
        return web.Response(
            text=f"hi {request.query.get('who', '?')}",
            headers={"Cache-Control": "max-age=60"},
        )

    local_app.router.add_get("/ui/hello", hello)

    server = TestServer(local_app)
    await server.start_server()
    try:
        agent = NodeAgent("ws://example.invalid", "key", "t", port=server.port)
        result = await agent._handle_http_request({
            "method": "GET", "path": "/ui/hello", "query": {"who": "vibr8"},
        })
        assert result["status"] == 200
        assert base64.b64decode(result["bodyB64"]) == b"hi vibr8"
        assert result["headers"]["Cache-Control"] == "max-age=60"

        bad = await agent._handle_http_request({"method": "GET", "path": "no-slash"})
        assert "error" in bad
    finally:
        await server.close()
