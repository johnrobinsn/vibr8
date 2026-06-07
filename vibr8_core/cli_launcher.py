"""Manage CLI backend processes (Claude Code via --sdk-url WebSocket,
or Codex via app-server stdio).

Originally ported from The Vibe Companion (cli-launcher.ts).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import shutil
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Literal, Optional

if TYPE_CHECKING:
    from vibr8_core.codex_adapter import CodexAdapter
    from vibr8_core.session_store import SessionStore

from vibr8_core.session_types import BackendType

logger = logging.getLogger(__name__)


# ─── Data Types ──────────────────────────────────────────────────────────────


@dataclass
class SdkSessionInfo:
    sessionId: str
    pid: Optional[int] = None
    state: Literal["starting", "connected", "running", "exited"] = "starting"
    exitCode: Optional[int] = None
    model: Optional[str] = None
    permissionMode: Optional[str] = None
    cwd: str = ""
    createdAt: float = 0.0
    cliSessionId: Optional[str] = None
    archived: Optional[bool] = None
    isWorktree: Optional[bool] = None
    repoRoot: Optional[str] = None
    branch: Optional[str] = None
    actualBranch: Optional[str] = None
    name: Optional[str] = None
    nodeId: Optional[str] = None
    backendType: Optional[BackendType] = None
    mcpConfig: Optional[str] = None
    agentType: Optional[str] = None
    agentConfig: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Strip None values for cleaner JSON
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SdkSessionInfo:
        # Only pass fields that the dataclass knows about
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


@dataclass
class WorktreeInfo:
    isWorktree: bool
    repoRoot: str
    branch: str
    actualBranch: str
    worktreePath: str


@dataclass
class LaunchOptions:
    model: Optional[str] = None
    permissionMode: Optional[str] = None
    cwd: Optional[str] = None
    claudeBinary: Optional[str] = None
    codexBinary: Optional[str] = None
    opencodeBinary: Optional[str] = None
    hermesBinary: Optional[str] = None
    allowedTools: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    backendType: Optional[BackendType] = None
    worktreeInfo: Optional[WorktreeInfo] = None
    mcpConfig: Optional[str] = None
    sessionId: Optional[str] = None
    resumeSessionId: Optional[str] = None
    nodeId: Optional[str] = None
    agentType: Optional[str] = None
    agentConfig: Optional[Dict[str, Any]] = None
    isBackgroundTask: bool = False


@dataclass
class _RelaunchOptions:
    """Internal options for spawnCLI that includes resumeSessionId."""

    model: Optional[str] = None
    permissionMode: Optional[str] = None
    cwd: Optional[str] = None
    claudeBinary: Optional[str] = None
    codexBinary: Optional[str] = None
    opencodeBinary: Optional[str] = None
    allowedTools: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    backendType: Optional[BackendType] = None
    worktreeInfo: Optional[WorktreeInfo] = None
    resumeSessionId: Optional[str] = None
    mcpConfig: Optional[str] = None
    isBackgroundTask: bool = False


# ─── CliLauncher ─────────────────────────────────────────────────────────────


def _codex_rollout_exists(thread_id: str) -> bool:
    """Return True if codex has a rollout file on disk for `thread_id`.

    Codex names rollouts as
    `~/.codex/sessions/{YYYY}/{MM}/{DD}/rollout-{timestamp}-{thread_id}.jsonl`
    and refuses `thread/resume` with "no rollout found for thread id <X>"
    when the file is missing. Used by `_spawn_codex` to clear a stale
    cliSessionId before it can produce a user-visible resume error.

    The codex sessions tree is shallow (year/month/day directories of
    jsonl files) so the recursive glob is cheap.
    """
    if not thread_id:
        return False
    base = Path.home() / ".codex" / "sessions"
    if not base.is_dir():
        return False
    # Codex's filename pattern ends with `-{thread_id}.jsonl`.
    try:
        for _ in base.glob(f"**/rollout-*-{thread_id}.jsonl"):
            return True
    except OSError:
        return False
    return False


class CliLauncher:
    """Manages CLI backend processes (Claude Code via --sdk-url WebSocket,
    or Codex via app-server stdio)."""

    def __init__(self, port: int, scheme: Optional[str] = None) -> None:
        self._sessions: Dict[str, SdkSessionInfo] = {}
        self._processes: Dict[str, asyncio.subprocess.Process] = {}
        self._port = port
        # scheme = "ws" or "wss". None = auto-detect from cert files (hub).
        # vibr8_node always passes "ws" because its local server is plain HTTP.
        self._scheme_override = scheme
        self._store: Optional[SessionStore] = None
        self._on_adapter: Optional[
            Callable[[str, Any, str], None]  # (session_id, adapter, backend_type)
        ] = None
        self._on_computer_use_created: Optional[
            Callable[[str, SdkSessionInfo], None]
        ] = None
        # Keep references to background tasks so they are not GC'd
        self._monitor_tasks: Dict[str, asyncio.Task[None]] = {}
        self._pipe_tasks: List[asyncio.Task[None]] = []

    # ── Callbacks / Store ───────────────────────────────────────────────────

    def on_codex_adapter_created(
        self,
        cb: Callable[[str, Any, str], None],
    ) -> None:
        """Register a callback for when an adapter (Codex/OpenCode) is created
        (WsBridge needs to attach it)."""
        self._on_adapter = cb

    def on_computer_use_created(
        self,
        cb: Callable[[str, SdkSessionInfo], None],
    ) -> None:
        """Register a callback for when a computer-use session is created
        (WsBridge needs to attach the agent)."""
        self._on_computer_use_created = cb

    def set_store(self, store: SessionStore) -> None:
        """Attach a persistent store for surviving server restarts."""
        self._store = store

    # ── Persistence ─────────────────────────────────────────────────────────

    def _persist_state(self) -> None:
        """Persist launcher state to disk."""
        if not self._store:
            return
        data = [info.to_dict() for info in self._sessions.values()]
        self._store.save_launcher(data)

    def _mcp_scheme(self) -> str:
        if self._scheme_override:
            return "https" if self._scheme_override == "wss" else "http"
        cert_dir = Path(__file__).parent.parent / "certs"
        return "https" if (cert_dir / "cert.pem").exists() else "http"

    def _mcp_env(
        self,
        session_id: str,
        backend_type: str,
        model: Optional[str] = None,
        cwd: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        env: Dict[str, str] = {
            "VIBR8_PORT": str(self._port),
            "VIBR8_SCHEME": self._mcp_scheme(),
            "VIBR8_SESSION_ID": session_id,
            "VIBR8_BACKEND": backend_type,
        }
        if model:
            env["VIBR8_MODEL"] = model
        if cwd:
            env["VIBR8_CWD"] = cwd
        if extra_env and extra_env.get("VIBR8_TOKEN"):
            env["VIBR8_TOKEN"] = extra_env["VIBR8_TOKEN"]
        return env

    @staticmethod
    def _mcp_command() -> tuple[str, list[str]]:
        server_dir = Path(__file__).parent.parent.resolve()
        mcp_script = str(server_dir / "vibr8_core" / "ring0_mcp.py")
        uv_bin = shutil.which("uv") or "uv"
        return uv_bin, ["run", "--project", str(server_dir), "--no-sync", "python", mcp_script]

    @staticmethod
    def _session_mcp_config_path(session_id: str) -> Path:
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id).strip("._")
        if not safe_id:
            safe_id = "session"
        return Path.home() / ".vibr8" / "mcp-configs" / f"{safe_id}.json"

    def _write_session_mcp_config(
        self,
        session_id: str,
        backend_type: str,
        cwd: str,
        model: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> str:
        command, args = self._mcp_command()
        config = {
            "mcpServers": {
                "vibr8": {
                    "type": "stdio",
                    "command": command,
                    "args": args,
                    "env": self._mcp_env(session_id, backend_type, model, cwd, extra_env),
                }
            }
        }
        path = self._session_mcp_config_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2))
        return str(path)

    def _build_acp_mcp_servers(
        self,
        session_id: str,
        backend_type: str,
        cwd: str,
        model: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> list[dict[str, Any]]:
        command, args = self._mcp_command()
        env = self._mcp_env(session_id, backend_type, model, cwd, extra_env)
        return [{
            "name": "vibr8",
            "command": command,
            "args": args,
            "env": [{"name": k, "value": v} for k, v in env.items()],
        }]

    def restore_from_disk(self) -> int:
        """Restore sessions from disk and check which PIDs are still alive.

        Returns the number of recovered (still-alive) sessions.
        """
        if not self._store:
            return 0
        data = self._store.load_launcher()
        if not data or not isinstance(data, list):
            return 0

        recovered = 0
        for raw in data:
            info = SdkSessionInfo.from_dict(raw) if isinstance(raw, dict) else raw
            if info.sessionId in self._sessions:
                continue

            # Check if the process is still alive
            if info.pid and info.state != "exited":
                try:
                    os.kill(info.pid, 0)  # signal 0 = just check if alive
                    info.state = "starting"  # WS not yet re-established
                    self._sessions[info.sessionId] = info
                    recovered += 1
                except OSError:
                    # Process is dead
                    info.state = "exited"
                    info.exitCode = -1
                    self._sessions[info.sessionId] = info
            else:
                # Already exited or no PID
                self._sessions[info.sessionId] = info

        if recovered > 0:
            logger.info("Recovered %d live session(s) from disk", recovered)
        return recovered

    # ── Launch / Relaunch ───────────────────────────────────────────────────

    def launch(self, options: Optional[LaunchOptions] = None) -> SdkSessionInfo:
        """Launch a new CLI session (Claude Code or Codex).

        The subprocess is spawned as a fire-and-forget coroutine on the
        running event loop; the method itself is synchronous so callers
        can use it from both sync and async contexts.
        """
        if options is None:
            options = LaunchOptions()

        session_id = options.sessionId or str(uuid.uuid4())
        cwd = options.cwd or os.getcwd()
        backend_type: BackendType = options.backendType or "claude"

        info = SdkSessionInfo(
            sessionId=session_id,
            state="starting",
            model=options.model,
            permissionMode=options.permissionMode,
            cwd=cwd,
            createdAt=time.time() * 1000,  # ms since epoch, matching JS Date.now()
            backendType=backend_type,
        )

        # Store MCP config path for relaunch
        if options.mcpConfig:
            info.mcpConfig = options.mcpConfig

        # Store agent config (e.g. Hermes MCP servers) for relaunch
        if options.agentConfig:
            info.agentConfig = options.agentConfig

        if backend_type not in ("computer-use", "terminal"):
            if backend_type == "claude" and not info.mcpConfig:
                info.mcpConfig = self._write_session_mcp_config(
                    session_id,
                    backend_type,
                    cwd,
                    model=options.model,
                    extra_env=options.env,
                )
                options.mcpConfig = info.mcpConfig
            elif backend_type == "hermes" and not info.agentConfig:
                info.agentConfig = {
                    "mcpServers": self._build_acp_mcp_servers(
                        session_id,
                        backend_type,
                        cwd,
                        model=options.model,
                        extra_env=options.env,
                    )
                }
                options.agentConfig = info.agentConfig

        # Store worktree metadata if provided
        if options.worktreeInfo:
            info.isWorktree = options.worktreeInfo.isWorktree
            info.repoRoot = options.worktreeInfo.repoRoot
            info.branch = options.worktreeInfo.branch
            info.actualBranch = options.worktreeInfo.actualBranch

        if options.nodeId:
            info.nodeId = options.nodeId

        self._sessions[session_id] = info

        # Inject worktree guardrails for any backend that supports project-level
        # agent context (Claude reads .claude/CLAUDE.md; codex/opencode/hermes
        # read AGENTS.md). Skipped for terminal/computer-use.
        if info.isWorktree and info.branch and backend_type not in ("computer-use", "terminal"):
            parent_branch: Optional[str] = None
            if info.actualBranch and info.actualBranch != info.branch:
                parent_branch = info.branch
            self._inject_worktree_guardrails(
                info.cwd,
                info.actualBranch or info.branch,
                info.repoRoot or "",
                parent_branch,
                backend_type=backend_type,
            )

        if backend_type == "computer-use":
            # Computer-use runs in-process — no subprocess to spawn
            info.state = "connected"
            if self._on_computer_use_created:
                self._on_computer_use_created(session_id, info)
            self._persist_state()
            return info

        if backend_type == "opencode":
            asyncio.ensure_future(self._spawn_opencode(session_id, info, options))
        elif backend_type == "codex":
            asyncio.ensure_future(self._spawn_codex(session_id, info, options))
        elif backend_type == "hermes":
            asyncio.ensure_future(self._spawn_hermes(session_id, info, options))
        else:
            relaunch_opts = _RelaunchOptions(
                model=options.model,
                permissionMode=options.permissionMode,
                cwd=options.cwd,
                claudeBinary=options.claudeBinary,
                codexBinary=options.codexBinary,
                allowedTools=options.allowedTools,
                env=options.env,
                backendType=options.backendType,
                worktreeInfo=options.worktreeInfo,
                mcpConfig=options.mcpConfig,
                resumeSessionId=options.resumeSessionId,
                isBackgroundTask=options.isBackgroundTask,
            )
            asyncio.ensure_future(self._spawn_cli(session_id, info, relaunch_opts))

        return info

    async def relaunch(self, session_id: str) -> bool:
        """Relaunch a CLI process for an existing session.

        Kills the old process if still alive, then spawns a fresh CLI
        that connects back to the same session in the WsBridge.
        """
        info = self._sessions.get(session_id)
        if not info:
            return False

        # Kill old process if still alive
        old_proc = self._processes.get(session_id)
        if old_proc:
            try:
                old_proc.terminate()
                try:
                    await asyncio.wait_for(old_proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
            except ProcessLookupError:
                pass
            self._processes.pop(session_id, None)
        elif info.pid:
            # Process from a previous server instance -- kill by PID
            try:
                os.kill(info.pid, signal.SIGTERM)
            except OSError:
                pass

        info.state = "starting"

        # Re-inject worktree guardrails (matches launch() behavior; useful if
        # the user deleted the context file between sessions).
        backend_type = info.backendType or "claude"
        if info.isWorktree and info.branch and backend_type not in ("computer-use", "terminal"):
            parent_branch: Optional[str] = None
            if info.actualBranch and info.actualBranch != info.branch:
                parent_branch = info.branch
            self._inject_worktree_guardrails(
                info.cwd,
                info.actualBranch or info.branch,
                info.repoRoot or "",
                parent_branch,
                backend_type=backend_type,
            )

        if info.backendType == "computer-use":
            # Computer-use is in-process — just mark connected and re-create
            info.state = "connected"
            if self._on_computer_use_created:
                self._on_computer_use_created(session_id, info)
            self._persist_state()
            return True

        if info.backendType == "opencode":
            await self._spawn_opencode(
                session_id,
                info,
                LaunchOptions(
                    model=info.model,
                    permissionMode=info.permissionMode,
                    cwd=info.cwd,
                ),
            )
        elif info.backendType == "codex":
            await self._spawn_codex(
                session_id,
                info,
                LaunchOptions(
                    model=info.model,
                    permissionMode=info.permissionMode,
                    cwd=info.cwd,
                ),
            )
        elif info.backendType == "hermes":
            if not info.agentConfig:
                info.agentConfig = {
                    "mcpServers": self._build_acp_mcp_servers(
                        session_id,
                        "hermes",
                        info.cwd,
                        model=info.model,
                    )
                }
            await self._spawn_hermes(
                session_id,
                info,
                LaunchOptions(
                    model=info.model,
                    permissionMode=info.permissionMode,
                    cwd=info.cwd,
                    agentConfig=info.agentConfig,
                ),
            )
        else:
            if not info.mcpConfig:
                info.mcpConfig = self._write_session_mcp_config(
                    session_id,
                    "claude",
                    info.cwd,
                    model=info.model,
                )
            await self._spawn_cli(
                session_id,
                info,
                _RelaunchOptions(
                    model=info.model,
                    permissionMode=info.permissionMode,
                    cwd=info.cwd,
                    resumeSessionId=info.cliSessionId,
                    mcpConfig=info.mcpConfig,
                ),
            )
        return True

    # ── Helpers ─────────────────────────────────────────────────────────────

    def get_starting_sessions(self) -> List[SdkSessionInfo]:
        """Get all sessions in 'starting' state (awaiting CLI WebSocket connection)."""
        return [s for s in self._sessions.values() if s.state == "starting"]

    def can_relaunch(self, session_id: str) -> bool:
        """Whether a relaunch should fire for this session right now.

        Returns False when the session is unknown, archived, or already
        has a spawn in flight (live entry in `_processes`). Notably,
        `state == "starting"` alone does NOT block: after a restart, the
        restore step can leave a session pinned at "starting" with a
        dead PID, and we still need to relaunch so queued messages get
        processed.
        """
        info = self._sessions.get(session_id)
        if not info or info.archived:
            return False
        return session_id not in self._processes

    # ── Spawn: Claude CLI ───────────────────────────────────────────────────

    async def _spawn_cli(
        self,
        session_id: str,
        info: SdkSessionInfo,
        options: _RelaunchOptions,
    ) -> None:
        binary = options.claudeBinary or "claude"
        if not binary.startswith("/"):
            resolved = shutil.which(binary)
            if resolved:
                binary = resolved

        if self._scheme_override:
            scheme = self._scheme_override
        else:
            cert_dir = Path(__file__).parent.parent / "certs"
            scheme = "wss" if (cert_dir / "cert.pem").exists() and (cert_dir / "key.pem").exists() else "ws"
        sdk_url = f"{scheme}://localhost:{self._port}/ws/cli/{session_id}"

        args: List[str] = [
            "--sdk-url", sdk_url,
            "--print",
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--verbose",
        ]

        if options.model:
            args.extend(["--model", options.model])
        if options.permissionMode:
            args.extend(["--permission-mode", options.permissionMode])
        if options.allowedTools:
            for tool in options.allowedTools:
                args.extend(["--allowedTools", tool])
        if options.mcpConfig:
            args.extend(["--mcp-config", options.mcpConfig, "--strict-mcp-config"])

        # Worktree guardrails are injected by launch() / relaunch() before
        # dispatching to the spawn function.

        # Always pass -p "" for headless mode. When relaunching, also pass --resume
        # to restore the CLI's conversation context.
        if options.resumeSessionId:
            args.extend(["--resume", options.resumeSessionId])
        args.extend(["-p", ""])

        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        if scheme == "wss":
            env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
        if options.env:
            env.update(options.env)
        env.update(self._mcp_env(
            session_id, "claude", options.model, info.cwd, env,
        ))

        preexec_fn = None
        if options.isBackgroundTask:
            node_opts = env.get("NODE_OPTIONS", "")
            env["NODE_OPTIONS"] = (node_opts + " --max-old-space-size=2048").strip()

            def preexec_fn() -> None:
                os.nice(10)

        # Ensure cwd exists (user may have specified a new project directory)
        if info.cwd:
            Path(info.cwd).mkdir(parents=True, exist_ok=True)

        logger.info(
            "Spawning session %s: %s %s",
            session_id, binary, " ".join(args),
        )

        proc = await asyncio.create_subprocess_exec(
            binary, *args,
            cwd=info.cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            preexec_fn=preexec_fn,
        )

        info.pid = proc.pid
        self._processes[session_id] = proc

        # Stream stdout/stderr for debugging
        self._pipe_output(session_id, proc)

        # Monitor process exit
        spawned_at = time.time() * 1000
        task = asyncio.create_task(
            self._monitor_exit_cli(session_id, proc, spawned_at, options.resumeSessionId),
        )
        self._monitor_tasks[session_id] = task

        self._persist_state()

    async def _monitor_exit_cli(
        self,
        session_id: str,
        proc: asyncio.subprocess.Process,
        spawned_at: float,
        resume_session_id: Optional[str],
    ) -> None:
        """Wait for a Claude CLI subprocess to exit and update state."""
        exit_code = await proc.wait()
        logger.info("Session %s exited (code=%s)", session_id, exit_code)

        session = self._sessions.get(session_id)
        if session:
            session.state = "exited"
            session.exitCode = exit_code

            # If the process exited almost immediately with --resume, the resume
            # likely failed.  Clear cliSessionId so the next relaunch starts fresh.
            uptime = time.time() * 1000 - spawned_at
            if uptime < 5000 and resume_session_id:
                logger.error(
                    "Session %s exited immediately after --resume (%.0fms). "
                    "Clearing cliSessionId for fresh start.",
                    session_id, uptime,
                )
                session.cliSessionId = None

        self._processes.pop(session_id, None)
        self._monitor_tasks.pop(session_id, None)
        self._persist_state()

    # ── Spawn: Codex ────────────────────────────────────────────────────────

    async def _spawn_codex(
        self,
        session_id: str,
        info: SdkSessionInfo,
        options: LaunchOptions,
    ) -> None:
        """Spawn a Codex app-server subprocess for a session.

        Unlike Claude Code (which connects back via WebSocket), Codex uses stdio.
        """
        binary = options.codexBinary or "codex"
        if not binary.startswith("/"):
            resolved = shutil.which(binary)
            if resolved:
                binary = resolved

        # Note: we used to pass `--enable use_legacy_landlock` here, but that
        # feature flag is `deprecated` in current codex (`codex features list`)
        # and routes through bubblewrap anyway. The actual sandbox-vs-no-sandbox
        # choice is made at thread/start in codex_adapter.py via
        # detect_codex_sandbox_mode().
        args: List[str] = ["app-server"]

        env = {**os.environ}
        if options.env:
            env.update(options.env)
        env.update(self._mcp_env(
            session_id, "codex", options.model, info.cwd, env,
        ))

        # Log the chosen sandbox mode once per spawn so it's obvious in logs
        # which mode every session uses.
        from vibr8_core.codex_adapter import detect_codex_sandbox_mode, codex_sandbox_reason
        sandbox_mode = detect_codex_sandbox_mode()

        logger.info(
            "Spawning Codex session %s: %s %s (sandbox=%s; %s)",
            session_id, binary, " ".join(args), sandbox_mode, codex_sandbox_reason(),
        )

        proc = await asyncio.create_subprocess_exec(
            binary, *args,
            cwd=info.cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        info.pid = proc.pid
        self._processes[session_id] = proc

        # Pipe stderr for debugging (stdout is used for JSON-RPC)
        if proc.stderr:
            task = asyncio.create_task(
                self._pipe_stream(session_id, proc.stderr, "stderr"),
            )
            self._pipe_tasks.append(task)

        # Create the CodexAdapter which handles JSON-RPC and message translation.
        # Import at runtime to avoid circular imports.
        from vibr8_core.codex_adapter import CodexAdapter

        from vibr8_core.codex_adapter import CodexAdapterOptions
        # Resume the prior thread if we have a cliSessionId. Codex persists
        # threads to ~/.codex/sessions/.../rollout-*.jsonl, and the adapter
        # issues `thread/resume` when thread_id is set. If the resume fails
        # (e.g. the rollout file was pruned or the model/cwd diverged), the
        # init_error handler clears cliSessionId so the next relaunch starts
        # fresh — matches the Claude (--resume) and Hermes (session_id_to_resume)
        # patterns.
        #
        # Defensive disk check first: if codex has no rollout file for this
        # thread, `thread/resume` would hard-fail with
        # "no rollout found for thread id <X>". The adapter handles that
        # gracefully (auto-falls back to thread/start with a soft notice),
        # but we can prevent the user-visible notice entirely by detecting
        # the missing rollout up front and starting fresh silently. Common
        # cause: the codex CLI was killed before its first rollout flush,
        # so the cliSessionId we persisted points at a thread codex itself
        # has no record of.
        resume_thread_id = info.cliSessionId
        if resume_thread_id and not _codex_rollout_exists(resume_thread_id):
            logger.info(
                "Codex thread %s has no rollout on disk — clearing "
                "cliSessionId and starting fresh", resume_thread_id,
            )
            resume_thread_id = None
            info.cliSessionId = None
            self._persist_state()
        adapter = CodexAdapter(proc, session_id, CodexAdapterOptions(
            model=options.model,
            cwd=info.cwd,
            approval_mode=options.permissionMode,
            thread_id=resume_thread_id,
        ))

        # Handle init errors -- mark session as exited so UI shows failure
        def _on_init_error(error: str) -> None:
            logger.error("Codex session %s init failed: %s", session_id, error)
            session = self._sessions.get(session_id)
            if session:
                session.state = "exited"
                session.exitCode = 1
                if resume_thread_id and session.cliSessionId == resume_thread_id:
                    logger.warning(
                        "Clearing cliSessionId for session %s after failed "
                        "Codex thread/resume — next relaunch will start fresh",
                        session_id,
                    )
                    session.cliSessionId = None
            self._persist_state()

        adapter.on_init_error(_on_init_error)

        # Notify the WsBridge to attach this adapter
        if self._on_adapter:
            self._on_adapter(session_id, adapter, "codex")

        # Mark as connected immediately (no WS handshake needed for stdio)
        info.state = "connected"

        # Monitor process exit
        task = asyncio.create_task(
            self._monitor_exit_codex(session_id, proc),
        )
        self._monitor_tasks[session_id] = task

        self._persist_state()

    async def _monitor_exit_codex(
        self,
        session_id: str,
        proc: asyncio.subprocess.Process,
    ) -> None:
        """Wait for a Codex subprocess to exit and update state."""
        exit_code = await proc.wait()
        logger.info("Codex session %s exited (code=%s)", session_id, exit_code)

        session = self._sessions.get(session_id)
        if session:
            session.state = "exited"
            session.exitCode = exit_code

        self._processes.pop(session_id, None)
        self._monitor_tasks.pop(session_id, None)
        self._persist_state()

    # ── Spawn: Hermes ────────────────────────────────────────────────────────

    async def _spawn_hermes(
        self,
        session_id: str,
        info: SdkSessionInfo,
        options: LaunchOptions,
    ) -> None:
        """Spawn a Hermes ACP subprocess for a session.

        Like Codex, Hermes ACP uses JSON-RPC over stdio.
        """
        binary = options.hermesBinary or "hermes"
        if not binary.startswith("/"):
            resolved = shutil.which(binary)
            if resolved:
                binary = resolved

        args: List[str] = ["acp"]

        env = {**os.environ}
        if options.env:
            env.update(options.env)
        env.update(self._mcp_env(
            session_id, "hermes", options.model, info.cwd, env,
        ))

        logger.info(
            "Spawning Hermes session %s: %s %s",
            session_id, binary, " ".join(args),
        )

        proc = await asyncio.create_subprocess_exec(
            binary, *args,
            cwd=info.cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        info.pid = proc.pid
        self._processes[session_id] = proc

        if proc.stderr:
            task = asyncio.create_task(
                self._pipe_stream(session_id, proc.stderr, "stderr"),
            )
            self._pipe_tasks.append(task)

        from vibr8_core.hermes_adapter import HermesAdapter, HermesAdapterOptions
        mcp_servers = None
        if options.agentConfig and "mcpServers" in options.agentConfig:
            mcp_servers = options.agentConfig["mcpServers"]
        adapter = HermesAdapter(proc, session_id, HermesAdapterOptions(
            model=options.model,
            cwd=info.cwd,
            approval_mode=options.permissionMode,
            mcp_servers=mcp_servers,
            session_id_to_resume=info.cliSessionId,
        ))

        def _on_init_error(error: str) -> None:
            logger.error("Hermes session %s init failed: %s", session_id, error)
            session = self._sessions.get(session_id)
            if session:
                session.state = "exited"
                session.exitCode = 1
                # If init failed while trying to resume, clear the saved id
                # so the next relaunch starts fresh.
                if info.cliSessionId:
                    logger.warning(
                        "Clearing cliSessionId for session %s after failed Hermes init",
                        session_id,
                    )
                    session.cliSessionId = None
            self._persist_state()

        adapter.on_init_error(_on_init_error)

        if self._on_adapter:
            self._on_adapter(session_id, adapter, "hermes")

        info.state = "connected"

        spawned_at = time.time() * 1000
        task = asyncio.create_task(
            self._monitor_exit_hermes(session_id, proc, spawned_at),
        )
        self._monitor_tasks[session_id] = task

        self._persist_state()

    async def _monitor_exit_hermes(
        self,
        session_id: str,
        proc: asyncio.subprocess.Process,
        spawned_at: float,
    ) -> None:
        """Wait for a Hermes subprocess to exit and update state."""
        exit_code = await proc.wait()
        logger.info("Hermes session %s exited (code=%s)", session_id, exit_code)

        session = self._sessions.get(session_id)
        if session:
            session.state = "exited"
            session.exitCode = exit_code

            # If Hermes died within ~5s of spawning and we had a session to
            # resume, treat that as a failed session/load and start fresh on
            # the next relaunch (same heuristic as _monitor_exit_cli).
            uptime = time.time() * 1000 - spawned_at
            if uptime < 5000 and session.cliSessionId:
                logger.error(
                    "Hermes session %s exited immediately after session/load (%.0fms). "
                    "Clearing cliSessionId for fresh start.",
                    session_id, uptime,
                )
                session.cliSessionId = None

        self._processes.pop(session_id, None)
        self._monitor_tasks.pop(session_id, None)
        self._persist_state()

    # ── Spawn: OpenCode ──────────────────────────────────────────────────────

    async def _spawn_opencode(
        self,
        session_id: str,
        info: SdkSessionInfo,
        options: LaunchOptions,
    ) -> None:
        """Spawn an OpenCode server subprocess for a session.

        Unlike Claude Code (WebSocket) or Codex (stdio), OpenCode runs as an
        HTTP server. We spawn ``opencode serve``, wait for it to become ready,
        then create an OpenCodeAdapter that communicates via REST + SSE.
        """
        import secrets

        binary = options.opencodeBinary or "opencode"
        if not binary.startswith("/"):
            resolved = shutil.which(binary)
            if resolved:
                binary = resolved

        port = self._allocate_port()
        password = secrets.token_urlsafe(24)

        args: List[str] = ["serve", "--port", str(port), "--hostname", "127.0.0.1"]

        env = {**os.environ}
        env["OPENCODE_SERVER_PASSWORD"] = password
        if options.env:
            env.update(options.env)
        env.update(self._mcp_env(
            session_id, "opencode", options.model, info.cwd, env,
        ))

        # Write opencode.jsonc config with model + MCP if needed
        cwd_path = Path(info.cwd) if info.cwd else Path.cwd()
        cwd_path.mkdir(parents=True, exist_ok=True)
        self._write_opencode_config(
            cwd_path,
            session_id=session_id,
            model=options.model,
            extra_env=options.env,
        )

        logger.info(
            "Spawning OpenCode session %s: %s %s (port %d)",
            session_id, binary, " ".join(args), port,
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                binary, *args,
                cwd=str(cwd_path),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError:
            logger.error(
                "OpenCode binary not found: %s. Install with: npm install -g @anthropic/opencode",
                binary,
            )
            info.state = "exited"
            info.exitCode = 1
            self._persist_state()
            return

        info.pid = proc.pid
        self._processes[session_id] = proc

        # Pipe stderr for debugging (stdout may have server output)
        self._pipe_output(session_id, proc)

        server_url = f"http://127.0.0.1:{port}"

        # Wait for the server to become ready
        ready = await self._wait_for_opencode_ready(server_url, password, proc)
        if not ready:
            logger.error("OpenCode server failed to start for session %s", session_id)
            info.state = "exited"
            info.exitCode = 1
            self._persist_state()
            return

        # Create the adapter
        from vibr8_core.opencode_adapter import OpenCodeAdapter, OpenCodeAdapterOptions

        adapter = OpenCodeAdapter(session_id, OpenCodeAdapterOptions(
            model=options.model,
            cwd=str(cwd_path),
            server_url=server_url,
            password=password,
            approval_mode=options.permissionMode,
        ))

        def _on_init_error(error: str) -> None:
            logger.error("OpenCode session %s init failed: %s", session_id, error)
            session = self._sessions.get(session_id)
            if session:
                session.state = "exited"
                session.exitCode = 1
            self._persist_state()

        adapter.on_init_error(_on_init_error)

        if self._on_adapter:
            self._on_adapter(session_id, adapter, "opencode")

        info.state = "connected"

        task = asyncio.create_task(
            self._monitor_exit_opencode(session_id, proc),
        )
        self._monitor_tasks[session_id] = task

        self._persist_state()

    async def _wait_for_opencode_ready(
        self,
        server_url: str,
        password: str,
        proc: asyncio.subprocess.Process,
        timeout: float = 30.0,
    ) -> bool:
        """Poll the OpenCode server until it responds to API requests."""
        import aiohttp

        auth = aiohttp.BasicAuth("opencode", password)
        deadline = time.time() + timeout

        while time.time() < deadline:
            if proc.returncode is not None:
                return False

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{server_url}/session",
                        auth=auth,
                        timeout=aiohttp.ClientTimeout(total=2),
                    ) as resp:
                        if resp.status == 200:
                            body = await resp.text()
                            if body.startswith("["):
                                return True
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                pass

            await asyncio.sleep(0.5)

        return False

    def _write_opencode_config(
        self,
        cwd: Path,
        session_id: str,
        model: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> None:
        """Write or update opencode.jsonc with per-session vibr8 MCP config."""
        config_path = cwd / "opencode.jsonc"
        config: Dict[str, Any] = {}
        if config_path.exists():
            try:
                raw = config_path.read_text()
                import json5
                parsed = json5.loads(raw) if raw.strip() else {}
                if isinstance(parsed, dict):
                    config = parsed
            except Exception:
                logger.warning(
                    "Could not parse %s; preserving session startup with fresh config",
                    config_path,
                )
        if model:
            config["model"] = model

        command, args = self._mcp_command()
        config.setdefault("mcp", {})["vibr8"] = {
            "type": "local",
            "command": [command, *args],
            "environment": self._mcp_env(
                session_id,
                "opencode",
                model,
                str(cwd),
                extra_env,
            ),
        }
        config_path.write_text(json.dumps(config, indent=2))

    @staticmethod
    def _allocate_port() -> int:
        """Find an available port for the OpenCode server."""
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    async def _monitor_exit_opencode(
        self,
        session_id: str,
        proc: asyncio.subprocess.Process,
    ) -> None:
        """Wait for an OpenCode subprocess to exit and update state."""
        exit_code = await proc.wait()
        logger.info("OpenCode session %s exited (code=%s)", session_id, exit_code)

        session = self._sessions.get(session_id)
        if session:
            session.state = "exited"
            session.exitCode = exit_code

        self._processes.pop(session_id, None)
        self._monitor_tasks.pop(session_id, None)
        self._persist_state()

    # ── Worktree Guardrails ─────────────────────────────────────────────────

    @staticmethod
    def _inject_worktree_guardrails(
        worktree_path: str,
        branch: str,
        repo_root: str,
        parent_branch: Optional[str] = None,
        backend_type: str = "claude",
    ) -> None:
        """Inject branch-guardrails into the worktree's agent context file.

        Claude reads ``.claude/CLAUDE.md``; codex/opencode/hermes read
        ``AGENTS.md`` at the project root. Only injects into actual worktree
        directories, never the main repo.
        """
        wt = Path(worktree_path)

        # Safety: never inject guardrails into the main repository itself
        if worktree_path == repo_root:
            logger.warning(
                "Skipping guardrails injection: worktree path is the main repo (%s)",
                repo_root,
            )
            return

        # Safety: only inject if the worktree directory actually exists
        if not wt.exists():
            logger.warning(
                "Skipping guardrails injection: worktree path does not exist (%s)",
                worktree_path,
            )
            return

        if parent_branch:
            branch_label = f"`{branch}` (created from `{parent_branch}`)"
        else:
            branch_label = f"`{branch}`"

        MARKER_START = "<!-- WORKTREE_GUARDRAILS_START -->"
        MARKER_END = "<!-- WORKTREE_GUARDRAILS_END -->"
        guardrails = (
            f"{MARKER_START}\n"
            f"# Worktree Session \u2014 Branch Guardrails\n"
            f"\n"
            f"You are working on branch: {branch_label}\n"
            f"This is a git worktree. The main repository is at: `{repo_root}`\n"
            f"\n"
            f"**Rules:**\n"
            f"1. DO NOT run `git checkout`, `git switch`, or any command that changes the current branch\n"
            f"2. All your work MUST stay on the `{branch}` branch\n"
            f"3. When committing, commit to `{branch}` only\n"
            f"4. If you need to reference code from another branch, use `git show other-branch:path/to/file`\n"
            f"{MARKER_END}"
        )

        if backend_type == "claude":
            target_paths = [wt / ".claude" / "CLAUDE.md"]
        elif backend_type in ("codex", "opencode", "hermes"):
            target_paths = [wt / "AGENTS.md"]
        else:
            logger.info(
                "Skipping guardrails injection: no known context file for backend %s",
                backend_type,
            )
            return

        for target in target_paths:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)

                if target.exists():
                    existing = target.read_text(encoding="utf-8")
                    # Replace existing guardrails section or append
                    if MARKER_START in existing:
                        before = existing[: existing.index(MARKER_START)]
                        end_idx = existing.find(MARKER_END)
                        after = (
                            existing[end_idx + len(MARKER_END) :]
                            if end_idx >= 0
                            else ""
                        )
                        target.write_text(
                            before + guardrails + after, encoding="utf-8",
                        )
                    else:
                        target.write_text(
                            existing + "\n\n" + guardrails, encoding="utf-8",
                        )
                else:
                    target.write_text(guardrails, encoding="utf-8")

                logger.info(
                    "Injected worktree guardrails for branch %s into %s",
                    branch, target,
                )
            except Exception:
                logger.warning(
                    "Failed to inject worktree guardrails into %s", target,
                    exc_info=True,
                )

    # ── Session State Mutations ─────────────────────────────────────────────

    def mark_connected(self, session_id: str) -> None:
        """Mark a session as connected (called when CLI establishes WS connection)."""
        session = self._sessions.get(session_id)
        if session and session.state in ("starting", "connected"):
            session.state = "connected"
            logger.info("Session %s connected via WebSocket", session_id)
            self._persist_state()

    def set_cli_session_id(self, session_id: str, cli_session_id: str) -> None:
        """Store the CLI's internal session ID (from system.init message).

        This is needed for --resume on relaunch.
        """
        session = self._sessions.get(session_id)
        if session:
            session.cliSessionId = cli_session_id
            self._persist_state()

    async def kill(self, session_id: str) -> bool:
        """Kill a session's CLI process and all its children (e.g. MCP)."""
        info = self._sessions.get(session_id)
        if info and info.backendType == "computer-use":
            # Computer-use has no subprocess — just mark exited
            info.state = "exited"
            info.exitCode = 0
            self._persist_state()
            return True

        proc = self._processes.get(session_id)
        if not proc:
            return False

        # Kill the entire process group so child processes (MCP) die too.
        # CLI processes are spawned with start_new_session=True.
        import signal
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()

        # Wait up to 5s for graceful exit, then force kill
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.info("Force-killing session %s", session_id)
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()

        session = self._sessions.get(session_id)
        if session:
            session.state = "exited"
            session.exitCode = -1

        self._processes.pop(session_id, None)
        self._monitor_tasks.pop(session_id, None)
        self._persist_state()
        return True

    def list_sessions(self) -> List[SdkSessionInfo]:
        """List all sessions (active + recently exited)."""
        return list(self._sessions.values())

    def get_session(self, session_id: str) -> Optional[SdkSessionInfo]:
        """Get a specific session."""
        return self._sessions.get(session_id)

    def get_all_session_ids(self) -> List[str]:
        """Get all session IDs."""
        return list(self._sessions.keys())

    def is_alive(self, session_id: str) -> bool:
        """Check if a session exists and is alive (not exited)."""
        session = self._sessions.get(session_id)
        return session is not None and session.state != "exited"

    def set_archived(self, session_id: str, archived: bool) -> None:
        """Set the archived flag on a session."""
        info = self._sessions.get(session_id)
        if info:
            info.archived = archived
            self._persist_state()

    def remove_session(self, session_id: str) -> None:
        """Remove a session from the internal map (after kill or cleanup)."""
        self._sessions.pop(session_id, None)
        self._processes.pop(session_id, None)
        self._monitor_tasks.pop(session_id, None)
        self._persist_state()

    def prune_exited(self) -> int:
        """Remove exited sessions from the list."""
        pruned = 0
        to_remove = [
            sid for sid, s in self._sessions.items() if s.state == "exited"
        ]
        for sid in to_remove:
            del self._sessions[sid]
            pruned += 1
        return pruned

    async def kill_all(self) -> None:
        """Kill all sessions."""
        ids = list(self._processes.keys())
        await asyncio.gather(*(self.kill(sid) for sid in ids))

    # ── Stdout/Stderr piping ────────────────────────────────────────────────

    async def _pipe_stream(
        self,
        session_id: str,
        stream: asyncio.StreamReader,
        label: str,
    ) -> None:
        """Read lines from a subprocess stream and log them."""
        log_fn = logger.info if label == "stdout" else logger.error
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    log_fn("[session:%s:%s] %s", session_id, label, text)
        except Exception:
            # stream closed
            pass

    def _pipe_output(
        self,
        session_id: str,
        proc: asyncio.subprocess.Process,
    ) -> None:
        """Start background tasks to pipe stdout and stderr."""
        if proc.stdout:
            task = asyncio.create_task(
                self._pipe_stream(session_id, proc.stdout, "stdout"),
            )
            self._pipe_tasks.append(task)
        if proc.stderr:
            task = asyncio.create_task(
                self._pipe_stream(session_id, proc.stderr, "stderr"),
            )
            self._pipe_tasks.append(task)
