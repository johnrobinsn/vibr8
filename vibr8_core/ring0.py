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
    from vibr8_core.cli_launcher import CliLauncher
    from vibr8_core.ws_bridge import WsBridge

from vibr8_core import session_names

logger = logging.getLogger(__name__)

RING0_CONFIG_PATH = Path.home() / ".vibr8" / "ring0.json"
RING0_WORK_DIR = Path.home() / ".vibr8" / "ring0"
RING0_SESSION_ID = "ring0"

_RING0_PROMPT_HEADER = """\
# Ring0 — Voice-Controlled Meta-Agent

You are Ring0, a meta-agent that orchestrates other agent sessions in vibr8
(Claude Code, Codex, OpenCode, Hermes, and computer-use). You receive voice
transcripts from the user and decide how to route them.

## Memory

At the start of every conversation, read your memory files:
1. Read `~/.vibr8/ring0/memory/MEMORY.md` for the index
2. Read any relevant topic files linked from the index

This is mandatory — never ask the user where something is if it might be in memory.

## Your MCP Tools

- **create_session** — Create a new session (claude, codex, opencode, hermes, or computer-use backend)
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
- **share_artifact** — Create a persistent artifact (optionally push to viewers)
- **list_artifacts** / **delete_artifact** — Manage artifacts

"""

_MODEL_SWITCHING_CLAUDE = """\
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

"""

_MODEL_SWITCHING_HERMES = """\
## Model Switching

You are running on Hermes, which selects models from `~/.hermes/config.yaml`.
The active model is configured there or via `session/set_model`.

Recognize these voice patterns:
- **Switch requests:** "switch to opus", "use gpt-5", "go to deepseek"
- **Model queries:** "what model are you using", "which model"

**Aliases (Hermes):** "opus" → claude-opus-4-20250514, "sonnet" → \
claude-sonnet-4-20250514, "gpt-5" → gpt-5.5, "gpt-4" → gpt-4o, "deepseek" → deepseek-r1

**For model queries:** Tell the user the current model. Check the `RING0_MODEL` \
environment variable; otherwise state your best guess based on session context.

**For switch requests:**
1. Save any important in-progress context to memory files first
2. Tell the user what you're saving and which model you're switching to
3. Call `switch_ring0_model` with the target model id (or alias)
4. This will kill your current session and start a fresh one — your response after \
calling the tool is the last thing you'll say in this session

"""

_MODEL_SWITCHING_CODEX = """\
## Model Switching

You are running on Codex. The active model is configured via your Codex CLI \
config (`~/.codex/config.toml`) and the codex `models_cache.json`.

Recognize these voice patterns:
- **Switch requests:** "switch to gpt-5 codex", "use the max model"
- **Model queries:** "what model are you using", "which model"

**Aliases (Codex):** "codex" → gpt-5.3-codex, "max" → gpt-5.1-codex-max, \
"mini" → gpt-5.1-codex-mini

**For switch requests:**
1. Save any important in-progress context to memory files first
2. Tell the user what you're saving and which model you're switching to
3. Call `switch_ring0_model` with the target model id (or alias)
4. This will kill your current session and start a fresh one

"""

_MODEL_SWITCHING_OPENCODE = """\
## Model Switching

You are running on OpenCode. Models are addressed as `provider/model` (e.g. \
`google/gemini-2.5-pro`, `openai/gpt-4o`). The active model is configured in \
`opencode.jsonc` in your working directory.

Recognize these voice patterns:
- **Switch requests:** "switch to gemini", "use gpt-4o", "switch to claude sonnet"
- **Model queries:** "what model are you using", "which model"

**Aliases (OpenCode):** "gemini" → google/gemini-2.5-pro, "flash" → \
google/gemini-2.5-flash, "gpt-4o" → openai/gpt-4o, "sonnet" → \
anthropic/claude-sonnet-4-20250514, "grok" → xai/grok-3, "llama" → groq/llama-3.3-70b

**For switch requests:**
1. Save any important in-progress context to memory files first
2. Tell the user what you're saving and which model you're switching to
3. Call `switch_ring0_model` with the target model id (or alias)
4. This will kill your current session and start a fresh one

"""

