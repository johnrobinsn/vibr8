"""Tests that pin the current public auth surface."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from server import auth
from server.auth import auth_middleware


class EnabledAuth:
    enabled = True

    def validate_session(self, token: str) -> str | None:
        return "alice" if token == "valid" else None


async def ok_handler(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "authUser": request.get("auth_user")})


async def call_middleware(
    path: str,
    *,
    token: str = "",
    cookie_token: str = "",
) -> web.StreamResponse:
    app = MagicMock()
    app.get.return_value = EnabledAuth()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if cookie_token:
        headers["Cookie"] = f"vibr8_session={cookie_token}"
    request = make_mocked_request("GET", path, headers=headers, app=app)
    return await auth_middleware(request, ok_handler)


@pytest.mark.parametrize(
    "path",
    [
        "/ws/cli/session-1",
        "/ws/node/node-1",
        "/api/auth/login",
        "/api/auth/me",
        "/api/pairing/request",
        "/api/pairing/status/123456",
        "/api/nodes/register",
        "/api/second-screen/pair-code",
        "/api/second-screen/status",
        "/assets/index.js",
        "/sw.js",
        "/manifest.json",
        "/logo.svg",
        "/favicon.ico",
        "/apple-touch-icon.png",
    ],
)
async def test_current_public_paths_bypass_auth(path: str) -> None:
    response = await call_middleware(path)
    assert response.status == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api/sessions",
        "/api/fs/read",
        "/api/auth/device-token",
        "/api/pairing/confirm",
        "/api/second-screen/list",
        "/api/nodes",
        "/api/nodes/node-1/activate",
        "/api/nodes/generate-key",
        "/api/nodes/tokens",
        "/api/nodes/active",
        "/api/ring0/status",
        "/api/ring0/query-client",
        "/api/ring0/send-message",
        "/api/ring0/respond-permission",
        "/ws/browser/session-1",
        "/ws/native/client-1",
        "/ws/terminal/session-1",
        "/ws/playground/client-1",
        "/ws/enrollment/client-1",
    ],
)
async def test_protected_api_and_ws_paths_reject_anonymous_requests(path: str) -> None:
    response = await call_middleware(path)
    assert response.status == 401


async def test_public_paths_still_capture_optional_auth_user() -> None:
    response = await call_middleware("/api/auth/me", token="valid")
    assert response.status == 200
    assert json.loads(response.text)["authUser"] == "alice"


@pytest.mark.parametrize(
    "path",
    [
        "/api/nodes",
        "/api/nodes/node-1/activate",
        "/api/ring0/status",
        "/api/ring0/query-client",
        "/api/ring0/send-message",
        "/api/ring0/respond-permission",
    ],
)
async def test_tightened_paths_allow_authenticated_bearer_tokens(path: str) -> None:
    response = await call_middleware(path, token="valid")
    assert response.status == 200
    assert json.loads(response.text)["authUser"] == "alice"


@pytest.mark.parametrize(
    "path",
    [
        "/api/nodes",
        "/api/nodes/node-1/activate",
    ],
)
async def test_tightened_node_paths_allow_authenticated_cookies(path: str) -> None:
    response = await call_middleware(path, cookie_token="valid")
    assert response.status == 200
    assert json.loads(response.text)["authUser"] == "alice"


def test_public_auth_surface_matches_audit_document() -> None:
    assert auth.PUBLIC_PREFIXES == (
        "/ws/cli/",
        "/ws/node/",
        "/api/auth/login",
        "/api/auth/me",
        "/api/pairing/request",
        "/api/pairing/status/",
        "/api/nodes/register",
        "/api/second-screen/pair-code",
        "/api/second-screen/status",
        "/assets/",
        "/sw.js",
        "/manifest.json",
        "/logo",
        "/favicon",
        "/apple-touch-icon",
    )
    assert auth.PUBLIC_EXACT_PATHS == frozenset()
    assert [pattern.pattern for pattern in auth._PUBLIC_PATH_PATTERNS] == []


def test_public_auth_surface_constants_are_named_in_audit_document() -> None:
    doc = Path("docs/public-auth-surface.md").read_text()
    for prefix in auth.PUBLIC_PREFIXES:
        assert prefix in doc
    for path in auth.PUBLIC_EXACT_PATHS:
        assert path in doc
    for pattern in auth._PUBLIC_PATH_PATTERNS:
        assert pattern.pattern in doc
