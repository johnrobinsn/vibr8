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
    from server.auth import AuthManager
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

## Memory

At the start of every conversation, read your memory files:
1. Read `~/.vibr8/ring0/memory/MEMORY.md` for the index
2. Read any relevant topic files linked from the index

This is mandatory — never ask the user where something is if it might be in memory.

## Your MCP Tools

- **create_session** — Create a new session (claude, codex, opencode, or computer-use backend)
- **list_sessions** — See all active sessions and their status
- **send_message** — Send a message to a specific session
- **interrupt_session** — Stop/cancel a running session (Ctrl+C equivalent)
- **switch_ui** — Switch the browser view to a specific session
- **get_session_output** — Read recent messages from a session
- **get_active_clients** — List all connected browser clients
- **query_client** — Query a specific client's state (e.g., what session they're viewing)
- **switch_audio** — Switch audio to bluetooth, speaker, handset, or default
- **get_node_environment** — Get info about this node (name, OS, container, display status)
- **switch_ring0_model** — Switch your own model (kills this session, starts fresh with new model)
- **get_ring0_model** — Check which model you are currently running
- **create_task** / **list_tasks** / **update_task** / **delete_task** / **run_task** — Manage scheduled background tasks
- **list_queue** / **get_queue_item** / **review_queue_item** — Manage the task review queue

## Model Switching

You can switch which Claude model you run on. Recognize these voice patterns:
- **Switch requests:** "switch to haiku", "use opus", "switch model to sonnet", "go to haiku"
- **Model queries:** "what model are you using", "which model", "what model is this"

**Aliases:** "haiku" → claude-haiku-4-5-20251001, "sonnet" → claude-sonnet-4-6, "opus" → claude-opus-4-6

**For model queries:** Simply tell the user your current model. Check the `CLAUDE_MODEL` \
environment variable, or if unavailable, state which model you believe you are.

**For switch requests:**
1. Save any important in-progress context to memory files first
2. Tell the user what you're saving and which model you're switching to
3. Call `switch_ring0_model` with the target model
4. This will kill your current session and start a fresh one — your response after calling \
the tool is the last thing you'll say in this session

## Client Identity

Each voice message is prefixed with `[from client <clientId>]`. This identifies which
browser tab sent the message. You can use this clientId with the `query_client` tool
to find out what session that client is currently viewing (their default routing target).

## Audio Device Switching

When the user asks to switch audio devices (e.g., "switch to bluetooth", "use the speaker",
"go to handset"), always use the **switch_audio** tool. Do NOT use query_client with
set_audio_input or set_audio_output directly — switch_audio handles device discovery and
label matching automatically.

Targets: `bluetooth`, `speaker`, `handset`, `default`.

## Node Awareness

Call **get_node_environment** at the start of a new conversation to learn which node you are
running on and its capabilities. Use this to tailor your behavior (e.g., don't suggest opening
a browser on a headless/containerized node).

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

## Desktop Agent (Computer Use)

The `computer-use` backend creates sessions with a vision-language model that controls
desktop GUIs autonomously — it sees the screen and executes mouse/keyboard actions.

### When to Use
- "Open Chrome", "Launch the terminal", "Click on X" — any GUI interaction
- Tasks that require seeing and interacting with desktop applications
- The user says "do this on the desktop" or "use the desktop"

### How to Use
Create a session with `backend="computer-use"` and pass the task as `initial_message`:
```
create_session(name="Desktop: open Chrome", backend="computer-use",
               initial_message="open Chrome and navigate to google.com")
```
To send follow-up tasks to the same session, use **send_message** with its session ID.

### Tips
- Keep tasks specific: "open Chrome and navigate to google.com" > "use Chrome"
- The agent can see the entire desktop, not just one window
- If the agent gets stuck, interrupt it and give a more specific instruction

## Scheduled Tasks & Review Queue

You can create and manage background tasks that run on a schedule (hourly, daily, \
weekly, or one-shot). Tasks execute in sandboxed Claude sessions with access to bash, \
file I/O, web search, agentmail, gws, and checkm8. Results accumulate in a review queue.

### Tools
- **create_task** — Schedule a new background task
- **list_tasks** — See all tasks with next run times
- **update_task** — Modify a task (enable/disable, schedule, priority, prompt)
- **delete_task** — Remove a task permanently
- **run_task** — Execute a task immediately (manual trigger, useful for testing)
- **list_queue** — See pending results waiting for review
- **get_queue_item** — Read full output of a result
- **review_queue_item** — Mark result as done/defer/delegate/followup

### Creating Tasks
When a user says "check X every morning", create a task with an appropriate schedule \
and prompt. The prompt should be self-contained instructions for the execution session. \
If the task needs a specific project directory (for CLAUDE.md context, scripts, etc.), \
set `project_dir`. Example: a daily job search runs in `/mntc/code/sita-job-search/`.