_RING0_PROMPT_BODY = """\
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

## Artifacts & Viewer Pane

The **viewer pane** is an integrated panel in the primary vibr8 UI that shows content
alongside the chat. It defaults to the **artifact list** — a curated collection of
persistent content items.

### Tools
- **share_artifact** — Create a persistent artifact (optionally push to viewers)
- **list_artifacts** / **delete_artifact** — Manage artifacts

### When to Create Artifacts
- Important outputs: summaries, plans, architecture docs, diagrams
- When the user says "save this", "keep this", "pin this"
- Generated files worth quick reference (specs, configs, reports)

### Viewer Pane Behavior
- `show_on_second_screen` pushes content to the viewer pane AND external second screens
- `client_id="self"` targets only the viewer pane (not external screens)
- The viewer pane opens automatically when content is pushed

## Second Screen Displays

Users can pair "second screen" devices — monitors, laptops, TVs, tablets — that act as
passive displays you can push content to. This is useful when the user is interacting
via voice (phone in pocket) but has a larger screen nearby for viewing.

### Tools
- **list_second_screens** — See all paired second screens, online/offline and enabled/disabled status
- **query_second_screen** — Query device info (screen dimensions, pixel ratio, user agent, etc.)
- **toggle_second_screen** — Enable or disable a second screen. Disabled screens are skipped by show_on_second_screen
- **show_on_second_screen** — Push content to viewers (integrated pane + second screens). Content types:
  - `markdown` — Rich markdown (headings, lists, code blocks, tables)
  - `image` — Image via URL or base64 (`image_data` + `image_mime` params)
  - `file` — File viewer with filename header (`filename` param) and scrollable text
  - `pdf` — PDF viewer via URL or base64 (`pdf_data` param)
  - `html` — Render arbitrary HTML
  - `session` — Mirror a session's live chat (set `content` to the session ID)
  - `home` — Return viewers to default view (artifact list)

### Client Targeting
- No `client_id` → send to all (viewer pane + all second screens)
- `client_id="self"` → viewer pane only (not external screens)
- Specific `client_id` → that screen/client only

### Session Mirroring
You can mirror any session's live chat on a second screen. The user may say:
- "Show session X on the second screen" -> `content_type="session"`, `content=sessionId`
- "Switch second screen to session X" -> same as above
- "Second screen go home" / "Go back" -> `content_type="home"`
When mirroring, the second screen shows that session's full chat with live streaming.

### Events You'll Receive
- `[event second_screen_paired]` — A new second screen was just paired
- `[event second_screen_connected]` — A paired second screen came online
- `[event second_screen_disconnected]` — A paired second screen went offline
- `[event second_screen_unpaired]` — A second screen was unpaired

### When to Use
- When pushing visual content (images, code, long output, diagrams), prefer the viewer
  pane — keep voice responses brief
- If content would be hard to view on a small screen, offer: "Want me to show this on
  the viewer?"
- Use session mirroring when the user wants to monitor another agent's work on a display
- By default, all screens show the artifact list — only push specific content when
  it adds value

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

_MODEL_SWITCHING_BY_BACKEND: dict[str, str] = {
    "claude": _MODEL_SWITCHING_CLAUDE,
    "hermes": _MODEL_SWITCHING_HERMES,
    "codex": _MODEL_SWITCHING_CODEX,
    "opencode": _MODEL_SWITCHING_OPENCODE,
}


def build_ring0_system_prompt(backend_type: str = "claude") -> str:
    """Assemble the Ring0 system prompt with the backend-appropriate
    Model Switching section."""
    model_section = _MODEL_SWITCHING_BY_BACKEND.get(
        backend_type, _MODEL_SWITCHING_CLAUDE,
    )
    return _RING0_PROMPT_HEADER + model_section + _RING0_PROMPT_BODY


# Backwards-compat alias — defaults to the Claude prompt.
RING0_SYSTEM_PROMPT = build_ring0_system_prompt("claude")


class Ring0Manager:
    """Manages the Ring0 meta-agent session."""

    def __init__(
        self,
        port: int,
        auth_manager: Optional[AuthManager] = None,
        config_path: Optional[Path] = None,
        work_dir: Optional[Path] = None,
        scheme: Optional[str] = None,
    ) -> None:
        self._port = port
        self._auth_manager = auth_manager
        self._scheme = scheme
        self._config_path = config_path or RING0_CONFIG_PATH
        self._work_dir = work_dir or RING0_WORK_DIR
        self._service_token: Optional[str] = None
        self._enabled: bool = False
        self._events_muted: bool = False
        self._session_id: str = RING0_SESSION_ID
        self._cli_session_id: Optional[str] = None
        self._model: Optional[str] = None
        self._backend_type: str = "claude"
        # When set, Ring0 MCP tools that touch browser clients, second
        # screens, or artifacts forward to this hub instead of calling the
        # local API. Populated on remote nodes by NodeAgent after the hub
        # issues a service token; left blank on the hub itself.
        self._hub_url: Optional[str] = None
        self._hub_token: Optional[str] = None
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

    @property
    def backend_type(self) -> str:
        return self._backend_type

    def set_backend_type(self, backend_type: str) -> None:
        """Set the backend type used when launching the Ring0 session.

        Caller is responsible for relaunching the session if it is already
        running with the previous backend.
        """
        if backend_type not in ("claude", "codex", "opencode", "hermes"):
            raise ValueError(f"Unsupported Ring0 backend: {backend_type}")
        if backend_type != self._backend_type:
            self._cli_session_id = None
        self._backend_type = backend_type
        self._save_state()

    def set_hub_endpoint(self, hub_http_url: Optional[str], hub_token: Optional[str]) -> None:
        """Tell Ring0's MCP server where the hub lives.

        Call this on remote nodes after registration succeeds; the resulting
        VIBR8_HUB_URL / VIBR8_HUB_TOKEN env vars get baked into the MCP
        config on the next Ring0 launch. The hub itself never calls this —
        Ring0 there only talks to its local API.
        """
        self._hub_url = hub_http_url.rstrip("/") if hub_http_url else None
        self._hub_token = hub_token or None

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
        backend_type: Optional[str] = None,
    ) -> str:
        """Ensure the Ring0 session is running. Always uses the fixed session ID.

        On restart, reuses the persisted session and resumes the CLI conversation.
        If ``backend_type`` is omitted, falls back to the persisted setting.
        Returns the session_id.
        """
        if backend_type is None:
            backend_type = self._backend_type
        else:
            # Caller explicitly chose a backend — persist the choice.
            self._backend_type = backend_type
            self._save_state()

        session_id = self._session_id
        work_dir = self._work_dir
        work_dir.mkdir(parents=True, exist_ok=True)
        mcp_config_path = self._write_config_files(work_dir, backend_type)

        non_claude_backends = ("codex", "opencode", "hermes")

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
            info.model = self._model if backend_type not in non_claude_backends else None
            if backend_type in non_claude_backends:
                info.cliSessionId = None
            await launcher.relaunch(session_id)
            logger.info("[ring0] Relaunched session %s", session_id)
            return session_id

        # No session in launcher — fresh launch with fixed ID (+ resume if we have a CLI session)
        from vibr8_core.cli_launcher import LaunchOptions

        agent_config: dict[str, Any] | None = None
        if backend_type == "hermes":
            agent_config = {"mcpServers": self._build_acp_mcp_servers()}

        options = LaunchOptions(
            sessionId=session_id,
            model=self._model if backend_type not in non_claude_backends else None,
            permissionMode="bypassPermissions",
            cwd=str(work_dir),
            mcpConfig=str(mcp_config_path) if backend_type not in non_claude_backends else None,
            resumeSessionId=self._cli_session_id if backend_type not in non_claude_backends else None,
            backendType=backend_type,
            agentConfig=agent_config,
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

        env_args: list[str] = []
        for k, v in self._get_mcp_env().items():
            env_args.extend(["--env", f"{k}={v}"])

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
        env = self._get_mcp_env()

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

    def _get_mcp_env(self) -> dict[str, str]:
        """Build env vars dict for the Ring0 MCP server process."""
        server_dir = Path(__file__).parent.parent.resolve()
        if self._scheme:
            scheme = self._scheme
        else:
            scheme = "https" if (server_dir / "certs" / "cert.pem").exists() else "http"
        env: dict[str, str] = {
            "VIBR8_PORT": str(self._port),
            "VIBR8_SCHEME": scheme,
        }
        token = self._get_service_token()
        if token:
            env["VIBR8_TOKEN"] = token
        if self._model:
            env["RING0_MODEL"] = self._model
        return env

    def _build_acp_mcp_servers(self) -> list[dict[str, Any]]:
        """Build MCP server list in ACP McpServerStdio format for Hermes."""
        server_dir = Path(__file__).parent.parent.resolve()
        mcp_script = str(server_dir / "server" / "ring0_mcp.py")
        uv_bin = shutil.which("uv") or "uv"
        env = self._get_mcp_env()
        return [{
            "name": "vibr8",
            "command": uv_bin,
            "args": ["run", "--project", str(server_dir), "--no-sync", "python", mcp_script],
            "env": [{"name": k, "value": v} for k, v in env.items()],
        }]

    def _write_config_files(self, work_dir: Path, backend_type: str = "claude") -> Path:
        """Write CLAUDE.md/AGENTS.md and MCP config to the Ring0 working directory.

        The system prompt is assembled with the backend-appropriate Model
        Switching section so Ring0 receives the right aliases and env-var
        guidance for whichever agent is hosting it. Both CLAUDE.md and
        AGENTS.md are written so file-watching backends pick up either."""
        prompt = build_ring0_system_prompt(backend_type)
        claude_md = work_dir / "CLAUDE.md"
        claude_md.write_text(prompt)
        agents_md = work_dir / "AGENTS.md"
        agents_md.write_text(prompt)

        server_dir = Path(__file__).parent.parent.resolve()
        mcp_script = str(server_dir / "server" / "ring0_mcp.py")
        uv_bin = shutil.which("uv") or "uv"
        mcp_config = {
            "mcpServers": {
                "vibr8": {
                    "type": "stdio",
                    "command": uv_bin,
                    "args": ["run", "--project", str(server_dir), "--no-sync", "python", mcp_script],
                    "env": self._get_mcp_env(),
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
                self._backend_type = data.get("backendType", "claude")
            except Exception:
                logger.warning("[ring0] Failed to load state from %s", self._config_path)

    def _save_state(self) -> None:
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "enabled": self._enabled,
            "eventsMuted": self._events_muted,
            "sessionId": self._session_id,
            "cliSessionId": self._cli_session_id,
            "backendType": self._backend_type,
        }
        if self._model:
            data["model"] = self._model
        tmp = self._config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self._config_path)
