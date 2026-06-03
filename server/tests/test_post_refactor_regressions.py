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
from types import SimpleNamespace
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


# ── 5. Dead self-node sessions must request relaunch on browser connect ─────


async def test_self_node_remote_session_requests_relaunch_on_dead_backend() -> None:
    """Self-node sessions are remote-qualified on the hub bridge.

    Browser reconnect to a dead self-node session must still request a
    relaunch; otherwise prompts can pile up after the node-side adapter exits.
    """
    bridge = WsBridge()
    node_id = "self-node"
    raw_session_id = "sess-codex"
    session_id = f"{node_id}:{raw_session_id}"
    bridge.set_self_node_id(node_id)
    bridge._node_registry = MagicMock()
    bridge._node_registry.get_node.return_value = SimpleNamespace(
        tunnel=SimpleNamespace(connected=False),
    )

    fired_for: list[str] = []
    bridge.on_cli_relaunch_needed_callback(fired_for.append)

    await bridge.handle_browser_open(_fake_ws(), session_id, client_id="client-1")

    assert fired_for == [session_id], (
        "browser reconnect to a dead self-node-qualified session did not "
        "request node-backed relaunch"
    )


async def test_remote_session_dead_backend_does_not_request_self_node_relaunch() -> None:
    """Disconnected non-self remote nodes must not relaunch through self-node."""
    bridge = WsBridge()
    bridge.set_self_node_id("self-node")
    session_id = "remote-node:sess-codex"
    bridge._node_registry = MagicMock()
    bridge._node_registry.get_node.return_value = SimpleNamespace(
        tunnel=SimpleNamespace(connected=False),
    )

    fired_for: list[str] = []
    bridge.on_cli_relaunch_needed_callback(fired_for.append)

    await bridge.handle_browser_open(_fake_ws(), session_id, client_id="client-1")

    assert fired_for == []


async def test_remote_session_dead_backend_without_self_node_id_does_not_relaunch() -> None:
    """Before self-node registration, qualified sessions remain non-relaunchable."""
    bridge = WsBridge()
    session_id = "self-node:sess-codex"
    bridge._node_registry = MagicMock()
    bridge._node_registry.get_node.return_value = SimpleNamespace(
        tunnel=SimpleNamespace(connected=False),
    )

    fired_for: list[str] = []
    bridge.on_cli_relaunch_needed_callback(fired_for.append)

    await bridge.handle_browser_open(_fake_ws(), session_id, client_id="client-1")

    assert fired_for == []


# ── 6. Adapter backends must request a relaunch when queuing ────────────────


from vibr8_core.ws_bridge import Session  # noqa: E402  (kept near the test)


@pytest.mark.parametrize("backend_type", ["codex", "opencode", "hermes"])
async def test_adapter_backend_queues_user_message_and_requests_relaunch(
    backend_type: str,
) -> None:
    """When a codex/opencode/hermes session has no adapter attached, a
    user_message must both queue *and* fire `_on_cli_relaunch_needed` so
    the node-side launcher respawns the adapter subprocess.

    Pre-fix, only the queue happened — after any hub/self-node restart,
    adapter-backend sessions sat with `adapter=None` forever and every
    prompt added to `pending_messages` without ever waking a relaunch.
    The claude path doesn't have this gap (it goes through
    `_handle_user_message`, which fires the hook).
    """
    bridge = WsBridge()
    session_id = f"sess-{backend_type}"
    session = Session(id=session_id, backend_type=backend_type)
    session.adapter = None
    bridge._sessions[session_id] = session

    fired_for: list[str] = []
    bridge.on_cli_relaunch_needed_callback(fired_for.append)

    await bridge._route_browser_message(
        session, {"type": "user_message", "content": "hello"}
    )

    assert fired_for == [session_id], (
        f"adapter-backend ({backend_type}) failed to request relaunch when "
        f"queuing user_message — sessions stay stuck after a hub restart"
    )
    assert len(session.pending_messages) == 1


