"""Manage CLI backend processes (Claude Code via --sdk-url WebSocket,
or Codex via app-server stdio).

Originally ported from The Vibe Companion (cli-launcher.ts).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import shutil
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Literal, Optional

if TYPE_CHECKING:
    from server.codex_adapter import CodexAdapter
    from server.session_store import SessionStore

from server.session_types import BackendType

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


@dataclass
class _RelaunchOptions:
    """Internal options for spawnCLI that includes resumeSessionId."""

    model: Optional[str] = None
    permissionMode: Optional[str] = None
    cwd: Optional[str] = None
    claudeBinary: Optional[str] = None
    codexBinary: Optional[str] = None
    allowedTools: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    backendType: Optional[BackendType] = None
    worktreeInfo: Optional[WorktreeInfo] = None
    resumeSessionId: Optional[str] = None
    mcpConfig: Optional[str] = None


# ─── CliLauncher ─────────────────────────────────────────────────────────────


class CliLauncher:
    """Manages CLI backend processes (Claude Code via --sdk-url WebSocket,
    or Codex via app-server stdio)."""

    def __init__(self, port: int) -> None:
        self._sessions: Dict[str, SdkSessionInfo] = {}
        self._processes: Dict[str, asyncio.subprocess.Process] = {}
        self._port = port
        self._store: Optional[SessionStore] = None
        self._on_codex_adapter: Optional[
            Callable[[str, CodexAdapter], None]
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
        cb: Callable[[str, CodexAdapter], None],
    ) -> None:
        """Register a callback for when a CodexAdapter is created
        (WsBridge needs to attach it)."""
        self._on_codex_adapter = cb

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

        # Store worktree metadata if provided
        if options.worktreeInfo:
            info.isWorktree = options.worktreeInfo.isWorktree
            info.repoRoot = options.worktreeInfo.repoRoot
            info.branch = options.worktreeInfo.branch
            info.actualBranch = options.worktreeInfo.actualBranch

        if options.nodeId:
            info.nodeId = options.nodeId

        self._sessions[session_id] = info

        if backend_type == "computer-use":
            # Computer-use runs in-process — no subprocess to spawn
            info.state = "connected"
            if self._on_computer_use_created:
                self._on_computer_use_created(session_id, info)
            self._persist_state()
            return info

        if backend_type == "codex":
            asyncio.ensure_future(self._spawn_codex(session_id, info, options))
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

        if info.backendType == "computer-use":
            # Computer-use is in-process — just mark connected and re-create
            info.state = "connected"
            if self._on_computer_use_created:
                self._on_computer_use_created(session_id, info)
            self._persist_state()
            return True

        if info.backendType == "codex":
            await self._spawn_codex(
                session_id,
                info,
                LaunchOptions(
                    model=info.model,
                    permissionMode=info.permissionMode,
                    cwd=info.cwd,
                ),
            )
        else:
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

        sdk_url = f"ws://localhost:{self._port}/ws/cli/{session_id}"

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

        # Inject CLAUDE.md guardrails for worktree sessions
        if info.isWorktree and info.branch:
            parent_branch: Optional[str] = None
            if info.actualBranch and info.actualBranch != info.branch:
                parent_branch = info.branch
            self._inject_worktree_guardrails(
                info.cwd,
                info.actualBranch or info.branch,
                info.repoRoot or "",
                parent_branch,
            )

        # Always pass -p "" for headless mode. When relaunching, also pass --resume
        # to restore the CLI's conversation context.
        if options.resumeSessionId:
            args.extend(["--resume", options.resumeSessionId])
        args.extend(["-p", ""])

        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        if options.env:
            env.update(options.env)

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

        args: List[str] = ["app-server"]

        env = {**os.environ}
        if options.env:
            env.update(options.env)

        logger.info(
            "Spawning Codex session %s: %s %s",
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

        # Pipe stderr for debugging (stdout is used for JSON-RPC)
        if proc.stderr:
            task = asyncio.create_task(
                self._pipe_stream(session_id, proc.stderr, "stderr"),
            )
            self._pipe_tasks.append(task)

        # Create the CodexAdapter which handles JSON-RPC and message translation.
        # Import at runtime to avoid circular imports.
        from server.codex_adapter import CodexAdapter

        adapter = CodexAdapter(proc, session_id, {
            "model": options.model,
            "cwd": info.cwd,
            "approvalMode": options.permissionMode,
            "threadId": info.cliSessionId,
        })

        # Handle init errors -- mark session as exited so UI shows failure
        def _on_init_error(error: str) -> None:
            logger.error("Codex session %s init failed: %s", session_id, error)
            session = self._sessions.get(session_id)
            if session:
                session.state = "exited"
                session.exitCode = 1
            self._persist_state()

        adapter.on_init_error(_on_init_error)

        # Notify the WsBridge to attach this adapter
        if self._on_codex_adapter:
            self._on_codex_adapter(session_id, adapter)

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

    # ── Worktree Guardrails ─────────────────────────────────────────────────

    @staticmethod
    def _inject_worktree_guardrails(
        worktree_path: str,
        branch: str,
        repo_root: str,
        parent_branch: Optional[str] = None,
    ) -> None:
        """Inject a CLAUDE.md file into the worktree with branch guardrails.

        Only injects into actual worktree directories, never the main repo.
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

        claude_dir = wt / ".claude"
        claude_md_path = claude_dir / "CLAUDE.md"

        try:
            claude_dir.mkdir(parents=True, exist_ok=True)

            if claude_md_path.exists():
                existing = claude_md_path.read_text(encoding="utf-8")
                # Replace existing guardrails section or append
                if MARKER_START in existing:
                    before = existing[: existing.index(MARKER_START)]
                    end_idx = existing.find(MARKER_END)
                    after = (
                        existing[end_idx + len(MARKER_END) :]
                        if end_idx >= 0
                        else ""
                    )
                    claude_md_path.write_text(
                        before + guardrails + after, encoding="utf-8",
                    )
                else:
                    claude_md_path.write_text(
                        existing + "\n\n" + guardrails, encoding="utf-8",
                    )
            else:
                claude_md_path.write_text(guardrails, encoding="utf-8")

            logger.info("Injected worktree guardrails for branch %s", branch)
        except Exception:
            logger.warning(
                "Failed to inject worktree guardrails", exc_info=True,
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
