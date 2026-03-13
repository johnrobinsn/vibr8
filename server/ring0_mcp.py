"""Ring0 MCP Server — stdio MCP server exposing vibr8 session tools.

Launched as a subprocess by Claude CLI with --mcp-config.
Communicates with vibr8's REST API via localhost.
"""

import os
import json
import logging
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
        client_id: Optional client ID to target. If provided, only that browser instance switches.
                   If omitted, all connected browsers switch.
    """
    body: dict[str, str] = {"sessionId": session_id}
    if client_id:
        body["clientId"] = client_id
    result = await _post("/ring0/switch-ui", body)
    if result.get("error"):
        return f"Error: {result['error']}"
    target = f"client {client_id[:8]}" if client_id else "all clients"
    return f"Switched {target} to session {session_id[:8]}."


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

    lines = []
    for msg in messages[-10:]:  # Last 10 messages
        role = msg.get("type", "?")
        if role == "user_message":
            content = msg.get("content", "")
            lines.append(f"User: {content}")
        elif role == "assistant":
            content = msg.get("message", "")
            if isinstance(content, dict):
                content = content.get("content", "")
            if isinstance(content, list):
                texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                content = " ".join(texts)
            if isinstance(content, str):
                lines.append(f"Assistant: {content[:500]}")
        elif role == "result":
            data = msg.get("data", {})
            if data.get("is_error"):
                lines.append(f"Error: {', '.join(data.get('errors', []))}")

    if permissions:
        lines.append("")
        lines.append("--- PENDING PERMISSIONS (session is blocked, waiting for response) ---")
        for perm in permissions:
            rid = perm.get("request_id", "?")[:8]
            tool = perm.get("tool_name", "?")
            desc = perm.get("description", "")
            inp = json.dumps(perm.get("input", {}))[:300]
            lines.append(f"  [{rid}] {tool}: {desc or inp}")

    return "\n".join(lines) if lines else "No readable messages."


@mcp.tool()
async def get_active_clients() -> str:
    """List all connected browser clients and which sessions they are connected to.

    Returns a mapping of client IDs to their WebSocket session IDs.
    """
    clients = await _get("/ring0/clients")
    if not clients:
        return "No clients connected."
    lines = []
    for client_id, info in clients.items():
        if isinstance(info, dict):
            sid = info.get("sessionId", "?")
            role = info.get("role", "primary")
            lines.append(f"- client={client_id[:8]}... → session={sid[:8]}... role={role}")
        else:
            lines.append(f"- client={client_id[:8]}... → session={info[:8]}...")
    return "\n".join(lines)


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
        client_id: The client ID to query.
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
        params: Optional JSON string of parameters to pass to the method (e.g., '{"title": "Hello", "body": "World"}').
    """
    body: dict[str, Any] = {"clientId": client_id, "method": method}
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


if __name__ == "__main__":
    mcp.run(transport="stdio")
