"""Ring0 MCP Server — stdio MCP server exposing vibr8 session tools.

Launched as a subprocess by Claude CLI with --mcp-config.
Communicates with vibr8's REST API via localhost.
"""

import asyncio
import os
import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PORT = int(os.environ.get("VIBR8_PORT", "3456"))
BASE_URL = f"http://localhost:{PORT}/api"

mcp = FastMCP("vibr8")


async def _get(path: str) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{BASE_URL}{path}", timeout=10)
        r.raise_for_status()
        return r.json()


async def _post(path: str, body: dict | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{BASE_URL}{path}", json=body, timeout=30)
        if r.status_code >= 400:
            try:
                return r.json()
            except Exception:
                return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
        return r.json()


async def _put(path: str, body: dict | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        r = await client.put(f"{BASE_URL}{path}", json=body, timeout=10)
        if r.status_code >= 400:
            try:
                return r.json()
            except Exception:
                return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
        return r.json()


def _slugify(name: str) -> str:
    """Convert a name to a filesystem-safe slug."""
    s = name.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s)
    return s.strip("-") or "session"


@mcp.tool()
async def create_session(
    name: str,
    backend: str = "claude",
    project_dir: str = "",
    model: str = "",
    initial_message: str = "",
) -> str:
    """Create a new coding session with its own working directory.

    Args:
        name: Human-readable session name (e.g., "auth refactor", "frontend tests").
        backend: Backend type — "claude" (default) or "codex".
        project_dir: Working directory for the session. If empty, creates /mntc/code/{slugified-name}.
        model: Optional model override (e.g., "claude-sonnet-4-6").
        initial_message: Optional first message to send to the session after creation.
    """
    if backend not in ("claude", "codex"):
        return f"Error: backend must be 'claude' or 'codex', got '{backend}'."

    # Resolve working directory
    cwd = project_dir.strip() if project_dir else f"/mntc/code/{_slugify(name)}"
    Path(cwd).mkdir(parents=True, exist_ok=True)

    # Create the session via REST API
    body: dict[str, Any] = {"cwd": cwd, "backend": backend, "name": name}
    if model:
        body["model"] = model
    result = await _post("/ring0/create-session", body)

    if result.get("error"):
        return f"Error creating session: {result['error']}"

    session_id = result.get("sessionId", "")
    if not session_id:
        return f"Error: no sessionId in response: {json.dumps(result)}"

    # Wait for CLI to connect, then send initial message if provided
    if initial_message:
        await asyncio.sleep(2)
        await _post("/ring0/send-message", {"sessionId": session_id, "message": initial_message})

    parts = [f"Session created: {name} (id={session_id[:8]}, cwd={cwd})"]
    if initial_message:
        parts.append(f"Initial message sent: {initial_message[:80]}")
    return "\n".join(parts)


@mcp.tool()
async def list_sessions() -> str:
    """List all active vibr8 sessions with their IDs, names, status, and working directories."""
    sessions = await _get("/ring0/sessions")
    if not sessions:
        return "No sessions found."

    lines = []
    for s in sessions:
        sid = s.get("sessionId", "?")
        name = s.get("name", "unnamed")
        state = s.get("state", "?")
        cwd = s.get("cwd", "")
        backend = s.get("backendType", "claude")
        archived = s.get("archived", False)
        if archived:
            continue
        pending = s.get("pendingPermissions", 0)
        perm_info = f", BLOCKED: {pending} pending permission(s)" if pending else ""
        lines.append(f"- {name} (id={sid[:8]}, state={state}, type={backend}, cwd={cwd}{perm_info})")
    return "\n".join(lines) if lines else "No active sessions."


@mcp.tool()
async def interrupt_session(session_id: str) -> str:
    """Interrupt/cancel a running session (equivalent to Ctrl+C / Escape).

    Use this when the user says "stop", "cancel", "nevermind", or you need to
    halt a session that is doing something wrong.

    Args:
        session_id: The session ID (full or prefix) to interrupt.
    """
    result = await _post("/ring0/interrupt", {"sessionId": session_id})
    if result.get("error"):
        return f"Error: {result['error']}"
    return f"Interrupted session {session_id[:8]}."


@mcp.tool()
async def send_message(session_id: str, message: str) -> str:
    """Send a message to a specific session.

    Args:
        session_id: The session ID (full or prefix) to send the message to.
        message: The message text to send.
    """
    result = await _post("/ring0/send-message", {"sessionId": session_id, "message": message})
    return f"Message sent to session {session_id[:8]}."


@mcp.tool()
async def switch_ui(session_id: str, client_id: str = "") -> str:
    """Switch the browser UI to show a specific session.

    Args:
        session_id: The session ID to switch to.
        client_id: Optional client ID, name, or prefix to target. If provided, only that browser instance switches.
                   If omitted, all connected browsers switch.
    """
    body: dict[str, str] = {"sessionId": session_id}
    if client_id:
        resolved, err = await _resolve_client(client_id)
        if err:
            return err
        body["clientId"] = resolved
    result = await _post("/ring0/switch-ui", body)
    if result.get("error"):
        return f"Error: {result['error']}"
    target = f"client {client_id[:8]}" if client_id else "all clients"
    return f"Switched {target} to session {session_id[:8]}."


def _extract_assistant_text(message: Any) -> tuple[str, bool]:
    """Extract readable text from an assistant message.

    Returns (text, has_real_text) where has_real_text is True if
    the message contains actual text content beyond just tool-use markers.
    """
    if isinstance(message, str):
        return message.strip(), bool(message.strip())
    if isinstance(message, dict):
        message = message.get("content", "")
    if isinstance(message, list):
        text_parts = []
        tool_parts = []
        for block in message:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text", "").strip():
                text_parts.append(block["text"].strip())
            elif block.get("type") == "tool_use":
                name = block.get("name", "unknown")
                tool_parts.append(f"[used tool: {name}]")
        parts = text_parts + tool_parts
        return " ".join(parts), bool(text_parts)
    if isinstance(message, str):
        return message.strip(), bool(message.strip())
    return "", False


@mcp.tool()
async def get_session_output(session_id: str) -> str:
    """Get recent messages from a specific session.

    Args:
        session_id: The session ID to get output from.
    """
    result = await _get(f"/ring0/session-output/{session_id}")
    messages = result.get("messages", [])
    permissions = result.get("pendingPermissions", [])

    if not messages and not permissions:
        return "No messages in this session yet."

    # Scan ALL messages, keep last 10 meaningful lines.
    lines = []
    trailing_tool_count = 0  # tool-use messages after last text response
    for msg in messages:
        role = msg.get("type", "?")
        if role == "user_message":
            content = msg.get("content", "")
            lines.append(f"User: {content}")
            trailing_tool_count = 0
        elif role == "assistant":
            raw_message = msg.get("message", "")
            if isinstance(raw_message, dict) and "content" in raw_message:
                raw_message = raw_message["content"]
            text, has_real_text = _extract_assistant_text(raw_message)
            if has_real_text:
                lines.append(f"Assistant: {text[:500]}")
                trailing_tool_count = 0
            else:
                trailing_tool_count += 1
        elif role == "result":
            data = msg.get("data", {})
            if data.get("is_error"):
                lines.append(f"Error: {', '.join(data.get('errors', []))}")
                trailing_tool_count = 0
    # Keep last 10 meaningful lines
    lines = lines[-10:]

    # If the session is mid-work (many tool calls after last text), note it
    if trailing_tool_count > 3:
        lines.append(f"[Session is actively working — {trailing_tool_count} tool calls since last text response]")

    if permissions:
        lines.append("")
        lines.append("--- PENDING PERMISSIONS (session is blocked, waiting for response) ---")
        for perm in permissions:
            rid = perm.get("request_id", "?")
            tool = perm.get("tool_name", "?")
            desc = perm.get("description", "")
            inp = json.dumps(perm.get("input", {}))[:300]
            lines.append(f"  [{rid}] {tool}: {desc or inp}")

    return "\n".join(lines) if lines else "No readable messages."


@mcp.tool()
async def get_active_clients() -> str:
    """List all known browser clients with names, device info, and online status.

    Shows both online and offline clients with their metadata.
    """
    clients = await _get("/clients")
    if not clients:
        return "No clients known."
    lines = []
    for c in clients:
        cid = c.get("clientId", "?")
        name = c.get("name", "")
        online = c.get("online", False)
        sid = c.get("sessionId", "")
        ws_role = c.get("wsRole", c.get("role", ""))
        role = c.get("role", "")
        dev = c.get("deviceInfo", {})

        label = f'"{name}"' if name else f"(unnamed)"
        status = "online" if online else "offline"
        parts = [f"- {label} (id={cid[:8]}..., {status}"]
        if sid:
            parts[0] += f", session={sid[:8]}..."
        if ws_role:
            parts[0] += f", wsRole={ws_role}"
        if role and role != ws_role:
            parts[0] += f", role={role}"
        parts[0] += ")"

        if dev:
            platform = dev.get("platform", "")
            w = dev.get("screenWidth", "")
            h = dev.get("screenHeight", "")
            touch = dev.get("touchSupport", False)
            info_parts = []
            if platform:
                info_parts.append(f"Platform: {platform}")
            if w and h:
                info_parts.append(f"Screen: {w}x{h}")
            info_parts.append(f"Touch: {'yes' if touch else 'no'}")
            parts.append(f"  {', '.join(info_parts)}")

        if c.get("description"):
            parts.append(f"  Description: {c['description']}")

        lines.append("\n".join(parts))
    return "\n".join(lines)


async def _resolve_client(identifier: str) -> tuple[str, str | None]:
    """Resolve a client by name, UUID, or prefix.

    Returns (resolved_client_id, error_message).
    If ambiguous or not found, error_message describes the issue.
    """
    clients = await _get("/clients")
    if not clients:
        return "", "No clients known."

    # Exact UUID match
    for c in clients:
        if c.get("clientId") == identifier:
            return c["clientId"], None

    # Exact name match (case-insensitive)
    by_name = [c for c in clients if c.get("name", "").lower() == identifier.lower()]
    if len(by_name) == 1:
        return by_name[0]["clientId"], None
    if len(by_name) > 1:
        desc_lines = [f"Multiple clients named \"{identifier}\":"]
        for c in by_name:
            cid = c["clientId"][:8]
            dev = c.get("deviceInfo", {})
            platform = dev.get("platform", "unknown")
            w = dev.get("screenWidth", "?")
            h = dev.get("screenHeight", "?")
            status = "online" if c.get("online") else "offline"
            role = c.get("role", "")
            desc = f"  - {cid}: role={role}, Platform: {platform}, Screen: {w}x{h}, {status}"
            if c.get("description"):
                desc += f" — {c['description']}"
            desc_lines.append(desc)
        desc_lines.append("Please specify by ID prefix or give them unique names.")
        return "", "\n".join(desc_lines)

    # UUID prefix match
    by_prefix = [c for c in clients if c.get("clientId", "").startswith(identifier)]
    if len(by_prefix) == 1:
        return by_prefix[0]["clientId"], None
    if len(by_prefix) > 1:
        return "", f"Ambiguous prefix '{identifier}' matches {len(by_prefix)} clients."

    return "", f"No client matching '{identifier}'."


@mcp.tool()
async def respond_to_permission(session_id: str, request_id: str, behavior: str, message: str = "") -> str:
    """Respond to a pending permission request in a session.

    Use get_session_output to see pending permissions and their request IDs.

    Args:
        session_id: The session ID containing the permission.
        request_id: The permission request ID (shown in brackets in get_session_output).
        behavior: "allow" or "deny".
        message: Optional reason message (used when denying).
    """
    if behavior not in ("allow", "deny"):
        return "Error: behavior must be 'allow' or 'deny'."
    result = await _post("/ring0/respond-permission", {
        "sessionId": session_id,
        "requestId": request_id,
        "behavior": behavior,
        "message": message,
    })
    if result.get("error"):
        return f"Error: {result['error']}"
    return f"Permission {request_id[:8]} {behavior}ed."


@mcp.tool()
async def query_client(client_id: str, method: str, params: str = "") -> str:
    """Send an RPC query to a specific browser client and get their response.

    Args:
        client_id: The client ID, name, or prefix to query.
        method: The RPC method to call. Available methods:
            - "get_state" — returns currentSessionId, dateTime, timeZone, locale, url
            - "get_location" — returns latitude, longitude, accuracy (may prompt user)
            - "get_visibility" — returns visible, state, hasFocus
            - "send_notification" — show browser notification. params: {"title": "...", "body": "..."}
            - "read_clipboard" — read clipboard text (may prompt user)
            - "write_clipboard" — write text to clipboard. params: {"text": "..."}
            - "open_url" — open a URL in a new tab/window. params: {"url": "..."}
            - "list_audio_devices" — list available audio input/output devices. No params.
            - "set_audio_output" — set the audio output device. params: {"deviceId": "..."}
            - "set_audio_input" — switch microphone input device. params: {"deviceId": "..."}
            - "bring_to_foreground" — bring the vibr8 app to front (Android native). No params.
            - "launch_app" — launch an app on Android. params: {"package": "com.example.app"} or {"url": "https://..."} or {"url": "tel:+1234567890"}
            - "capture_screenshot" — capture a screenshot of the client's current view. params: {"format": "png"|"jpeg", "quality": 0.0-1.0}. Use the capture_screen tool instead for a higher-level interface.
            - "set_scale" — set second screen font scale. params: {"scale": 1.5} for absolute, or {"delta": 0.25} for relative adjustment.
        params: Optional JSON string of parameters to pass to the method (e.g., '{"title": "Hello", "body": "World"}').
    """
    resolved, err = await _resolve_client(client_id)
    if err:
        return err
    body: dict[str, Any] = {"clientId": resolved, "method": method}
    if params:
        try:
            body["params"] = json.loads(params)
        except json.JSONDecodeError:
            return f"Error: invalid params JSON: {params}"
    result = await _post("/ring0/query-client", body)
    if result.get("error"):
        return f"Error: {result['error']}"
    return json.dumps(result.get("result", {}), indent=2)


@mcp.tool()
async def update_client_metadata(
    client_id: str,
    name: str = "",
    description: str = "",
    role: str = "",
) -> str:
    """Update metadata for a client device (name, description, role).

    Args:
        client_id: Client ID, name, or prefix to identify the client.
        name: Display name for this client (e.g., "Laptop", "Tesla", "Phone").
        description: Free text description of the client.
        role: What this client is used for (e.g., "primary", "car display", "second screen").
    """
    resolved, err = await _resolve_client(client_id)
    if err:
        return err
    updates: dict[str, str] = {}
    if name:
        updates["name"] = name
    if description:
        updates["description"] = description
    if role:
        updates["role"] = role
    if not updates:
        return "Error: provide at least one of name, description, or role."
    result = await _put(f"/clients/{resolved}", updates)
    if result.get("error"):
        return f"Error: {result['error']}"
    new_name = result.get("name", "")
    return f"Updated client {resolved[:8]}" + (f' (name: "{new_name}")' if new_name else "") + "."


@mcp.tool()
async def launch_app(package: str = "", url: str = "") -> str:
    """Launch an app on the user's Android device.

    Uses the native WebSocket connection to send a launch_app command.
    Requires an Android device with the vibr8 native layer connected.

    Args:
        package: Android package name (e.g., "com.google.android.gm" for Gmail,
                 "com.android.chrome" for Chrome, "com.google.android.apps.maps" for Maps).
        url: URL or intent URI to open (e.g., "https://gmail.com", "tel:+1234567890",
             "mailto:user@example.com"). Can be used instead of or together with package.
    """
    if not package and not url:
        return "Error: provide either 'package' or 'url' (or both)."

    # Find a connected client to send the command to
    clients = await _get("/ring0/clients")
    if not clients:
        return "Error: no clients connected."

    # Pick the first primary client
    target_id = None
    for cid, info in clients.items():
        role = info.get("role", "primary") if isinstance(info, dict) else "primary"
        if role == "primary":
            target_id = cid
            break
    if not target_id:
        target_id = next(iter(clients))

    params: dict[str, str] = {}
    if package:
        params["package"] = package
    if url:
        params["url"] = url

    result = await _post("/ring0/query-client", {
        "clientId": target_id,
        "method": "launch_app",
        "params": params,
    })
    if result.get("error"):
        return f"Error: {result['error']}"
    return f"Launched {'package ' + package if package else url}."


@mcp.tool()
async def pair_second_screen(code: str, primary_client_id: str = "") -> str:
    """Pair a second screen display using its pairing code.

    The second screen shows a pairing code on its display. Enter that code here
    to complete the pairing. If primary_client_id is omitted, the first connected
    primary client is used.

    Args:
        code: The pairing code displayed on the second screen.
        primary_client_id: Optional primary client ID to pair with. If omitted,
                          uses the first connected primary client.
    """
    if not primary_client_id:
        # Find first connected primary client
        clients = await _get("/ring0/clients")
        for c in clients:
            if c.get("online") and c.get("role") != "secondscreen":
                primary_client_id = c["clientId"]
                break
        if not primary_client_id:
            return "Error: no online primary client found. Provide a primary_client_id."

    result = await _post("/second-screen/pair", {
        "code": code,
        "clientId": primary_client_id,
    })
    if result.get("error"):
        return f"Error: {result['error']}"
    return f"Paired successfully. Second screen {result.get('secondScreenClientId', '?')[:8]}... is now connected."


@mcp.tool()
async def list_second_screens() -> str:
    """List all paired second screen displays and their online/offline status.

    Returns information about each paired second screen including which primary
    client it's paired to and whether it's currently online.
    """
    screens = await _get("/second-screen/list")
    if not screens:
        return "No second screens paired."
    lines = []
    for s in screens:
        status = "online" if s.get("online") else "offline"
        enabled = "enabled" if s.get("enabled", True) else "disabled"
        lines.append(
            f"- Screen {s['clientId'][:8]}... ({status}, {enabled}, "
            f"paired_to={s['pairedClientId'][:8]}...)"
        )
    return "\n".join(lines)


@mcp.tool()
async def show_on_second_screen(
    content: str = "",
    content_type: str = "markdown",
    client_id: str = "",
    image_data: str = "",
    image_mime: str = "image/png",
    filename: str = "",
    pdf_data: str = "",
) -> str:
    """Push content to one or all connected second screen displays.

    Args:
        content: The content to display. Depends on content_type:
                 - markdown: the markdown text
                 - image: a URL, or empty if using image_data
                 - file: the file text content
                 - pdf: a URL, or empty if using pdf_data
                 - html: the HTML string to render
                 - session: the session ID to mirror
                 - home: ignored (returns second screen to default view)
        content_type: Type of content. One of:
                 "markdown", "image", "file", "pdf", "html", "session", "home".
        client_id: Optional specific second screen client ID. If omitted,
                   sends to all connected second screens.
        image_data: Base64-encoded image bytes. Only used when content_type is "image".
        image_mime: MIME type for image_data (default "image/png").
        filename: Display filename for content_type "file".
        pdf_data: Base64-encoded PDF bytes. Only used when content_type is "pdf".
    """
    # Build the actual content to send
    display_content = content
    if content_type == "image" and image_data:
        display_content = f"data:{image_mime};base64,{image_data}"
    elif content_type == "pdf" and pdf_data:
        display_content = f"data:application/pdf;base64,{pdf_data}"

    # Get all second screen clients (online and enabled)
    screens = await _get("/second-screen/list")
    online_screens = [s for s in screens if s.get("online") and s.get("enabled", True)]

    if not online_screens:
        return "No second screens are online and enabled."

    targets = online_screens
    if client_id:
        # Try name resolution first, fall back to prefix match on screen list
        resolved, err = await _resolve_client(client_id)
        if not err:
            targets = [s for s in online_screens if s["clientId"] == resolved]
        else:
            targets = [s for s in online_screens if s["clientId"].startswith(client_id)]
        if not targets:
            return f"No online second screen matching '{client_id}'."

    results = []
    for screen in targets:
        # Session mirroring and home use a different RPC method
        if content_type == "session":
            body: dict[str, Any] = {
                "clientId": screen["clientId"],
                "method": "mirror_session",
                "params": {"sessionId": content},
            }
        elif content_type == "home":
            body = {
                "clientId": screen["clientId"],
                "method": "mirror_session",
                "params": {"sessionId": None},
            }
        else:
            params: dict[str, str] = {"type": content_type, "content": display_content}
            if filename:
                params["filename"] = filename
            body = {
                "clientId": screen["clientId"],
                "method": "show_content",
                "params": params,
            }
        result = await _post("/ring0/query-client", body)
        if result.get("error"):
            results.append(f"Error sending to {screen['clientId'][:8]}: {result['error']}")
        else:
            results.append(f"Sent to {screen['clientId'][:8]}")

    return "\n".join(results)


@mcp.tool()
async def query_second_screen(client_id: str = "") -> str:
    """Query second screen(s) for device info — screen dimensions, pixel ratio, user agent, etc.

    Args:
        client_id: Optional specific second screen client ID. If omitted,
                   queries all connected second screens.
    """
    screens = await _get("/second-screen/list")
    online_screens = [s for s in screens if s.get("online")]

    if not online_screens:
        return "No second screens are online."

    targets = online_screens
    if client_id:
        targets = [s for s in online_screens if s["clientId"].startswith(client_id)]
        if not targets:
            return f"No online second screen matching '{client_id}'."

    results = []
    for screen in targets:
        body: dict[str, Any] = {
            "clientId": screen["clientId"],
            "method": "get_device_info",
        }
        result = await _post("/ring0/query-client", body)
        if result.get("error"):
            results.append(f"Screen {screen['clientId'][:8]}: error — {result['error']}")
        else:
            info = result.get("result", result)
            lines = [f"Screen {screen['clientId'][:8]}:"]
            for key, val in info.items():
                lines.append(f"  {key}: {val}")
            results.append("\n".join(lines))

    return "\n\n".join(results)


@mcp.tool()
async def set_second_screen_scale(
    scale: float = 0,
    delta: float = 0,
    client_id: str = "",
) -> str:
    """Adjust font size / scale on second screen displays.

    Use scale for absolute values (0.5–3.0, where 1.0 is default),
    or delta for relative adjustments (e.g., 0.25 to increase, -0.25 to decrease).

    Args:
        scale: Absolute scale (0.5–3.0). 1.0 = default. Leave at 0 to use delta instead.
        delta: Relative adjustment (e.g., 0.25 to increase, -0.25 to decrease).
        client_id: Target specific second screen (name, prefix, or full ID). Omit for all screens.
    """
    if scale == 0 and delta == 0:
        return "Error: provide either 'scale' (absolute) or 'delta' (relative adjustment)."

    screens = await _get("/second-screen/list")
    online_screens = [s for s in screens if s.get("online")]
    if not online_screens:
        return "No second screens are online."

    targets = online_screens
    if client_id:
        resolved, err = await _resolve_client(client_id)
        if not err:
            targets = [s for s in online_screens if s["clientId"] == resolved]
        else:
            targets = [s for s in online_screens if s["clientId"].startswith(client_id)]
        if not targets:
            return f"No online second screen matching '{client_id}'."

    params: dict[str, float] = {}
    if scale != 0:
        params["scale"] = scale
    else:
        params["delta"] = delta

    results = []
    for screen in targets:
        body: dict[str, Any] = {
            "clientId": screen["clientId"],
            "method": "set_scale",
            "params": params,
        }
        result = await _post("/ring0/query-client", body)
        if result.get("error"):
            results.append(f"Screen {screen['clientId'][:8]}: error — {result['error']}")
        else:
            new_scale = result.get("result", {}).get("scale", "?")
            results.append(f"Screen {screen['clientId'][:8]}: scale set to {new_scale}")

    return "\n".join(results)


@mcp.tool()
async def set_tv_safe(
    enabled: bool = True,
    padding_percent: float = 0,
    client_id: str = "",
) -> str:
    """Enable or disable TV-safe mode on second screen displays.

    TV-safe mode adds padding around the content to keep it visible on TVs
    where bezels cover the screen edges. Off by default (edge-to-edge).

    Args:
        enabled: True to enable TV-safe padding, False for edge-to-edge.
        padding_percent: Custom padding percentage (e.g. 1.0, 2.5, 5.0). When enabled
                         without specifying this, the current value is kept (default 2.5%).
        client_id: Target specific second screen (name, prefix, or full ID). Omit for all screens.
    """
    screens = await _get("/second-screen/list")
    online_screens = [s for s in screens if s.get("online")]
    if not online_screens:
        return "No second screens are online."

    targets = online_screens
    if client_id:
        resolved, err = await _resolve_client(client_id)
        if not err:
            targets = [s for s in online_screens if s["clientId"] == resolved]
        else:
            targets = [s for s in online_screens if s["clientId"].startswith(client_id)]
        if not targets:
            return f"No online second screen matching '{client_id}'."

    params: dict[str, Any] = {"enabled": enabled}
    if padding_percent > 0:
        params["padding_percent"] = padding_percent

    results = []
    for screen in targets:
        body: dict[str, Any] = {
            "clientId": screen["clientId"],
            "method": "set_tv_safe",
            "params": params,
        }
        result = await _post("/ring0/query-client", body)
        if result.get("error"):
            results.append(f"Screen {screen['clientId'][:8]}: error — {result['error']}")
        else:
            r = result.get("result", {})
            state = "enabled" if r.get("tvSafe") else "disabled"
            pct = r.get("paddingPercent", 0)
            detail = f" ({pct}%)" if pct > 0 else ""
            results.append(f"Screen {screen['clientId'][:8]}: TV-safe {state}{detail}")

    return "\n".join(results)


@mcp.tool()
async def toggle_second_screen(client_id: str, enabled: bool = True) -> str:
    """Enable or disable a second screen. Disabled screens won't receive pushed content.

    Args:
        client_id: The second screen client ID (or prefix).
        enabled: True to enable, False to disable.
    """
    # Resolve prefix to full client ID
    screens = await _get("/second-screen/list")
    matches = [s for s in screens if s["clientId"].startswith(client_id)]
    if not matches:
        return f"No second screen matching '{client_id}'."
    if len(matches) > 1:
        return f"Ambiguous prefix '{client_id}' matches {len(matches)} screens."

    full_id = matches[0]["clientId"]
    result = await _post("/second-screen/toggle", {"clientId": full_id, "enabled": enabled})
    if result.get("error"):
        return f"Error: {result['error']}"
    state = "enabled" if result.get("enabled") else "disabled"
    return f"Screen {full_id[:8]} is now {state}."


@mcp.tool()
async def capture_screen(
    client_id: str = "",
    format: str = "png",
    quality: float = 0.8,
) -> str:
    """Capture a screenshot of a browser client's current view.

    Returns the file path of the saved screenshot image and its dimensions.
    Works with both main UI clients and second screen displays.

    Args:
        client_id: Client ID, name, or prefix to capture. If empty, captures the first connected client.
        format: Image format — "png" or "jpeg". Default "png".
        quality: JPEG quality 0.0–1.0. Only used for jpeg format. Default 0.8.
    """
    import base64
    import tempfile

    # Resolve client ID
    if not client_id:
        clients = await _get("/clients")
        online = [c for c in clients if c.get("online")]
        if not online:
            return "Error: no clients connected."
        client_id = online[0].get("clientId", "")
    else:
        resolved, err = await _resolve_client(client_id)
        if err:
            return err
        client_id = resolved

    body: dict[str, Any] = {
        "clientId": client_id,
        "method": "capture_screenshot",
        "params": {"format": format, "quality": quality},
    }
    result = await _post("/ring0/query-client", body)
    if result.get("error"):
        return f"Error: {result['error']}"

    data = result.get("result", {})
    image_b64 = data.get("image", "")
    if not image_b64:
        return "Error: no image data returned from client."

    # Save to temp file
    ext = "jpg" if format == "jpeg" else "png"
    tmp = tempfile.NamedTemporaryFile(
        prefix="vibr8_screenshot_", suffix=f".{ext}", delete=False
    )
    tmp.write(base64.b64decode(image_b64))
    tmp.close()

    width = data.get("width", "?")
    height = data.get("height", "?")
    size_kb = len(image_b64) * 3 // 4 // 1024

    return (
        f"Screenshot captured: {tmp.name}\n"
        f"Dimensions: {width}x{height}, Size: ~{size_kb}KB, Format: {format}\n"
        f"Client: {client_id[:12]}..."
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
