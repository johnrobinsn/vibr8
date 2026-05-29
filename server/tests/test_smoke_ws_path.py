"""E2E smoke gate for the browser→bridge WS path.

The bug class this catches: anything that breaks "browser opens
/ws/browser/{sid}, sends a user_message, server's WsBridge logs and
routes it." Several refactor regressions in the last week (per-tab
close orphaning, self-node proxy skip, broadcast cleanup cascading)
manifested as exactly this path being broken in subtle ways while
every unit test passed.

Deliberately narrow:
- Real aiohttp server (TestServer).
- Real WebSocket client.
- Real WsBridge (no mocks).
- No CLI, no Ring0, no node registry, no auth.

A green test means: a browser frame on the WS lands in
`session.message_history`. A red test means production-grade "I sent a
prompt and the server never logged it" — the symptom we just spent
hours diagnosing.
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from vibr8_core.ws_bridge import WsBridge


BRIDGE_KEY = web.AppKey("bridge", WsBridge)


# Mirrors server.main.handle_browser_ws — copied here so the smoke
# doesn't pull in the entire create_app() (Ring0, warmup, scheduler).
# If this handler diverges from main.py's, that's the regression we
# want this smoke to catch — keep the two in sync.
async def _handle_browser_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=45)
    await ws.prepare(request)
    session_id = request.match_info["session_id"]
    bridge = request.app[BRIDGE_KEY]
    client_id = request.rel_url.query.get("clientId", "")
    role = request.rel_url.query.get("role", "primary")
    mirror = request.rel_url.query.get("mirror", "") == "true"
    await bridge.handle_browser_open(ws, session_id, client_id, role=role, mirror=mirror)
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await bridge.handle_browser_message(ws, msg.data)
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        await bridge.handle_browser_close(ws)
    return ws


@pytest.fixture
async def smoke_client():
    bridge = WsBridge()
    app = web.Application()
    app[BRIDGE_KEY] = bridge
    app.router.add_get("/ws/browser/{session_id}", _handle_browser_ws)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client, bridge
    finally:
        await client.close()


async def test_browser_user_message_lands_in_bridge(smoke_client) -> None:
    """The headline smoke: a browser sending `user_message` over WS
    must make the bridge record it. If this is red, vibr8 is broken in
    the most user-visible way possible — no prompt reaches the server.
    """
    client, bridge = smoke_client
    session_id = "smoke-session"
    client_id = "smoke-client"

    async with client.ws_connect(
        f"/ws/browser/{session_id}?clientId={client_id}",
    ) as ws:
        await ws.send_json({"type": "user_message", "content": "hello, smoke"})

        # The bridge processes messages on the same event loop. Give
        # it a moment to route through _route_browser_message →
        # _handle_user_message → message_history.append.
        for _ in range(50):
            session = bridge._sessions.get(session_id)
            if session and any(
                m.get("type") == "user_message" and m.get("content") == "hello, smoke"
                for m in session.message_history
            ):
                break
            await asyncio.sleep(0.02)
        else:
            session = bridge._sessions.get(session_id)
            history = session.message_history if session else None
            pytest.fail(
                f"user_message did not land in bridge.message_history within 1s. "
                f"session={session is not None} history={history}"
            )


async def test_browser_open_registers_client_in_bridge(smoke_client) -> None:
    """Companion check: opening the WS must put the client in
    `_client_sessions` / `_ws_by_client`, otherwise `query_client` /
    RPC fails downstream with 'Client X not connected'.

    Catches the class of bug we hit twice: per-tab close orphaning,
    self-node proxy skipping (which made API calls hit the wrong
    bridge), broadcast cleanup cascading.
    """
    client, bridge = smoke_client
    session_id = "smoke-session-2"
    client_id = "smoke-client-2"

    async with client.ws_connect(
        f"/ws/browser/{session_id}?clientId={client_id}",
    ):
        # The open is fire-and-forget; wait briefly for handle_browser_open
        # to finish running.
        for _ in range(50):
            if client_id in bridge._client_sessions:
                break
            await asyncio.sleep(0.02)
        assert client_id in bridge._client_sessions, (
            f"client {client_id} not registered after WS open — "
            f"`query_client` / RPC will fail with 'not connected'"
        )
        assert bridge._client_sessions[client_id] == session_id
        assert client_id in bridge._ws_by_client


async def test_browser_close_unregisters_when_no_survivor(smoke_client) -> None:
    """Closing the only ws for a client must remove it from
    `_client_sessions` so stale entries don't accumulate."""
    client, bridge = smoke_client
    session_id = "smoke-session-3"
    client_id = "smoke-client-3"

    async with client.ws_connect(
        f"/ws/browser/{session_id}?clientId={client_id}",
    ) as ws:
        for _ in range(50):
            if client_id in bridge._client_sessions:
                break
            await asyncio.sleep(0.02)
        assert client_id in bridge._client_sessions

    # Now the `async with` has closed the WS; wait briefly for
    # handle_browser_close → cleanup helper to run.
    for _ in range(50):
        if client_id not in bridge._client_sessions:
            break
        await asyncio.sleep(0.02)

    assert client_id not in bridge._client_sessions, (
        f"client {client_id} still registered after WS close"
    )
