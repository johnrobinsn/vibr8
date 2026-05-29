"""Regression tests for the six post-node-parity-refactor bugs.

Each test pins one of the failure modes that escaped CI during the
refactor. Cheap unit-level coverage — the bugs themselves are all at
process/instance boundaries that mocks would otherwise hide, but the
*guard conditions* introduced by the fixes are testable directly.

Bugs covered:
1. Ring0 MCP `mcp_script` paths must resolve to a real file.
2. CliLauncher with `scheme="ws"` must build `ws://` URLs, not `wss://`.
3. Auto-relaunch must fire when state="starting" lingers from a dead
   PID (no live process tracked in `_processes`).
4. `WsBridge.handle_browser_close` must not orphan surviving tabs that
   share a `client_id`.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vibr8_core import ring0
from vibr8_core.cli_launcher import (
    CliLauncher,
    SdkSessionInfo,
    _RelaunchOptions,
)
from vibr8_core.ws_bridge import WsBridge


# ── 1. ring0_mcp script path ────────────────────────────────────────────────


def test_ring0_mcp_script_path_resolves() -> None:
    """The mcp_script paths constructed in ring0.py must exist on disk.

    Previously hardcoded as `server/ring0_mcp.py` after the file moved to
    `vibr8_core/ring0_mcp.py`. Claude CLI silently failed to load
    `--mcp-config` → Ring0 ran with zero `mcp__vibr8__*` tools.
    """
    source = Path(ring0.__file__).read_text()
    candidates = re.findall(
        r'server_dir\s*/\s*"([^"]+)"\s*/\s*"(ring0_mcp\.py)"',
        source,
    )
    assert candidates, "no mcp_script paths found in ring0.py (regex stale?)"
    server_dir = Path(ring0.__file__).parent.parent.resolve()
    missing = [
        (subdir, fname)
        for subdir, fname in candidates
        if not (server_dir / subdir / fname).exists()
    ]
    assert not missing, (
        f"ring0.py constructs mcp_script paths that don't exist: {missing}. "
        f"Did ring0_mcp.py move again?"
    )


# ── 2. CliLauncher scheme override ──────────────────────────────────────────


async def test_cli_launcher_scheme_ws_produces_ws_url(tmp_path) -> None:
    """Node's local server is plain HTTP — scheme="ws" must win regardless
    of whether the hub's certs/ exist."""
    captured: dict = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        proc = MagicMock()
        proc.pid = 12345
        proc.returncode = None
        proc.wait = AsyncMock(return_value=0)
        proc.stdout = None
        proc.stderr = None
        return proc

    launcher = CliLauncher(port=3459, scheme="ws")
    info = SdkSessionInfo(sessionId="test-sess", cwd=str(tmp_path), backendType="claude")
    options = _RelaunchOptions(cwd=str(tmp_path))

    with patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec):
        await launcher._spawn_cli("test-sess", info, options)

    argv = captured.get("args", ())
    sdk_url_idx = argv.index("--sdk-url") + 1 if "--sdk-url" in argv else None
    assert sdk_url_idx is not None, f"no --sdk-url in argv: {argv}"
    sdk_url = argv[sdk_url_idx]
    assert sdk_url.startswith("ws://"), (
        f"expected ws:// scheme on node CLI spawn, got {sdk_url!r}. "
        f"Auto-detect picked wss based on hub certs?"
    )


async def test_cli_launcher_scheme_default_uses_certs(tmp_path) -> None:
    """Without scheme override, auto-detect from certs/ still works
    (this is the hub's path)."""
    captured: dict = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        proc = MagicMock()
        proc.pid = 12345
        proc.stdout = None
        proc.stderr = None
        proc.wait = AsyncMock(return_value=0)
        return proc

    launcher = CliLauncher(port=3456)  # no scheme
    info = SdkSessionInfo(sessionId="t", cwd=str(tmp_path), backendType="claude")
    options = _RelaunchOptions(cwd=str(tmp_path))

    with patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec):
        await launcher._spawn_cli("t", info, options)

    argv = captured["args"]
    sdk_url = argv[argv.index("--sdk-url") + 1]
    # Whichever scheme the auto-detect picks, it must be one of these two.
    assert sdk_url.startswith(("ws://", "wss://")), sdk_url


# ── 3. Stale "starting" state must not block relaunch ───────────────────────


