"""Terminal session management — PTY subprocess I/O.

Spawns interactive shell sessions (bash/zsh) via pseudo-terminals,
with non-blocking asyncio reads and WebSocket-friendly I/O.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import signal
import struct
import termios
from typing import Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class TerminalSession:
    """Manages a single PTY + child process."""

    def __init__(
        self,
        session_id: str,
        shell: str = "/bin/bash",
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        self.session_id = session_id
        self.master_fd: int = -1
        self.pid: int = -1
        self._on_data: Optional[Callable[[bytes], Awaitable[None]]] = None
        self._on_exit: Optional[Callable[[int], Awaitable[None]]] = None
        self._shell = shell
        self._cwd = cwd or os.getcwd()
        self._env = env
        self._reading = False

    def spawn(self) -> None:
        """Fork a child process with a new PTY."""
        master_fd, slave_fd = pty.openpty()
        pid = os.fork()
        if pid == 0:
            # Child: session leader, attach to slave PTY
            os.setsid()
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            os.close(master_fd)
            os.close(slave_fd)
            os.chdir(self._cwd)
            env = {**os.environ, **(self._env or {}), "TERM": "xterm-256color"}
            # Remove TMUX so the child shell starts fresh (not nested)
            env.pop("TMUX", None)
            env.pop("TMUX_PANE", None)
            os.execve(self._shell, [self._shell, "-l"], env)
        else:
            os.close(slave_fd)
            self.master_fd = master_fd
            self.pid = pid
            # Non-blocking reads
            flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            logger.info(
                "[terminal] Spawned shell pid=%d for session %s (cwd=%s)",
                pid,
                self.session_id,
                self._cwd,
            )

    def start_reading(self, loop: asyncio.AbstractEventLoop) -> None:
        """Register a reader on the event loop for PTY output."""
        if self._reading or self.master_fd < 0:
            return
        loop.add_reader(self.master_fd, self._on_readable)
        self._reading = True

    def stop_reading(self) -> None:
        """Remove the event loop reader."""
        if not self._reading or self.master_fd < 0:
            return
        try:
            loop = asyncio.get_event_loop()
            loop.remove_reader(self.master_fd)
        except Exception:
            pass
        self._reading = False

    def _on_readable(self) -> None:
        """Called when PTY has data available."""
        try:
            data = os.read(self.master_fd, 65536)
            if data and self._on_data:
                asyncio.ensure_future(self._on_data(data))
            elif not data:
                # EOF — child exited
                self._handle_exit()
        except OSError:
            self._handle_exit()

    def _handle_exit(self) -> None:
        """Handle child process exit."""
        self.stop_reading()
        exit_code = -1
        if self.pid > 0:
            try:
                _, status = os.waitpid(self.pid, os.WNOHANG)
                if os.WIFEXITED(status):
                    exit_code = os.WEXITSTATUS(status)
            except ChildProcessError:
                pass
        logger.info(
            "[terminal] Shell exited for session %s (code=%d)",
            self.session_id,
            exit_code,
        )
        if self._on_exit:
            asyncio.ensure_future(self._on_exit(exit_code))

    def write(self, data: bytes) -> None:
        """Write data to the PTY (user input from browser)."""
        if self.master_fd >= 0:
            os.write(self.master_fd, data)

    def resize(self, cols: int, rows: int) -> None:
        """Resize the PTY window."""
        if self.master_fd >= 0:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)

    def close(self) -> None:
        """Kill the child process and close the PTY."""
        self.stop_reading()
        if self.master_fd >= 0:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = -1
        if self.pid > 0:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                pass
            self.pid = -1


class TerminalManager:
    """Manages all terminal sessions."""

    def __init__(self) -> None:
        self._sessions: Dict[str, TerminalSession] = {}

    def create(
        self,
        session_id: str,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> TerminalSession:
        if session_id in self._sessions:
            self._sessions[session_id].close()
        term = TerminalSession(session_id, cwd=cwd, env=env)
        term.spawn()
        self._sessions[session_id] = term
        return term

    def get(self, session_id: str) -> Optional[TerminalSession]:
        return self._sessions.get(session_id)

    def close(self, session_id: str) -> None:
        term = self._sessions.pop(session_id, None)
        if term:
            term.close()

    def get_all_ids(self) -> list[str]:
        return list(self._sessions.keys())

    async def close_all(self) -> None:
        for term in list(self._sessions.values()):
            term.close()
        self._sessions.clear()
