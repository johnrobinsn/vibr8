"""Ring0 — voice-controlled meta-agent manager.

Ring0 is a Claude CLI session launched with MCP tools that can list sessions,
send messages to other sessions, switch the UI, and check session output.
When enabled, voice transcripts route to Ring0 instead of the active session.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from server.cli_launcher import CliLauncher
    from server.ws_bridge import WsBridge

from server import session_names

logger = logging.getLogger(__name__)

RING0_CONFIG_PATH = Path.home() / ".vibr8" / "ring0.json"

RING0_SYSTEM_PROMPT = """\
# Ring0 — Voice-Controlled Meta-Agent

You are Ring0, a meta-agent that orchestrates other Claude Code sessions in vibr8.
You receive voice transcripts from the user and decide how to route them.

## Your MCP Tools

- **list_sessions** — See all active sessions and their status
- **send_message** — Send a message to a specific session
- **switch_ui** — Switch the browser view to a specific session
- **get_session_output** — Read recent messages from a session
- **get_active_clients** — List all connected browser clients
- **query_client** — Query a specific client's state (e.g., what session they're viewing)

## Client Identity

Each voice message is prefixed with `[from client <clientId>]`. This identifies which
browser tab sent the message. You can use this clientId with the `query_client` tool
to find out what session that client is currently viewing (their default routing target).

## Behavior

1. When you receive a voice message, parse the `[from client <clientId>]` prefix to identify
   the sender, then call **query_client** with that clientId and method "get_state" to learn
   which session they are currently viewing. This is your default routing target.

2. Determine if the message is:
   - A command for you (e.g., "list sessions", "switch to session X", "what's session Y doing?")
   - A message intended for the session the user is viewing (route it with send_message)
   - A message for a different session (find it, route it, switch the UI)
   - Ambiguous — ask for clarification

3. Keep responses **very brief** — you are voice-first. Prefer short sentences.

4. When routing a message, also switch the UI to that session so the user sees the response.

5. If the user says "send to [session name/description]", find the matching session and route the message.

6. You can proactively check session status and report back.

## Session Events

You receive automatic event notifications as messages prefixed with `[event ...]`.
These are system-generated, not from a user. Do not treat them as voice commands.

- `[event session_state_change] session=<name> (id=<short>) transition=idle→running`
  A session started working on a task.
- `[event session_state_change] session=<name> (id=<short>) transition=running→idle`
  A session finished its task. Use get_session_output to see what it did.

When a session finishes (running→idle), proactively summarize the result if audio is active.
Keep summaries very brief and suitable for voice.
"""


class Ring0Manager:
    """Manages the Ring0 meta-agent session."""

    def __init__(self, port: int) -> None:
        self._port = port
        self._enabled: bool = False
        self._session_id: Optional[str] = None
        self._temp_dir: Optional[str] = None
        self._load_state()

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    # ── Enable / Disable ──────────────────────────────────────────────────

    def enable(self) -> None:
        self._enabled = True
        self._save_state()
        logger.info("[ring0] Enabled")

    def disable(self) -> None:
        self._enabled = False
        self._save_state()
        logger.info("[ring0] Disabled")

    def toggle(self, enabled: bool) -> None:
        if enabled:
            self.enable()
        else:
            self.disable()

    # ── Session management ────────────────────────────────────────────────

    async def ensure_session(self, launcher: CliLauncher, ws_bridge: WsBridge) -> str:
        """Lazily create the Ring0 session if it doesn't exist yet.

        Returns the session_id.
        """
        if self._session_id:
            info = launcher.get_session(self._session_id)
            if info and info.state not in ("exited",):
                return self._session_id

        # Create temp dir with CLAUDE.md
        self._temp_dir = tempfile.mkdtemp(prefix="ring0_")
        claude_md = Path(self._temp_dir) / "CLAUDE.md"
        claude_md.write_text(RING0_SYSTEM_PROMPT)

        # Build MCP config — must use absolute paths (no cwd support)
        server_dir = Path(__file__).parent.parent.resolve()
        mcp_script = str(server_dir / "server" / "ring0_mcp.py")
        uv_bin = shutil.which("uv") or "uv"
        mcp_config = {
            "mcpServers": {
                "vibr8": {
                    "type": "stdio",
                    "command": uv_bin,
                    "args": ["run", "--project", str(server_dir), "python", mcp_script],
                    "env": {"VIBR8_PORT": str(self._port)},
                }
            }
        }
        mcp_config_path = Path(self._temp_dir) / "mcp.json"
        mcp_config_path.write_text(json.dumps(mcp_config))

        # Launch CLI session
        from server.cli_launcher import LaunchOptions
        options = LaunchOptions(
            permissionMode="bypassPermissions",
            cwd=self._temp_dir,
            mcpConfig=str(mcp_config_path),
        )

        info = launcher.launch(options)
        session_id = info.sessionId
        self._session_id = session_id
        session_names.set_name(session_id, "Ring0", unique=False)
        self._save_state()

        logger.info("[ring0] Created session %s in %s", session_id, self._temp_dir)
        return session_id

    # ── Persistence ───────────────────────────────────────────────────────

    def _load_state(self) -> None:
        if RING0_CONFIG_PATH.exists():
            try:
                data = json.loads(RING0_CONFIG_PATH.read_text())
                self._enabled = data.get("enabled", False)
                self._session_id = data.get("sessionId")
                if self._session_id:
                    session_names.set_name(self._session_id, "Ring0", unique=False)
            except Exception:
                logger.warning("[ring0] Failed to load state from %s", RING0_CONFIG_PATH)

    def _save_state(self) -> None:
        RING0_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {"enabled": self._enabled, "sessionId": self._session_id}
        RING0_CONFIG_PATH.write_text(json.dumps(data))
