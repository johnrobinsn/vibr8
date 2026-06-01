"""Startup security guard tests."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.main import (
    resolve_bind_host,
    resolve_self_node_enabled,
    run_legacy_session_startup_sync,
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


def test_self_node_mode_is_enabled_by_default() -> None:
    assert resolve_self_node_enabled({}) is True


def test_disabling_self_node_requires_explicit_legacy_override() -> None:
    with pytest.raises(RuntimeError, match="legacy in-process fallback"):
        resolve_self_node_enabled({"VIBR8_DISABLE_SELF_NODE": "1"})


def test_legacy_in_process_mode_requires_two_explicit_flags(caplog) -> None:
    caplog.set_level(logging.WARNING)

    assert (
        resolve_self_node_enabled(
            {
                "VIBR8_DISABLE_SELF_NODE": "1",
                "VIBR8_ALLOW_LEGACY_IN_PROCESS": "1",
            }
        )
        is False
    )
    assert "legacy in-process node path" in caplog.text
    records = [
        record for record in caplog.records
        if getattr(record, "audit_event", "") == "legacy_in_process_mode_enabled"
    ]
    assert records[-1].env == "VIBR8_DISABLE_SELF_NODE"
    assert records[-1].allow_env == "VIBR8_ALLOW_LEGACY_IN_PROCESS"


def test_self_node_disable_flag_uses_env_truthiness() -> None:
    assert (
        resolve_self_node_enabled(
            {
                "VIBR8_DISABLE_SELF_NODE": "0",
                "VIBR8_ALLOW_LEGACY_IN_PROCESS": "1",
            }
        )
        is True
    )
    assert (
        resolve_self_node_enabled(
            {
                "VIBR8_DISABLE_SELF_NODE": "invalid",
                "VIBR8_ALLOW_LEGACY_IN_PROCESS": "1",
            }
        )
        is True
    )
    assert (
        resolve_self_node_enabled(
            {
                "VIBR8_DISABLE_SELF_NODE": "yes",
                "VIBR8_ALLOW_LEGACY_IN_PROCESS": "true",
            }
        )
        is False
    )


async def test_self_node_mode_skips_legacy_session_startup_sync() -> None:
    """Default self-node mode must not touch dormant launcher session state."""
    launcher = MagicMock()
    session_registry = SimpleNamespace(sync_from_launcher=AsyncMock())
    spawn_task = MagicMock()

    await run_legacy_session_startup_sync(
        use_self_node=True,
        launcher=launcher,
        ring0_manager=SimpleNamespace(session_id="ring0"),
        session_registry=session_registry,
        spawn_task=spawn_task,
    )

    launcher.get_starting_sessions.assert_not_called()
    session_registry.sync_from_launcher.assert_not_awaited()
    spawn_task.assert_not_called()


async def test_legacy_mode_runs_launcher_session_startup_sync() -> None:
    """Explicit legacy mode still syncs restored launcher sessions."""
    launcher = MagicMock()
    launcher.get_starting_sessions.return_value = []
    session_registry = SimpleNamespace(sync_from_launcher=AsyncMock())
    spawn_task = MagicMock()

    await run_legacy_session_startup_sync(
        use_self_node=False,
        launcher=launcher,
        ring0_manager=SimpleNamespace(session_id="ring0"),
        session_registry=session_registry,
        spawn_task=spawn_task,
    )

    launcher.get_starting_sessions.assert_called_once_with()
    session_registry.sync_from_launcher.assert_awaited_once_with("ring0")
    spawn_task.assert_not_called()


async def test_legacy_mode_schedules_reconnect_watchdog_for_starting_sessions() -> None:
    """Explicit legacy mode preserves the reconnect watchdog."""
    launcher = MagicMock()
    launcher.get_starting_sessions.return_value = [
        SimpleNamespace(sessionId="s1", archived=False),
    ]
    session_registry = SimpleNamespace(sync_from_launcher=AsyncMock())
    spawn_task = MagicMock()

    await run_legacy_session_startup_sync(
        use_self_node=False,
        launcher=launcher,
        ring0_manager=SimpleNamespace(session_id="ring0"),
        session_registry=session_registry,
        spawn_task=spawn_task,
    )

    launcher.get_starting_sessions.assert_called_once_with()
    session_registry.sync_from_launcher.assert_awaited_once_with("ring0")
    spawn_task.assert_called_once()
    scheduled = spawn_task.call_args.args[0]
    assert hasattr(scheduled, "__await__")
    scheduled.close()


def test_self_node_mode_only_wires_node_backed_relaunch_callback() -> None:
    """Default self-node mode wires relaunch but skips hub-local callbacks."""
    launcher = MagicMock()
    ws_bridge = MagicMock()
    on_cli_relaunch_needed = MagicMock()

    wire_session_callbacks(
        use_self_node=True,
        launcher=launcher,
        ws_bridge=ws_bridge,
        has_computer_use=True,
        on_computer_use_created=MagicMock(),
        on_cli_relaunch_needed=on_cli_relaunch_needed,
        on_first_turn_completed=MagicMock(),
    )

    launcher.on_computer_use_created.assert_not_called()
    ws_bridge.on_cli_relaunch_needed_callback.assert_called_once_with(
        on_cli_relaunch_needed
    )
    ws_bridge.on_first_turn_completed_callback.assert_not_called()


def test_legacy_mode_wires_in_process_callbacks() -> None:
    """Explicit legacy mode preserves hub-local launcher/session callbacks."""
    launcher = MagicMock()
    ws_bridge = MagicMock()
    on_computer_use_created = MagicMock()
    on_cli_relaunch_needed = MagicMock()
    on_first_turn_completed = MagicMock()

    wire_session_callbacks(
        use_self_node=False,
        launcher=launcher,
        ws_bridge=ws_bridge,
        has_computer_use=True,
        on_computer_use_created=on_computer_use_created,
        on_cli_relaunch_needed=on_cli_relaunch_needed,
        on_first_turn_completed=on_first_turn_completed,
    )

    launcher.on_computer_use_created.assert_called_once_with(on_computer_use_created)
    ws_bridge.on_cli_relaunch_needed_callback.assert_called_once_with(
        on_cli_relaunch_needed
    )
    ws_bridge.on_first_turn_completed_callback.assert_called_once_with(
        on_first_turn_completed
    )


def test_legacy_callback_wiring_respects_missing_computer_use() -> None:
    """Legacy mode must not register the CU callback if CU support is absent."""
    launcher = MagicMock()
    ws_bridge = MagicMock()

    wire_session_callbacks(
        use_self_node=False,
        launcher=launcher,
        ws_bridge=ws_bridge,
        has_computer_use=False,
        on_computer_use_created=MagicMock(),
        on_cli_relaunch_needed=MagicMock(),
        on_first_turn_completed=MagicMock(),
    )

    launcher.on_computer_use_created.assert_not_called()
    ws_bridge.on_cli_relaunch_needed_callback.assert_called_once()
    ws_bridge.on_first_turn_completed_callback.assert_called_once()
