"""Ring0 — voice-controlled meta-agent manager.

Ring0 is a Claude CLI session launched with MCP tools that can list sessions,
send messages to other sessions, switch the UI, and check session output.
When enabled, voice transcripts route to Ring0 instead of the active session.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from server.cli_launcher import CliLauncher
    from server.ws_bridge import WsBridge

from server import session_names

logger = logging.getLogger(__name__)

RING0_CONFIG_PATH = Path.home() / ".vibr8" / "ring0.json"
RING0_WORK_DIR = Path.home() / ".vibr8" / "ring0"
RING0_SESSION_ID = "ring0"

RING0_SYSTEM_PROMPT = """\
# Ring0 — Voice-Controlled Meta-Agent

You are Ring0, a meta-agent that orchestrates other Claude Code sessions in vibr8.
You receive voice transcripts from the user and decide how to route them.

## Your MCP Tools

- **create_session** — Create a new coding session with its own working directory
- **list_sessions** — See all active sessions and their status
- **send_message** — Send a message to a specific session
- **interrupt_session** — Stop/cancel a running session (Ctrl+C equivalent)
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

- `idle->running` — A session started working on a task.
- `running->waiting_for_permission` — A session is blocked on a tool permission prompt.
  Consider using respond_to_permission to approve if appropriate.
- `waiting_for_permission->running` — Permission approved, session resumed.
- `waiting_for_permission->idle` — Permission denied, session stopped.
- `running->idle` — Session finished. Use get_session_output to see what it did.

When a session finishes (running->idle), proactively summarize the result if audio is active.
When a session is waiting_for_permission, consider auto-approving safe tools.
Keep summaries very brief and suitable for voice.

## Second Screen Displays

Users can pair "second screen" devices — monitors, laptops, TVs, tablets — that act as
passive displays you can push content to. This is useful when the user is interacting
via voice (phone in pocket) but has a larger screen nearby for viewing.

### Tools
- **list_second_screens** — See all paired second screens, online/offline and enabled/disabled status
- **query_second_screen** — Query device info (screen dimensions, pixel ratio, user agent, etc.)
- **toggle_second_screen** — Enable or disable a second screen. Disabled screens are skipped by show_on_second_screen
- **show_on_second_screen** — Push content to a second screen (skips disabled screens). Content types:
  - `markdown` — Rich markdown (headings, lists, code blocks, tables)
  - `image` — Image via URL or base64 (`image_data` + `image_mime` params)
  - `file` — File viewer with filename header (`filename` param) and scrollable text
  - `pdf` — PDF viewer via URL or base64 (`pdf_data` param)
  - `html` — Render arbitrary HTML on the second screen
  - `session` — Mirror a session's live chat (set `content` to the session ID)
  - `home` — Return second screen to its default view (Ring0 chat)

### Session Mirroring
You can mirror any session's live chat on the second screen. The user may say:
- "Show session X on the second screen" -> `content_type="session"`, `content=sessionId`
- "Switch second screen to session X" -> same as above
- "Second screen go home" / "Go back" -> `content_type="home"`
When mirroring, the second screen shows that session's full chat with live streaming.

### Events You'll Receive
- `[event second_screen_paired]` — A new second screen was just paired
- `[event second_screen_connected]` — A paired second screen came online
- `[event second_screen_disconnected]` — A paired second screen went offline
- `[event second_screen_unpaired]` — A second screen was unpaired

### When to Use Second Screens
- When a second screen is available, prefer sending visual content there (images, code,
  long output, diagrams) while keeping voice responses brief on the primary device
- If content would be hard to view on a small screen, offer: "Want me to show this on
  the second screen?"
- By default, second screens show Ring0 chat history — only push specific content when
  it adds value
- Use session mirroring when the user wants to monitor another agent's work on a display
"""


class Ring0Manager:
    """Manages the Ring0 meta-agent session."""

    def __init__(self, port: int) -> None:
        self._port = port
        self._enabled: bool = False
        self._events_muted: bool = False
        self._session_id: str = RING0_SESSION_ID
        self._cli_session_id: Optional[str] = None
        self._model: Optional[str] = None
        self._load_state()

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def events_muted(self) -> bool:
        return self._events_muted

    @property
    def session_id(self) -> str:
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

    def set_events_muted(self, muted: bool) -> None:
        self._events_muted = muted
        self._save_state()
        logger.info("[ring0] Events %s", "muted" if muted else "unmuted")

    # ── Session management ────────────────────────────────────────────────

    def on_cli_session_id(self, cli_session_id: str) -> None:
        """Called when the CLI reports its internal session ID (for --resume)."""
        self._cli_session_id = cli_session_id
        self._save_state()
        logger.info("[ring0] Saved CLI session ID %s", cli_session_id)

    async def ensure_session(self, launcher: CliLauncher, ws_bridge: WsBridge) -> str:
        """Ensure the Ring0 session is running. Always uses the fixed session ID.

        On restart, reuses the persisted session and resumes the CLI conversation.
        Returns the session_id.
        """
        session_id = self._session_id
        work_dir = RING0_WORK_DIR
        work_dir.mkdir(parents=True, exist_ok=True)
        mcp_config_path = self._write_config_files(work_dir)

        session_names.set_name(session_id, "Ring0", unique=False)

        info = launcher.get_session(session_id)

        if info and info.state not in ("exited",):
            # Session is alive — nothing to do
            logger.info("[ring0] Session %s already running", session_id)
            return session_id

        if info:
            # Session exists but is exited — update cwd and relaunch
            info.cwd = str(work_dir)
            info.mcpConfig = str(mcp_config_path)
            await launcher.relaunch(session_id)
            logger.info("[ring0] Relaunched session %s", session_id)
            return session_id

        # No session in launcher — fresh launch with fixed ID (+ resume if we have a CLI session)
        from server.cli_launcher import LaunchOptions
        options = LaunchOptions(
            sessionId=session_id,
            model=self._model,
            permissionMode="bypassPermissions",
            cwd=str(work_dir),
            mcpConfig=str(mcp_config_path),
            resumeSessionId=self._cli_session_id,
        )

        launcher.launch(options)
        self._save_state()

        logger.info("[ring0] Created session %s in %s", session_id, work_dir)
        return session_id

    def _write_config_files(self, work_dir: Path) -> Path:
        """Write CLAUDE.md and MCP config to the Ring0 working directory."""
        claude_md = work_dir / "CLAUDE.md"
        if not claude_md.exists():
            claude_md.write_text(RING0_SYSTEM_PROMPT)

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
        mcp_config_path = work_dir / "mcp.json"
        mcp_config_path.write_text(json.dumps(mcp_config))
        return mcp_config_path

    # ── Persistence ───────────────────────────────────────────────────────

    def _load_state(self) -> None:
        if RING0_CONFIG_PATH.exists():
            try:
                data = json.loads(RING0_CONFIG_PATH.read_text())
                self._enabled = data.get("enabled", False)
                self._events_muted = data.get("eventsMuted", False)
                self._cli_session_id = data.get("cliSessionId")
                self._model = data.get("model")
            except Exception:
                logger.warning("[ring0] Failed to load state from %s", RING0_CONFIG_PATH)

    def _save_state(self) -> None:
        RING0_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "enabled": self._enabled,
            "eventsMuted": self._events_muted,
            "sessionId": self._session_id,
            "cliSessionId": self._cli_session_id,
        }
        if self._model:
            data["model"] = self._model
        tmp = RING0_CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(RING0_CONFIG_PATH)
