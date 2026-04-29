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
_SCHEME = os.environ.get("VIBR8_SCHEME", "http")
BASE_URL = f"{_SCHEME}://localhost:{PORT}/api"
_TOKEN = os.environ.get("VIBR8_TOKEN")
_VERIFY_SSL = _SCHEME != "https"  # Disable for self-signed certs

mcp = FastMCP("vibr8")


def _auth_headers() -> dict[str, str]:
    if _TOKEN:
        return {"Authorization": f"Bearer {_TOKEN}"}
    return {}


async def _get(path: str) -> dict[str, Any]:
    async with httpx.AsyncClient(verify=_VERIFY_SSL) as client:
        r = await client.get(f"{BASE_URL}{path}", headers=_auth_headers(), timeout=10)
        r.raise_for_status()
        return r.json()


async def _post(path: str, body: dict | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(verify=_VERIFY_SSL) as client:
        r = await client.post(f"{BASE_URL}{path}", json=body, headers=_auth_headers(), timeout=30)
        if r.status_code >= 400:
            try:
                return r.json()
            except Exception:
                return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
        return r.json()


async def _put(path: str, body: dict | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(verify=_VERIFY_SSL) as client:
        r = await client.put(f"{BASE_URL}{path}", json=body, headers=_auth_headers(), timeout=10)
        if r.status_code >= 400:
            try:
                return r.json()
            except Exception:
                return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
        return r.json()


async def _delete(path: str) -> dict[str, Any]:
    async with httpx.AsyncClient(verify=_VERIFY_SSL) as client:
        r = await client.delete(f"{BASE_URL}{path}", headers=_auth_headers(), timeout=10)
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
    node_id: str = "",
) -> str:
    """Create a new session.

    Args:
        name: Human-readable session name (e.g., "auth refactor", "frontend tests",
              "Desktop: open Chrome").
        backend: Backend type — "claude" (default), "codex", or "computer-use".
                 Use "computer-use" for desktop/Android GUI tasks (opens apps, clicks, types).
        project_dir: Working directory for the session. Only used for claude/codex backends.
                     If empty, creates /mntc/code/{slugified-name}.
        model: Optional model override (e.g., "claude-sonnet-4-6").
        initial_message: Optional first message to send to the session after creation.
                         For computer-use sessions, this is the task to execute
                         (e.g., "open Chrome and go to google.com").
        node_id: Optional node ID to target. For Android nodes (which can't run sessions
                 locally), the session runs on the host but is associated with the node.
                 For desktop nodes, the session runs on the remote node.
    """
    if backend not in ("claude", "codex", "computer-use"):
        return f"Error: backend must be 'claude', 'codex', or 'computer-use', got '{backend}'."

    body: dict[str, Any] = {"backend": backend, "name": name}
    if model:
        body["model"] = model
    if node_id:
        body["nodeId"] = node_id

    if backend == "computer-use":
        # Computer-use doesn't need a working directory
        pass
    else:
        # Resolve working directory for coding backends
        cwd = project_dir.strip() if project_dir else f"/mntc/code/{_slugify(name)}"
        Path(cwd).mkdir(parents=True, exist_ok=True)
        body["cwd"] = cwd

    result = await _post("/ring0/create-session", body)

    if result.get("error"):
        return f"Error creating session: {result['error']}"

    session_id = result.get("sessionId", "")
    if not session_id:
        return f"Error: no sessionId in response: {json.dumps(result)}"

    # Send initial message (task for computer-use, prompt for claude/codex)
    if initial_message:
        if backend == "computer-use":
            await asyncio.sleep(1)  # Agent starts faster than CLI
        else:
            await asyncio.sleep(2)
        await _post("/ring0/send-message", {"sessionId": session_id, "message": initial_message})

    parts = [f"Session created: {name} (id={session_id[:8]}, backend={backend})"]
    if backend != "computer-use":
        parts.append(f"cwd={body.get('cwd', '')}")
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
        pen = s.get("controlledBy", "ring0")
        pen_info = ", PEN: user (do not send messages)" if pen == "user" else ""
        lines.append(f"- {name} (id={sid[:8]}, state={state}, type={backend}, cwd={cwd}{perm_info}{pen_info})")
    return "\n".join(lines) if lines else "No active sessions."


@mcp.tool()
async def rename_session(session_id: str, new_name: str) -> str:
    """Rename a session.

    Args:
        session_id: The session ID (full or prefix) to rename.
        new_name: The new human-readable name for the session.
    """
    result = await _post("/ring0/rename-session", {"sessionId": session_id, "name": new_name})
    if result.get("error"):
        return f"Error: {result['error']}"
    return f"Renamed session {session_id[:8]} to '{new_name}'."


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
    if result.get("error"):
        return f"Error: {result['error']}. The user is currently working in this session. Wait for them to finish or check back later."
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

    # Use server-side formatted output (no truncation)
    if "formatted" in result:
        return result["formatted"]

    # Fallback: format locally (for older hub versions)
    messages = result.get("messages", [])
    permissions = result.get("pendingPermissions", [])

    if not messages and not permissions:
        return "No messages in this session yet."

    lines = []
    trailing_tool_count = 0
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
                lines.append(f"Assistant: {text}")
                trailing_tool_count = 0
            else:
                trailing_tool_count += 1
        elif role == "result":
            data = msg.get("data", {})
            if data.get("is_error"):
                lines.append(f"Error: {', '.join(data.get('errors', []))}")
                trailing_tool_count = 0
    lines = lines[-30:]
    while len(lines) > 1 and sum(len(l) for l in lines) > 50000:
        lines.pop(0)

    if trailing_tool_count > 3:
        lines.append(f"[Session is actively working — {trailing_tool_count} tool calls since last text response]")

    if permissions:
        lines.append("")
        lines.append("--- PENDING PERMISSIONS (session is blocked, waiting for response) ---")
        for perm in permissions:
            rid = perm.get("request_id", "?")
            tool = perm.get("tool_name", "?")
            desc = perm.get("description", "")
            inp = json.dumps(perm.get("input", {}), indent=2)
            if desc:
                lines.append(f"  [{rid}] {tool}: {desc}")
                lines.append(f"    Input: {inp}")
            else:
                lines.append(f"  [{rid}] {tool}: {inp}")

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
            - "set_audio_input" — switch microphone input device. params: {"label": "Bluetooth headset"} (preferred — stable across ID rotations) or {"deviceId": "..."}. Always call list_audio_devices first to see available device labels.
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
async def switch_audio(target: str, client_id: str = "") -> str:
    """Switch audio input/output to a high-level device category.

    Automatically resolves the correct device by scanning available audio devices
    and matching by label. Prefer this over raw query_client set_audio_input calls.

    Args:
        target: Device category — "bluetooth", "speaker", "handset", or "default".
            - bluetooth: Bluetooth headset/earbuds (prioritizes "Bluetooth" in label)
            - speaker: Phone speaker or external speaker (prioritizes "Speakerphone" or "Speaker")
            - handset: Phone earpiece (prioritizes "Earpiece" or "Handset earpiece")
            - default: Reset to system default device
        client_id: Optional client ID, name, or prefix. If empty, auto-resolves to the
            first online client.
    """
    target = target.lower().strip()

    if client_id:
        resolved, err = await _resolve_client(client_id)
    else:
        clients = await _get("/clients")
        online = [c for c in (clients or []) if c.get("online")]
        if not online:
            return "Error: no online clients."
        resolved, err = online[0]["clientId"], None
    if err:
        return err

    devices_result = await _post("/ring0/query-client", {
        "clientId": resolved, "method": "list_audio_devices",
    })
    if devices_result.get("error"):
        return f"Error listing devices: {devices_result['error']}"

    all_devices = devices_result.get("result", {})
    inputs = all_devices.get("inputs", [])
    outputs = all_devices.get("outputs", [])

    if not inputs and not outputs:
        available = json.dumps(all_devices, indent=2)
        return f"Error: no audio devices found. Raw response: {available}"

    if target == "default":
        input_dev = next((d for d in inputs if d.get("deviceId") == "default"), None)
        output_dev = next((d for d in outputs if d.get("deviceId") == "default"), None)
    else:
        keywords: dict[str, list[str]] = {
            "bluetooth": ["bluetooth"],
            "speaker": ["speakerphone", "speaker"],
            "handset": ["earpiece", "handset"],
        }
        if target not in keywords:
            return f"Error: unknown target '{target}'. Use bluetooth, speaker, handset, or default."
        kws = keywords[target]

        def match_device(devices: list[dict]) -> dict | None:
            for kw in kws:
                for d in devices:
                    if kw in d.get("label", "").lower():
                        return d
            return None

        input_dev = match_device(inputs)
        output_dev = match_device(outputs)

    results = []
    if input_dev:
        r = await _post("/ring0/query-client", {
            "clientId": resolved, "method": "set_audio_input",
            "params": {"label": input_dev["label"]},
        })
        if r.get("error"):
            results.append(f"Input error: {r['error']}")
        else:
            results.append(f"Input → {input_dev['label']}")
    else:
        labels = [d.get("label", "?") for d in inputs]
        results.append(f"No {target} input found (available: {labels})")

    if output_dev:
        r = await _post("/ring0/query-client", {
            "clientId": resolved, "method": "set_audio_output",
            "params": {"deviceId": output_dev["deviceId"]},
        })
        if r.get("error"):
            results.append(f"Output error: {r['error']}")
        else:
            results.append(f"Output → {output_dev['label']}")
    else:
        labels = [d.get("label", "?") for d in outputs]
        results.append(f"No {target} output found (available: {labels})")

    return "; ".join(results)


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
    # Push name update to the client via RPC so its UI reflects the change
    if "name" in updates:
        await _post("/ring0/query-client", {
            "clientId": resolved,
            "method": "set_name",
            "params": {"name": updates["name"]},
        })
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
async def pair_second_screen(code: str, username: str = "") -> str:
    """Pair a second screen display using its pairing code.

    The second screen shows a pairing code on its display. Enter that code here
    to complete the pairing. The second screen is paired to the user identity,
    so any client belonging to that user can interact with it.

    Args:
        code: The pairing code displayed on the second screen.
        username: Optional username to pair with. If omitted, uses the default user.
    """
    body: dict[str, str] = {"code": code}
    if username:
        body["username"] = username

    result = await _post("/second-screen/pair", body)
    if result.get("error"):
        return f"Error: {result['error']}"
    return f"Paired successfully. Second screen {result.get('secondScreenClientId', '?')[:8]}... is now connected."


@mcp.tool()
async def list_second_screens() -> str:
    """List all paired second screen displays and their online/offline status.

    Returns information about each paired second screen including which user
    it's paired to and whether it's currently online.
    """
    screens = await _get("/second-screen/list")
    if not screens:
        return "No second screens paired."
    lines = []
    for s in screens:
        status = "online" if s.get("online") else "offline"
        enabled = "enabled" if s.get("enabled", True) else "disabled"
        name = s.get("name", "")
        label = f'"{name}"' if name else f"Screen {s['clientId'][:8]}..."
        lines.append(
            f"- {label} ({s['clientId'][:8]}..., {status}, {enabled}, "
            f"user={s.get('pairedUser', 'default')})"
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
    node_id: str = "",
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
                 - desktop: ignored (streams the remote desktop to second screen)
        content_type: Type of content. One of:
                 "markdown", "image", "file", "pdf", "html", "session", "home", "desktop".
        client_id: Optional specific second screen client ID. If omitted,
                   sends to all connected second screens.
        image_data: Base64-encoded image bytes. Only used when content_type is "image".
        image_mime: MIME type for image_data (default "image/png").
        filename: Display filename for content_type "file".
        pdf_data: Base64-encoded PDF bytes. Only used when content_type is "pdf".
        node_id: Optional node ID for content_type "desktop". If provided, the second
                 screen will connect to this specific node's desktop. Defaults to the
                 second screen's own active node.
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
            if content_type == "desktop" and node_id:
                params["nodeId"] = node_id
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


# ── Extension mapping for push_file_to_second_screen ─────────────────────────
_EXT_TO_CONTENT_TYPE: dict[str, str] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
    ".svg": "image",
    ".pdf": "pdf",
    ".html": "html",
}

_EXT_TO_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
}


