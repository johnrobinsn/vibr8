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
        r.raise_for_status()
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
        lines.append(f"- {name} (id={sid[:8]}, state={state}, type={backend}, cwd={cwd})")
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
    if not messages:
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
    for client_id, session_id in clients.items():
        lines.append(f"- client={client_id[:8]}... → session={session_id[:8]}...")
    return "\n".join(lines)


@mcp.tool()
async def query_client(client_id: str, method: str) -> str:
    """Send an RPC query to a specific browser client and get their response.

    Args:
        client_id: The client ID to query.
        method: The RPC method to call (e.g., "get_state" returns currentSessionId, url, timestamp).
    """
    result = await _post("/ring0/query-client", {"clientId": client_id, "method": method})
    if result.get("error"):
        return f"Error: {result['error']}"
    return json.dumps(result.get("result", {}), indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
