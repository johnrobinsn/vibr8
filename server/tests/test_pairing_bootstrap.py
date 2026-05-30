"""Tests for public pairing/bootstrap security semantics."""

from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from server import auth as auth_module
from server.auth import AuthManager


def _audit_records(caplog, event: str):
    return [record for record in caplog.records if getattr(record, "audit_event", "") == event]


@pytest.fixture
def auth_manager(tmp_path, monkeypatch) -> AuthManager:
    monkeypatch.setattr(auth_module, "VIBR8_DIR", tmp_path)
    monkeypatch.setattr(auth_module, "DEVICE_TOKENS_FILE", tmp_path / "device-tokens.json")

    manager = AuthManager.__new__(AuthManager)
    manager._users = {"alice": "unused"}
    manager._secret = "test-secret"
    manager._device_tokens = []
    manager._revoked_device_sigs = set()
    manager._pairing_codes = {}
    manager._pairing_rate = {}
    manager._pairing_fails = {}
    return manager


@pytest.fixture
def app(auth_manager, tmp_path, monkeypatch):
    from server import routes as routes_module
    from server.routes import create_routes
    from vibr8_core.session_store import SessionStore
    from vibr8_core.worktree_tracker import WorktreeTracker
    from vibr8_core.ws_bridge import WsBridge

    monkeypatch.setattr(routes_module.Path, "home", lambda: tmp_path)

    launcher = MagicMock()
    bridge = WsBridge()
    store = SessionStore()
    routes = create_routes(
        launcher,
        bridge,
        store,
        worktree_tracker=WorktreeTracker(),
        auth_manager=auth_manager,
    )
    @web.middleware
    async def auth_user_middleware(request, handler):
        request["auth_user"] = "alice"
        return await handler(request)

    app = web.Application(middlewares=[auth_user_middleware])
    app.router.add_routes(routes)
    return app


def test_native_pairing_token_delivery_is_single_use(auth_manager) -> None:
    requested = auth_manager.request_pairing("native", "1.2.3.4")
    code = requested["code"]

    confirmed = auth_manager.confirm_pairing(code, "alice", "Laptop")
    assert confirmed is not None
    assert confirmed["type"] == "native"
    assert confirmed["tokenId"]

    first_status = auth_manager.get_pairing_status(code, "1.2.3.4")
    second_status = auth_manager.get_pairing_status(code, "1.2.3.4")

    assert first_status["status"] == "complete"
    assert first_status["token"].startswith("d:alice:")
    assert second_status == {"status": "expired"}


def test_second_screen_pairing_token_delivery_is_single_use(auth_manager) -> None:
    requested = auth_manager.request_pairing(
        "second-screen",
        "1.2.3.4",
        client_id="screen-1",
    )
    code = requested["code"]

    confirmed = auth_manager.confirm_pairing(code, "alice", "Kitchen")
    assert confirmed["type"] == "second-screen"
    assert confirmed["clientId"] == "screen-1"

    first_status = auth_manager.get_pairing_status(code, "1.2.3.4")
    second_status = auth_manager.get_pairing_status(code, "1.2.3.4")

    assert first_status["status"] == "complete"
    assert first_status["clientId"] == "screen-1"
    assert first_status["pairedUser"] == "alice"
    assert first_status["token"].startswith("d:alice:")
    assert second_status == {"status": "expired"}


def test_pairing_codes_cannot_be_confirmed_twice(auth_manager) -> None:
    native = auth_manager.request_pairing("native", "1.2.3.4")
    assert auth_manager.confirm_pairing(native["code"], "alice", "Laptop") is not None
    assert auth_manager.confirm_pairing(native["code"], "alice", "Laptop") is None

    second_screen = auth_manager.request_pairing(
        "second-screen",
        "1.2.3.4",
        client_id="screen-1",
    )
    assert auth_manager.confirm_pairing(second_screen["code"], "alice", "Kitchen") is not None
    assert auth_manager.confirm_pairing(second_screen["code"], "alice", "Kitchen") is None


def test_pairing_lifecycle_emits_audit_logs(auth_manager, caplog) -> None:
    caplog.set_level(logging.INFO)

    requested = auth_manager.request_pairing("native", "1.2.3.4")
    confirmed = auth_manager.confirm_pairing(requested["code"], "alice", "Laptop")
    delivered = auth_manager.get_pairing_status(requested["code"], "1.2.3.4")
    failed = auth_manager.confirm_pairing("000000", "alice", "Laptop")

    assert confirmed is not None
    assert delivered["status"] == "complete"
    assert failed is None

    created = _audit_records(caplog, "pairing_code_created")
    assert created[-1].pairing_type == "native"
    assert created[-1].ip == "1.2.3.4"

    confirmed_records = _audit_records(caplog, "pairing_confirmed")
    assert confirmed_records[-1].pairing_type == "native"
    assert confirmed_records[-1].username == "alice"
    assert confirmed_records[-1].token_id == confirmed["tokenId"]

    delivered_records = _audit_records(caplog, "pairing_token_delivered")
    assert delivered_records[-1].pairing_type == "native"
    assert delivered_records[-1].token_id == confirmed["tokenId"]

    failed_records = _audit_records(caplog, "pairing_confirm_failed")
    assert failed_records[-1].reason == "invalid_or_expired"
    assert failed_records[-1].username == "alice"


def test_expired_pairing_codes_cannot_be_confirmed_or_polled(auth_manager) -> None:
    requested = auth_manager.request_pairing("native", "1.2.3.4")
    code = requested["code"]
    auth_manager._pairing_codes[code]["expiresAt"] = time.time() - 1

    assert auth_manager.confirm_pairing(code, "alice", "Laptop") is None
    assert auth_manager.get_pairing_status(code, "1.2.3.4") == {"status": "expired"}