### Priority
Infer priority from context — default to "normal". Respect explicit cues:
- "high priority" or "important" → `high`
- "urgent" → `urgent` (results interrupt the user immediately)
Don't quiz the user about priority.

### Re-engagement
When you receive `[event user_returned]` with `pending_tasks > 0`, announce the count \
briefly: "3 tasks available for review." Don't list them unless asked. Calibrate your \
greeting to `away_seconds` — a quick return vs. first thing in the morning.

### Review Flow
When the user says "review tasks" or "what's pending":
1. Call **list_queue** to see pending items
2. Present one-line summaries in priority order
3. When the user picks one, call **get_queue_item** to read the full output
4. Present the findings conversationally
5. Wait for their decision: done, defer (come back later), delegate, or followup
6. Call **review_queue_item** with the chosen action
7. If the action is delegate or followup, ask what they want done and handle it \
(create a session, send an email, add a todo, etc.)

### Rollup
If a result shows `run_count > 1`, mention it: "This has run N times since last review." \
The user may want to adjust the schedule.

### Urgent Tasks
When you receive `[event task_completed]` with priority "urgent", announce the result \
immediately via voice. Keep it to one sentence. Don't wait for the user to ask.
"""


class Ring0Manager:
    """Manages the Ring0 meta-agent session."""

    def __init__(
        self,
        port: int,
        auth_manager: Optional[AuthManager] = None,
        config_path: Optional[Path] = None,
        work_dir: Optional[Path] = None,
    ) -> None:
        self._port = port
        self._auth_manager = auth_manager
        self._config_path = config_path or RING0_CONFIG_PATH
        self._work_dir = work_dir or RING0_WORK_DIR
        self._service_token: Optional[str] = None
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

    @property
    def model(self) -> Optional[str]:
        return self._model

    def _get_service_token(self) -> Optional[str]:
        """Get or create a service token for Ring0 MCP API calls."""
        if not self._auth_manager or not self._auth_manager.enabled:
            return None
        if not self._service_token:
            self._service_token = self._auth_manager.create_service_token("ring0")
            logger.info("[ring0] Created service token for MCP API access")
        return self._service_token

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

    async def ensure_session(
        self,
        launcher: CliLauncher,
        ws_bridge: WsBridge,
        backend_type: str = "claude",
    ) -> str:
        """Ensure the Ring0 session is running. Always uses the fixed session ID.

        On restart, reuses the persisted session and resumes the CLI conversation.
        Returns the session_id.
        """
        session_id = self._session_id
        work_dir = self._work_dir
        work_dir.mkdir(parents=True, exist_ok=True)
        mcp_config_path = self._write_config_files(work_dir)

        if backend_type == "codex":
            self._ensure_codex_mcp(work_dir)
        elif backend_type == "opencode":
            self._ensure_opencode_mcp(work_dir)

        session_names.set_name(session_id, "Ring0", unique=False)

        info = launcher.get_session(session_id)

        if info and info.state not in ("exited",):
            # Session is alive — nothing to do
            logger.info("[ring0] Session %s already running", session_id)
            return session_id

        if info:
            # Session exists but is exited — update config and relaunch
            info.cwd = str(work_dir)
            info.mcpConfig = str(mcp_config_path)
            info.model = self._model if backend_type not in ("codex", "opencode") else None
            if backend_type in ("codex", "opencode"):
                info.cliSessionId = None
            await launcher.relaunch(session_id)
            logger.info("[ring0] Relaunched session %s", session_id)
            return session_id

        # No session in launcher — fresh launch with fixed ID (+ resume if we have a CLI session)
        from server.cli_launcher import LaunchOptions
        options = LaunchOptions(
            sessionId=session_id,
            model=self._model if backend_type not in ("codex", "opencode") else None,
            permissionMode="bypassPermissions",
            cwd=str(work_dir),
            mcpConfig=str(mcp_config_path) if backend_type not in ("codex", "opencode") else None,
            resumeSessionId=self._cli_session_id if backend_type not in ("codex", "opencode") else None,
            backendType=backend_type,
        )

        launcher.launch(options)
        self._save_state()

        logger.info("[ring0] Created session %s in %s (backend=%s)", session_id, work_dir, backend_type)
        return session_id

    async def switch_model(self, model: str, launcher: CliLauncher, ws_bridge: WsBridge) -> str:
        """Switch Ring0 to a different model. Kills current session and starts fresh."""
        old_model = self._model
        self._model = model
        self._cli_session_id = None  # Fresh conversation, don't resume
        self._save_state()
        logger.info("[ring0] Switching model: %s → %s", old_model, model)

        # Kill the current session
        await launcher.kill(self._session_id)

        # Start a fresh session with the new model
        session_id = await self.ensure_session(launcher, ws_bridge)
        logger.info("[ring0] Model switch complete — session %s running %s", session_id, model)
        return session_id

    def _ensure_codex_mcp(self, work_dir: Path) -> None:
        """Ensure the vibr8 MCP server is registered in Codex's global config."""
        import subprocess

        server_dir = Path(__file__).parent.parent.resolve()
        mcp_script = str(server_dir / "server" / "ring0_mcp.py")
        uv_bin = shutil.which("uv") or "uv"
        codex_bin = shutil.which("codex") or "codex"

        env_args: list[str] = [
            "--env", f"VIBR8_PORT={self._port}",
            "--env", f"VIBR8_SCHEME={'https' if (server_dir / 'certs' / 'cert.pem').exists() else 'http'}",
        ]
        token = self._get_service_token()
        if token:
            env_args.extend(["--env", f"VIBR8_TOKEN={token}"])
        if self._model:
            env_args.extend(["--env", f"RING0_MODEL={self._model}"])

        # Remove existing entry first (idempotent), then add fresh
        subprocess.run(
            [codex_bin, "mcp", "remove", "vibr8"],
            capture_output=True, timeout=10,
        )
        cmd = [
            codex_bin, "mcp", "add", *env_args, "vibr8", "--",
            uv_bin, "run", "--project", str(server_dir), "--no-sync", "python", mcp_script,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            logger.info("[ring0] Registered vibr8 MCP server in Codex config")
        else:
            logger.error("[ring0] Failed to register Codex MCP: %s", result.stderr)

    def _ensure_opencode_mcp(self, work_dir: Path) -> None:
        """Write opencode.jsonc in the work dir with vibr8 MCP server config."""
        server_dir = Path(__file__).parent.parent.resolve()
        mcp_script = str(server_dir / "server" / "ring0_mcp.py")
        uv_bin = shutil.which("uv") or "uv"
        scheme = "https" if (server_dir / "certs" / "cert.pem").exists() else "http"
        token = self._get_service_token()

        env: dict[str, str] = {
            "VIBR8_PORT": str(self._port),
            "VIBR8_SCHEME": scheme,
        }
        if token:
            env["VIBR8_TOKEN"] = token
        if self._model:
            env["RING0_MODEL"] = self._model

        config: dict[str, Any] = {}
        config_path = work_dir / "opencode.jsonc"
        if config_path.exists():
            try:
                import re
                raw = config_path.read_text()
                # Strip single-line comments for JSON parsing
                stripped = re.sub(r'//.*$', '', raw, flags=re.MULTILINE)
                config = json.loads(stripped)
            except Exception:
                config = {}

        config.setdefault("mcp", {})["vibr8"] = {
            "type": "local",
            "command": [uv_bin, "run", "--project", str(server_dir), "--no-sync", "python", mcp_script],
            "environment": env,
        }

        if self._model:
            config["model"] = self._model

        config_path.write_text(json.dumps(config, indent=2))
        logger.info("[ring0] Wrote OpenCode MCP config to %s", config_path)

    def _write_config_files(self, work_dir: Path) -> Path:
        """Write CLAUDE.md/AGENTS.md and MCP config to the Ring0 working directory."""
        claude_md = work_dir / "CLAUDE.md"
        claude_md.write_text(RING0_SYSTEM_PROMPT)
        agents_md = work_dir / "AGENTS.md"
        agents_md.write_text(RING0_SYSTEM_PROMPT)

        server_dir = Path(__file__).parent.parent.resolve()
        mcp_script = str(server_dir / "server" / "ring0_mcp.py")
        uv_bin = shutil.which("uv") or "uv"
        mcp_config = {
            "mcpServers": {
                "vibr8": {
                    "type": "stdio",
                    "command": uv_bin,
                    "args": ["run", "--project", str(server_dir), "--no-sync", "python", mcp_script],
                    "env": {
                        "VIBR8_PORT": str(self._port),
                        "VIBR8_SCHEME": "https" if (server_dir / "certs" / "cert.pem").exists() else "http",
                        **({} if not self._get_service_token() else {"VIBR8_TOKEN": self._get_service_token()}),
                        **({"RING0_MODEL": self._model} if self._model else {}),
                    },
                }
            }
        }
        mcp_config_path = work_dir / "mcp.json"
        mcp_config_path.write_text(json.dumps(mcp_config))
        return mcp_config_path

    # ── Persistence ───────────────────────────────────────────────────────

    def _load_state(self) -> None:
        if self._config_path.exists():
            try:
                data = json.loads(self._config_path.read_text())
                self._enabled = data.get("enabled", False)
                self._events_muted = data.get("eventsMuted", False)
                self._cli_session_id = data.get("cliSessionId")
                self._model = data.get("model")
            except Exception:
                logger.warning("[ring0] Failed to load state from %s", self._config_path)

    def _save_state(self) -> None:
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "enabled": self._enabled,
            "eventsMuted": self._events_muted,
            "sessionId": self._session_id,
            "cliSessionId": self._cli_session_id,
        }
        if self._model:
            data["model"] = self._model
        tmp = self._config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self._config_path)
