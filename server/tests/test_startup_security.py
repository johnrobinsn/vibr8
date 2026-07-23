"""Startup security guard tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from server.main import (
    resolve_bind_host,
    wire_session_callbacks,
)


def test_auth_enabled_uses_requested_bind_host() -> None:
    assert resolve_bind_host(True, {"VIBR8_HOST": "0.0.0.0"}) == "0.0.0.0"
    assert resolve_bind_host(True, {"VIBR8_HOST": "127.0.0.1"}) == "127.0.0.1"


def test_no_auth_refuses_start_without_explicit_allow() -> None:
    with pytest.raises(RuntimeError, match="Refusing to start without auth"):
        resolve_bind_host(False, {})


def test_no_auth_allows_explicit_loopback_bind() -> None:
    assert (
        resolve_bind_host(False, {"VIBR8_ALLOW_NO_AUTH": "1", "VIBR8_HOST": "localhost"})
        == "localhost"
    )
    assert (
        resolve_bind_host(False, {"VIBR8_ALLOW_NO_AUTH": "true", "VIBR8_HOST": "::1"})
        == "::1"
    )


def test_no_auth_defaults_to_loopback_when_explicitly_allowed() -> None:
    assert resolve_bind_host(False, {"VIBR8_ALLOW_NO_AUTH": "1"}) == "127.0.0.1"


def test_no_auth_forces_requested_public_bind_to_loopback() -> None:
    assert (
        resolve_bind_host(False, {"VIBR8_ALLOW_NO_AUTH": "1", "VIBR8_HOST": "0.0.0.0"})
        == "127.0.0.1"
    )


def test_no_auth_public_bind_requires_second_explicit_override() -> None:
    assert (
        resolve_bind_host(
            False,
            {
                "VIBR8_ALLOW_NO_AUTH": "1",
                "VIBR8_ALLOW_PUBLIC_NO_AUTH": "1",
                "VIBR8_HOST": "0.0.0.0",
            },
        )
        == "0.0.0.0"
    )


def test_wire_session_callbacks_registers_only_relaunch_hint() -> None:
    """The stateless hub wires only the cross-node relaunch callback —
    everything else (computer-use creation, first-turn auto-naming) lives
    on the node that owns the session."""
    ws_bridge = MagicMock()
    on_cli_relaunch_needed = MagicMock()

    wire_session_callbacks(
        ws_bridge=ws_bridge,
        on_cli_relaunch_needed=on_cli_relaunch_needed,
    )

    ws_bridge.on_cli_relaunch_needed_callback.assert_called_once_with(
        on_cli_relaunch_needed
    )
    ws_bridge.on_first_turn_completed_callback.assert_not_called()