def test_pairing_request_rate_limit_tracks_recent_requests(auth_manager) -> None:
    ip = "1.2.3.4"
    for _ in range(auth_module.PAIRING_RATE_LIMIT):
        assert auth_manager.check_pairing_rate_limit(ip) is False
        auth_manager.request_pairing("native", ip)

    assert auth_manager.check_pairing_rate_limit(ip) is True


def test_pairing_status_bruteforce_cooldown_after_failed_lookups(auth_manager) -> None:
    ip = "1.2.3.4"
    for _ in range(auth_module.PAIRING_FAIL_THRESHOLD):
        assert auth_manager.check_pairing_brute_force(ip) is False
        assert auth_manager.get_pairing_status("000000", ip) == {"status": "expired"}

    assert auth_manager.check_pairing_brute_force(ip) is True


async def test_pairing_request_route_returns_429_after_rate_limit(app, caplog) -> None:
    caplog.set_level(logging.WARNING)
    async with TestClient(TestServer(app)) as client:
        for _ in range(auth_module.PAIRING_RATE_LIMIT):
            resp = await client.post("/api/pairing/request", json={"type": "native"})
            assert resp.status == 200

        limited = await client.post("/api/pairing/request", json={"type": "native"})
        assert limited.status == 429
        assert await limited.json() == {"error": "Too many requests"}

    records = _audit_records(caplog, "pairing_rate_limited")
    assert records[-1].path == "/api/pairing/request"


async def test_pairing_status_route_returns_429_after_failed_lookups(app, caplog) -> None:
    caplog.set_level(logging.WARNING)
    async with TestClient(TestServer(app)) as client:
        for _ in range(auth_module.PAIRING_FAIL_THRESHOLD):
            resp = await client.get("/api/pairing/status/000000")
            assert resp.status == 200
            assert await resp.json() == {"status": "expired"}

        limited = await client.get("/api/pairing/status/000000")
        assert limited.status == 429
        assert await limited.json() == {"error": "Too many requests"}

    records = _audit_records(caplog, "pairing_bruteforce_limited")
    assert records[-1].path == "/api/pairing/status"


async def test_pairing_confirm_route_logs_rejected_attempt_with_ip(app, caplog) -> None:
    caplog.set_level(logging.WARNING)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/pairing/confirm",
            json={"code": "000000", "name": "Laptop"},
        )
        body = await resp.json()

    assert resp.status == 400
    assert body == {"error": "Invalid or expired code"}
    records = _audit_records(caplog, "pairing_confirm_rejected")
    assert records[-1].path == "/api/pairing/confirm"
    assert records[-1].username == "alice"
    assert records[-1].ip


async def test_second_screen_pair_code_requires_client_id(app) -> None:
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/second-screen/pair-code", json={})
        body = await resp.json()

    assert resp.status == 400
    assert body == {"error": "clientId required"}


async def test_second_screen_pair_code_route_returns_429_after_rate_limit(app, caplog) -> None:
    caplog.set_level(logging.WARNING)
    async with TestClient(TestServer(app)) as client:
        for index in range(auth_module.PAIRING_RATE_LIMIT):
            resp = await client.post(
                "/api/second-screen/pair-code",
                json={"clientId": f"screen-{index}"},
            )
            assert resp.status == 200

        limited = await client.post(
            "/api/second-screen/pair-code",
            json={"clientId": "screen-limited"},
        )
        body = await limited.json()

    assert limited.status == 429
    assert body == {"error": "Too many requests"}
    records = _audit_records(caplog, "pairing_rate_limited")
    assert records[-1].path == "/api/second-screen/pair-code"


async def test_unified_status_delivers_second_screen_token_once(app, auth_manager) -> None:
    async with TestClient(TestServer(app)) as client:
        request_resp = await client.post(
            "/api/second-screen/pair-code",
            json={"clientId": "screen-1"},
        )
        assert request_resp.status == 200
        code = (await request_resp.json())["code"]

        confirm_result = auth_manager.confirm_pairing(code, "alice", "Kitchen")
        assert confirm_result is not None

        first = await client.get("/api/pairing/status/" + code)
        first_body = await first.json()
        second = await client.get("/api/pairing/status/" + code)
        second_body = await second.json()

    assert first.status == 200
    assert first_body["status"] == "complete"
    assert first_body["clientId"] == "screen-1"
    assert first_body["token"].startswith("d:alice:")

    assert second.status == 200
    assert second_body == {"status": "expired"}


async def test_legacy_second_screen_status_delivers_pending_token_once(app) -> None:
    async with TestClient(TestServer(app)) as client:
        request_resp = await client.post(
            "/api/second-screen/pair-code",
            json={"clientId": "screen-legacy"},
        )
        assert request_resp.status == 200
        code = (await request_resp.json())["code"]

        pair_resp = await client.post(
            "/api/second-screen/pair",
            json={"code": code, "username": "alice", "name": "Kitchen"},
        )
        pair_body = await pair_resp.json()
        assert pair_resp.status == 200
        assert pair_body == {"ok": True, "secondScreenClientId": "screen-legacy"}

        first = await client.get(
            "/api/second-screen/status",
            params={"clientId": "screen-legacy"},
        )
        first_body = await first.json()
        second = await client.get(
            "/api/second-screen/status",
            params={"clientId": "screen-legacy"},
        )
        second_body = await second.json()

    assert first.status == 200
    assert first_body["paired"] is True
    assert first_body["role"] == "secondscreen"
    assert first_body["pairedUser"] == "alice"
    assert first_body["deviceToken"].startswith("d:alice:")

    assert second.status == 200
    assert second_body["paired"] is True
    assert second_body["role"] == "secondscreen"
    assert "deviceToken" not in second_body