def test_can_relaunch_allows_starting_with_no_live_process() -> None:
    """After a restart, `restore_from_disk` can pin a session at
    "starting" with a dead PID. `can_relaunch` must still return True
    so queued user_messages get processed — the post-restart deadlock
    was caused by gating on `state != "starting"` instead.
    """
    launcher = CliLauncher(port=3459, scheme="ws")
    sid = "session-with-dead-starting-state"
    launcher._sessions[sid] = SdkSessionInfo(
        sessionId=sid,
        pid=99999999,
        state="starting",
        backendType="claude",
    )
    assert sid not in launcher._processes  # no spawn in flight
    assert launcher.can_relaunch(sid)


def test_can_relaunch_blocks_when_spawn_in_flight() -> None:
    """If `_processes` has a live entry, don't double-spawn."""
    launcher = CliLauncher(port=3459, scheme="ws")
    sid = "session-spawning"
    launcher._sessions[sid] = SdkSessionInfo(sessionId=sid, state="starting", backendType="claude")
    launcher._processes[sid] = MagicMock()
    assert not launcher.can_relaunch(sid)


def test_can_relaunch_blocks_archived() -> None:
    """Archived sessions must never be auto-relaunched."""
    launcher = CliLauncher(port=3459, scheme="ws")
    sid = "session-archived"
    launcher._sessions[sid] = SdkSessionInfo(
        sessionId=sid, state="exited", archived=True, backendType="claude",
    )
    assert not launcher.can_relaunch(sid)


def test_can_relaunch_blocks_unknown_session() -> None:
    """No session, nothing to relaunch."""
    launcher = CliLauncher(port=3459, scheme="ws")
    assert not launcher.can_relaunch("not-a-real-session")


# ── 4. Per-tab browser close must not orphan surviving tabs ─────────────────


def _fake_ws(closed: bool = False) -> MagicMock:
    """Mock ws_response that satisfies the lookups WsBridge does on close."""
    ws = MagicMock()
    ws.closed = closed
    return ws


async def test_browser_close_promotes_survivor_for_same_client() -> None:
    """Two tabs share client_id; closing one must promote the other's ws
    into `_ws_by_client`, not orphan it."""
    bridge = WsBridge()
    session_id = "sess-A"
    client_id = "client-shared"

    ws_a = _fake_ws()
    ws_b = _fake_ws()

    await bridge.handle_browser_open(ws_a, session_id, client_id=client_id)
    await bridge.handle_browser_open(ws_b, session_id, client_id=client_id)

    # After both connects, _ws_by_client should have the most recent (ws_b).
    assert bridge._ws_by_client[client_id] is ws_b
    assert bridge._client_sessions[client_id] == session_id

    # Close ws_b (the registered one). ws_a is still open in browser_sockets
    # → cleanup must promote ws_a, not clear.
    ws_b.closed = True  # simulate closed state
    await bridge.handle_browser_close(ws_b)

    assert client_id in bridge._client_sessions, (
        "closing one tab orphaned a surviving tab's client tracking — "
        "regression of the per-tab close bug"
    )
    assert bridge._ws_by_client[client_id] is ws_a


async def test_browser_close_clears_when_no_survivor() -> None:
    """Single-tab case: closing the only ws for a client must clear
    the tracking dicts."""
    bridge = WsBridge()
    session_id = "sess-B"
    client_id = "client-solo"

    ws = _fake_ws()
    await bridge.handle_browser_open(ws, session_id, client_id=client_id)
    assert client_id in bridge._ws_by_client

    ws.closed = True
    await bridge.handle_browser_close(ws)
    assert client_id not in bridge._ws_by_client
    assert client_id not in bridge._client_sessions


async def test_browser_close_ignores_non_tracked_ws() -> None:
    """If the closing ws is NOT the currently-tracked one (e.g. an older
    tab closing after a newer connect overwrote `_ws_by_client`), the
    cleanup must leave the tracked ws alone."""
    bridge = WsBridge()
    session_id = "sess-C"
    client_id = "client-multi"

    ws_old = _fake_ws()
    ws_new = _fake_ws()

    await bridge.handle_browser_open(ws_old, session_id, client_id=client_id)
    await bridge.handle_browser_open(ws_new, session_id, client_id=client_id)
    # ws_new is now the tracked one.
    assert bridge._ws_by_client[client_id] is ws_new

    ws_old.closed = True
    await bridge.handle_browser_close(ws_old)

    # ws_new must still be tracked.
    assert bridge._ws_by_client[client_id] is ws_new
    assert bridge._client_sessions[client_id] == session_id