@mcp.tool()
async def push_file_to_second_screen(
    path: str,
    content_type: str = "",
    title: str = "",
) -> str:
    """Push a file from disk to all connected second screens without reading it into context.

    Use this instead of show_on_second_screen when the content already exists as a file.
    The server reads the file directly — the file contents never enter your context window.

    Args:
        path: Absolute path to the file on disk.
        content_type: Override auto-detection. One of "markdown", "image", "file",
                      "pdf", "html". Default: inferred from file extension.
        title: Display title shown on the second screen. Default: the filename.
    """
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        return f"File not found: {path}"

    ext = file_path.suffix.lower()

    # Auto-detect content type from extension
    if not content_type:
        content_type = _EXT_TO_CONTENT_TYPE.get(ext, "file")

    if not title:
        title = file_path.name

    # Read file contents
    try:
        if content_type == "image":
            import base64
            raw = file_path.read_bytes()
            mime = _EXT_TO_MIME.get(ext, "application/octet-stream")
            display_content = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
        elif content_type == "pdf":
            import base64
            raw = file_path.read_bytes()
            display_content = f"data:application/pdf;base64,{base64.b64encode(raw).decode()}"
        else:
            display_content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        return f"Error reading {path}: {e}"

    # Get all online, enabled second screens
    screens = await _get("/second-screen/list")
    online_screens = [s for s in screens if s.get("online") and s.get("enabled", True)]

    if not online_screens:
        return "No second screens are online and enabled."

    results = []
    for screen in online_screens:
        params: dict[str, str] = {"type": content_type, "content": display_content}
        if content_type == "file" or content_type == "markdown":
            params["filename"] = title
        body = {
            "clientId": screen["clientId"],
            "method": "show_content",
            "params": params,
        }
        result = await _post("/ring0/query-client", body)
        if result.get("error"):
            results.append(f"Error sending to {screen['clientId'][:8]}: {result['error']}")
        else:
            results.append(f"Sent {title} ({content_type}) to {screen['clientId'][:8]}")

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
            name = screen.get("name", "")
            header = f'"{name}" ({screen["clientId"][:8]})' if name else f"Screen {screen['clientId'][:8]}"
            lines = [f"{header}:"]
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
async def set_dark_mode(
    enabled: bool = True,
    client_id: str = "",
) -> str:
    """Enable or disable dark mode on second screen displays.

    Dark mode uses a dark background with light text. Enabled by default.

    Args:
        enabled: True for dark mode, False for light mode.
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

    results = []
    for screen in targets:
        body: dict[str, Any] = {
            "clientId": screen["clientId"],
            "method": "set_dark_mode",
            "params": {"enabled": enabled},
        }
        result = await _post("/ring0/query-client", body)
        if result.get("error"):
            results.append(f"Screen {screen['clientId'][:8]}: error — {result['error']}")
        else:
            r = result.get("result", {})
            mode = "dark" if r.get("darkMode") else "light"
            results.append(f"Screen {screen['clientId'][:8]}: {mode} mode")

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


@mcp.tool()
async def set_session_mode(session_id: str, mode: str) -> str:
    """Set a session's permission mode.

    Use "plan" to put a session into plan-only mode where it must propose
    a plan before implementing anything. Use "acceptEdits" to return it to
    normal mode where file edits are auto-allowed.

    Args:
        session_id: The session ID (full or prefix).
        mode: "plan" or "acceptEdits".
    """
    if mode not in ("plan", "acceptEdits"):
        return "Error: mode must be 'plan' or 'acceptEdits'."
    result = await _post("/ring0/set-session-mode", {
        "sessionId": session_id,
        "mode": mode,
    })
    if result.get("error"):
        return f"Error: {result['error']}"
    return f"Session {session_id[:8]} mode set to '{mode}'."


@mcp.tool()
async def get_session_mode(session_id: str) -> str:
    """Query the current permission mode of a session.

    Args:
        session_id: The session ID (full or prefix).
    """
    result = await _get(f"/ring0/get-session-mode?sessionId={session_id}")
    if result.get("error"):
        return f"Error: {result['error']}"
    return f"Session {session_id[:8]} is in '{result['mode']}' mode."


@mcp.tool()
async def set_guard_mode(session_id: str, enabled: bool) -> str:
    """Enable or disable voice guard mode for a session.

    When guard mode is on (default), voice input is only processed when
    preceded by the guard word "vibr8". When off, all speech is passed
    through directly.

    Args:
        session_id: The session ID (full or prefix).
        enabled: True to enable guard mode, False to disable it.
    """
    result = await _post("/ring0/set-guard", {
        "sessionId": session_id,
        "enabled": enabled,
    })
    if result.get("error"):
        return f"Error: {result['error']}"
    state = "enabled" if enabled else "disabled"
    return f"Guard mode {state} for session {session_id[:8]}."


@mcp.tool()
async def get_node_environment() -> str:
    """Get information about the node this Ring0 is running on.

    Returns the node name, platform, architecture, whether it's containerized,
    and whether a display is available.
    """
    info = await _get("/ring0/node-environment")
    lines = [
        f"Node: {info['nodeName']}",
        f"Platform: {info['platform']} ({info['arch']})",
        f"Hostname: {info['hostname']}",
        f"Containerized: {'yes (Docker)' if info['containerized'] else 'no'}",
        f"Display: {'available' if info['display'] else 'headless'}",
    ]
    return "\n".join(lines)


@mcp.tool()
async def switch_ring0_model(model: str) -> str:
    """Switch Ring0 to a different Claude model. This will kill the current session
    and start a fresh one with the new model.

    Accepts full model IDs (e.g. "claude-sonnet-4-6") or friendly aliases:
    - "haiku" → claude-haiku-4-5-20251001
    - "sonnet" → claude-sonnet-4-6
    - "opus" → claude-opus-4-6

    WARNING: Calling this tool will terminate your current session. Save any
    important context to memory files BEFORE calling this tool.

    Args:
        model: Model ID or alias (e.g. "haiku", "sonnet", "opus", or full model ID)
    """
    # Resolve aliases locally for the confirmation message
    aliases = {
        "haiku": "claude-haiku-4-5-20251001",
        "sonnet": "claude-sonnet-4-6",
        "opus": "claude-opus-4-6",
    }
    resolved = aliases.get(model.lower().strip(), model.strip())

    result = await _post("/ring0/switch-model", {"model": model})
    if result.get("error"):
        return f"Error: {result['error']}"

    previous = result.get("previous", "unknown")
    return (
        f"Model switch initiated: {previous} → {resolved}\n"
        f"This session will terminate in ~1.5 seconds. "
        f"A new Ring0 session will start with {resolved}."
    )


@mcp.tool()
async def get_ring0_model() -> str:
    """Get the current Ring0 model name.

    Use this when the user asks "what model are you on" or "which model".
    """
    # Check env var first (set by Ring0Manager when launching)
    model = os.environ.get("RING0_MODEL")
    if model:
        return f"Current model: {model}"

    # Fall back to REST API
    status = await _get("/ring0/status")
    model = status.get("model")
    if model:
        return f"Current model: {model}"

    return "Model not specified — using the default Claude model."


# ── Scheduled Tasks & Queue ──────────────────────────────────────────────────


@mcp.tool()
async def create_task(
    name: str,
    prompt: str,
    schedule: str = "daily",
    priority: str = "normal",
    schedule_hour: int = 9,
    schedule_minute: int = 0,
    schedule_day: int = 0,
    project_dir: str = "",
    model: str = "",
    run_if_missed: bool = True,
) -> str:
    """Create a scheduled background task.

    Args:
        name: Short human-readable name for the task (e.g. "Check PR reviews")
        prompt: The instruction that will be sent to the execution session
        schedule: "hourly", "daily", "weekly", or "once"
        priority: "normal", "high", or "urgent". Urgent tasks interrupt the user immediately on completion
        schedule_hour: Hour of day (0-23) for daily/weekly tasks. Default: 9
        schedule_minute: Minute within the hour (0-59). Default: 0
        schedule_day: Day of week for weekly tasks (0=Monday, 6=Sunday). Default: 0
        project_dir: Working directory for the execution session. If set, the session runs there and picks up the project's CLAUDE.md
        model: Model override for this task (e.g. "claude-haiku-4-5-20251001"). Empty = server default
        run_if_missed: If true, execute immediately on server startup if a scheduled run was missed. Default: true
    """
    body = {
        "name": name,
        "prompt": prompt,
        "schedule": schedule,
        "priority": priority,
        "schedule_hour": schedule_hour,
        "schedule_minute": schedule_minute,
        "schedule_day": schedule_day,
        "project_dir": project_dir,
        "model": model,
        "run_if_missed": run_if_missed,
    }
    result = await _post("/ring0/tasks", body)
    if "error" in result:
        return f"Error: {result['error']}"
    task_id = result.get("id", "?")
    next_run = result.get("next_run_at", 0)
    from datetime import datetime
    next_str = datetime.fromtimestamp(next_run).strftime("%Y-%m-%d %H:%M") if next_run else "unknown"
    return f"Created task '{name}' ({task_id}). Schedule: {schedule}. Next run: {next_str}."


@mcp.tool()
async def list_tasks() -> str:
    """List all scheduled tasks with their status and next run time."""
    tasks = await _get("/ring0/tasks")
    if isinstance(tasks, dict) and "error" in tasks:
        return f"Error: {tasks['error']}"
    if not tasks:
        return "No scheduled tasks."

    from datetime import datetime
    lines = []
    for t in tasks:
        status = "enabled" if t.get("enabled") else "disabled"
        next_run = t.get("next_run_at", 0)
        next_str = datetime.fromtimestamp(next_run).strftime("%Y-%m-%d %H:%M") if next_run else "—"
        last_run = t.get("last_run_at", 0)
        last_str = datetime.fromtimestamp(last_run).strftime("%Y-%m-%d %H:%M") if last_run else "never"
        lines.append(
            f"• {t['name']} ({t['id']}) — {t['schedule']}, {t['priority']} priority, "
            f"{status}. Next: {next_str}. Last: {last_str}."
        )
    return "\n".join(lines)


@mcp.tool()
async def update_task(
    task_id: str,
    enabled: bool | None = None,
    schedule: str = "",
    priority: str = "",
    prompt: str = "",
    name: str = "",
    schedule_hour: int = -1,
    schedule_minute: int = -1,
    schedule_day: int = -1,
    project_dir: str | None = None,
    model: str | None = None,
) -> str:
    """Update a scheduled task's properties.

    Args:
        task_id: The task ID to update
        enabled: Enable or disable the task
        schedule: New schedule ("hourly", "daily", "weekly", "once")
        priority: New priority ("normal", "high", "urgent")
        prompt: New task prompt/instruction
        name: New task name
        schedule_hour: New hour (0-23) for daily/weekly
        schedule_minute: New minute (0-59)
        schedule_day: New day of week (0=Mon..6=Sun) for weekly
        project_dir: New working directory
        model: New model override
    """
    body: dict[str, Any] = {}
    if enabled is not None:
        body["enabled"] = enabled
    if schedule:
        body["schedule"] = schedule
    if priority:
        body["priority"] = priority
    if prompt:
        body["prompt"] = prompt
    if name:
        body["name"] = name
    if schedule_hour >= 0:
        body["schedule_hour"] = schedule_hour
    if schedule_minute >= 0:
        body["schedule_minute"] = schedule_minute
    if schedule_day >= 0:
        body["schedule_day"] = schedule_day
    if project_dir is not None:
        body["project_dir"] = project_dir
    if model is not None:
        body["model"] = model

    if not body:
        return "No updates specified."

    result = await _put(f"/ring0/tasks/{task_id}", body)
    if "error" in result:
        return f"Error: {result['error']}"
    return f"Updated task {task_id}."


@mcp.tool()
async def delete_task(task_id: str) -> str:
    """Delete a scheduled task permanently.

    Args:
        task_id: The task ID to delete
    """
    result = await _delete(f"/ring0/tasks/{task_id}")
    if "error" in result:
        return f"Error: {result['error']}"
    return f"Deleted task {task_id}."


@mcp.tool()
async def run_task(task_id: str) -> str:
    """Execute a scheduled task immediately, regardless of its schedule.

    The task runs in the background. Results will appear in the review queue
    when complete (usually within a few minutes).

    Args:
        task_id: The task ID to execute now
    """
    result = await _post(f"/ring0/tasks/{task_id}/run")
    if "error" in result:
        return f"Error: {result['error']}"
    return f"Task execution started. Results will appear in the review queue when complete."


@mcp.tool()
async def list_queue(status: str = "pending") -> str:
    """List results in the review queue.

    Args:
        status: Filter — "pending" (unreviewed), "reviewed", or "all". Default: "pending"
    """
    results = await _get(f"/ring0/queue?status={status}")
    if isinstance(results, dict) and "error" in results:
        return f"Error: {results['error']}"
    if not results:
        return f"No {status} results in the queue."

    from datetime import datetime
    lines = []
    for r in results:
        created = datetime.fromtimestamp(r.get("created_at", 0)).strftime("%Y-%m-%d %H:%M")
        rollup = f" ({r['run_count']} runs)" if r.get("run_count", 1) > 1 else ""
        status_icon = "✓" if r.get("status") == "completed" else "✗"
        # First line of output as a brief summary
        output = r.get("output", "")
        summary = output.split("\n")[0][:120] if output else "(no output)"
        lines.append(
            f"• [{r['priority']}] {r['task_name']} ({r['id']}) — {status_icon} {r['status']}{rollup}, "
            f"{created}. {summary}"
        )
    return "\n".join(lines)


@mcp.tool()
async def get_queue_item(result_id: str) -> str:
    """Get full details of a queue item, including the complete output from the task execution.

    Args:
        result_id: The result ID to retrieve
    """
    result = await _get(f"/ring0/queue/{result_id}")
    if isinstance(result, dict) and "error" in result:
        return f"Error: {result['error']}"

    from datetime import datetime
    created = datetime.fromtimestamp(result.get("created_at", 0)).strftime("%Y-%m-%d %H:%M")
    output = result.get("output", "(no output)")
    rollup = f"\nThis result accumulated from {result['run_count']} runs." if result.get("run_count", 1) > 1 else ""
    cost = result.get("execution_cost_usd", 0)
    cost_str = f"${cost:.4f}" if cost else "—"

    return (
        f"Task: {result['task_name']} ({result['task_id']})\n"
        f"Status: {result['status']} | Priority: {result['priority']} | Cost: {cost_str}\n"
        f"Completed: {created}{rollup}\n"
        f"\n--- Output ---\n\n{output}"
    )


@mcp.tool()
async def review_queue_item(result_id: str, action: str) -> str:
    """Mark a queue item as reviewed with a disposition.

    Args:
        result_id: The result ID to review
        action: Disposition — "done" (handled), "defer" (come back later),
                "delegate" (hand off to someone/something), or "followup" (needs follow-up action)
    """
    result = await _post(f"/ring0/queue/{result_id}/review", {"action": action})
    if "error" in result:
        return f"Error: {result['error']}"
    return f"Marked as '{action}'."


if __name__ == "__main__":
    mcp.run(transport="stdio")