async def test_adapter_backend_does_not_request_relaunch_when_attached() -> None:
    """Negative case: when the adapter is attached, the relaunch hook
    must not fire (or we'd spin up duplicate subprocesses each time the
    user types)."""
    bridge = WsBridge()
    session_id = "sess-codex-live"
    session = Session(id=session_id, backend_type="codex")
    session.adapter = MagicMock()
    bridge._sessions[session_id] = session

    fired_for: list[str] = []
    bridge.on_cli_relaunch_needed_callback(fired_for.append)

    await bridge._route_browser_message(
        session, {"type": "user_message", "content": "hello"}
    )

    assert fired_for == []
    session.adapter.send_browser_message.assert_called_once()
    assert session.pending_messages == []


# ── 7. Spawn must only be called inside a running event loop ────────────────


import ast  # noqa: E402


def _main_py_path() -> Path:
    """Resolve server/main.py from the test's installed location."""
    import server.main as _server_main
    return Path(_server_main.__file__)


def test_spawn_uses_get_running_loop_create_task() -> None:
    """The `spawn()` helper in `create_app()` must attach tasks to the
    *running* loop, not whichever loop `asyncio.get_event_loop()` invents.

    Before the fix, `spawn()` called `asyncio.ensure_future(coro)`. When
    invoked at create_app() top level (before web.run_app's loop exists),
    `ensure_future` silently created a brand-new "current thread" loop
    and attached the task there. At shutdown, `asyncio.gather(*background_tasks)`
    saw a task belonging to a different loop and raised `ValueError:
    The future belongs to a different loop`, aborting `on_shutdown`
    mid-flight and leaving the process zombied (listeners gone,
    interpreter still sleeping).
    """
    source = _main_py_path().read_text()
    assert "asyncio.get_running_loop().create_task" in source, (
        "server/main.py:spawn() must use asyncio.get_running_loop().create_task(coro). "
        "Falling back to asyncio.ensure_future(coro) re-introduces the "
        "loop-mismatch shutdown bug."
    )
    assert "asyncio.ensure_future(coro)" not in source, (
        "Detected `asyncio.ensure_future(coro)` in server/main.py — "
        "use `asyncio.get_running_loop().create_task(coro)` instead."
    )


def test_no_spawn_call_directly_inside_create_app_body() -> None:
    """The `spawn(...)` helper must not be invoked at the top level of
    `create_app()`'s body — at that point `web.run_app` hasn't started
    its loop yet, so `get_running_loop()` would raise.

    Other call sites (inside nested `async def` callbacks, route
    handlers, registered event hooks) are fine — by the time they run
    the loop is up. The bug was specifically the historical
    `spawn(warmup_voice_models())` at create_app() module body level.
    """
    tree = ast.parse(_main_py_path().read_text())

    create_app: ast.FunctionDef | None = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "create_app":
            create_app = node
            break
    assert create_app is not None, "could not locate create_app() in server/main.py"

    # Walk only the direct statement children of create_app's body.
    # Anything nested inside an inner `async def` or `def` is fine —
    # those run later, when the loop exists.
    def _is_spawn_call(expr: ast.AST) -> bool:
        return (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Name)
            and expr.func.id == "spawn"
        )

    offending: list[int] = []
    for stmt in ast.walk(ast.Module(body=create_app.body, type_ignores=[])):
        # Skip into nested function bodies — their call sites are evaluated
        # later, after the loop is running.
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt is not create_app:
            continue
        for node in ast.walk(stmt):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Don't descend further than the first-level body
                continue
            if _is_spawn_call(node):
                offending.append(node.lineno)

    # Filter out any spawn() inside a nested def (we want only the truly
    # top-level body of create_app).
    def _enclosing_func(target_lineno: int) -> ast.AST | None:
        for inner in ast.walk(create_app):
            if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)) and inner is not create_app:
                if inner.lineno < target_lineno <= (inner.end_lineno or inner.lineno):
                    return inner
        return None

    truly_top_level = [ln for ln in offending if _enclosing_func(ln) is None]

    assert not truly_top_level, (
        f"spawn() called at create_app() top level at server/main.py "
        f"line(s) {truly_top_level}. No loop is running there — task "
        f"lands on a phantom loop and shutdown's "
        f"`asyncio.gather(*background_tasks)` raises ValueError."
    )
