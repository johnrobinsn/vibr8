"""REST API routes — mirrors the Hono routes from the TypeScript version."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform as plat
import secrets
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from server import env_manager, git_utils, session_names
from server import voice_profiles, voice_logger
from server.usage_limits import get_usage_limits
from server.cli_launcher import CliLauncher, LaunchOptions, WorktreeInfo
from server.worktree_tracker import WorktreeTracker, WorktreeMapping

if TYPE_CHECKING:
    from server.ws_bridge import WsBridge
    from server.session_store import SessionStore
    from server.webrtc import WebRTCManager
    from server.terminal import TerminalManager
    from server.auth import AuthManager

from server.ring0 import Ring0Manager

logger = logging.getLogger(__name__)


def _extract_assistant_text(message: Any) -> tuple[str, bool]:
    """Extract readable text from an assistant message content.

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


def _format_session_output(messages: list[dict], permissions: list[dict]) -> str:
    """Format session messages into readable text for Ring0. No per-message truncation."""
    if not messages and not permissions:
        return "No messages in this session yet."

    lines: list[str] = []
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
    # Keep last 30 entries
    lines = lines[-30:]
    # Guard total size — drop oldest if over 50k chars
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
            inp = json.dumps(perm.get("input", {}))[:300]
            lines.append(f"  [{rid}] {tool}: {desc or inp}")

    return "\n".join(lines) if lines else "No readable messages."


def _to_camel(name: str) -> str:
    """Convert snake_case to camelCase."""
    parts = name.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _camel_dict(obj: Any) -> dict[str, Any]:
    """Convert a dataclass or dict with snake_case keys to camelCase."""
    d = obj if isinstance(obj, dict) else obj.__dict__
    return {_to_camel(k): v for k, v in d.items()}


def create_routes(
    launcher: CliLauncher,
    ws_bridge: WsBridge,
    session_store: SessionStore,
    worktree_tracker: WorktreeTracker,
    webrtc_manager: WebRTCManager | None = None,
    terminal_manager: TerminalManager | None = None,
    auth_manager: AuthManager | None = None,
    ring0_manager: Ring0Manager | None = None,
    node_registry: Any | None = None,
) -> web.RouteTableDef:
    routes = web.RouteTableDef()

    async def _forward_to_remote(session_id: str, command_type: str, extra: dict | None = None) -> web.Response | None:
        """If session_id belongs to a remote node, forward the command via tunnel.
        Returns a Response if forwarded, or None if the session is local.
        Strips the node prefix so the remote node receives its raw session ID."""
        node_id = ws_bridge.get_session_node_id(session_id)
        if not node_id or not ws_bridge._is_remote_session(session_id):
            return None
        if not node_registry:
            return web.json_response({"error": "Node registry unavailable"}, status=503)
        node = node_registry.get_node(node_id)
        if not node or not node.tunnel or not node.tunnel.connected:
            return web.json_response({"error": "Remote node unavailable"}, status=503)
        raw_id = ws_bridge._raw_session_id(session_id)
        cmd: dict[str, Any] = {"type": command_type, "sessionId": raw_id}
        if extra:
            cmd.update(extra)
        result = await node.tunnel.send_command(cmd)
        if result.get("error"):
            return web.json_response({"error": result["error"]}, status=500)
        return web.json_response(result)

    # ── Auth ─────────────────────────────────────────────────────────────

    @routes.post("/api/auth/login")
    async def auth_login(request: web.Request) -> web.Response:
        if not auth_manager or not auth_manager.enabled:
            return web.json_response({"error": "Auth not configured"}, status=501)
        body = await request.json()
        username = body.get("username", "")
        password = body.get("password", "")
        if not auth_manager.verify(username, password):
            return web.json_response({"error": "Invalid credentials"}, status=401)
        token = auth_manager.create_session(username)
        resp = web.json_response({"ok": True, "username": username})
        resp.set_cookie(
            "vibr8_session",
            token,
            httponly=True,
            samesite="Lax",
            secure=request.secure,
            max_age=30 * 86400,
            path="/",
        )
        return resp

    @routes.post("/api/auth/logout")
    async def auth_logout(request: web.Request) -> web.Response:
        token = request.cookies.get("vibr8_session")
        if token and auth_manager:
            auth_manager.revoke_session(token)
        resp = web.json_response({"ok": True})
        resp.del_cookie("vibr8_session", path="/")
        return resp

    @routes.get("/api/auth/me")
    async def auth_me(request: web.Request) -> web.Response:
        if not auth_manager or not auth_manager.enabled:
            return web.json_response({"authEnabled": False})
        # This route is public (skips middleware), so validate cookie directly
        username = None
        token = request.cookies.get("vibr8_session")
        if token:
            username = auth_manager.validate_session(token)
        return web.json_response({
            "authEnabled": True,
            "authenticated": username is not None,
            "username": username,
        })

    # ── Device tokens ────────────────────────────────────────────────────

    @routes.post("/api/auth/device-token")
    async def create_device_token(request: web.Request) -> web.Response:
        if not auth_manager or not auth_manager.enabled:
            return web.json_response({"error": "Auth not configured"}, status=501)
        username = request.get("auth_user")
        if not username:
            return web.json_response({"error": "Unauthorized"}, status=401)
        body = await request.json()
        name = body.get("name", "").strip()
        if not name:
            return web.json_response({"error": "name is required"}, status=400)
        result = auth_manager.create_device_token(username, name)
        return web.json_response(result)

    @routes.get("/api/auth/device-tokens")
    async def list_device_tokens(request: web.Request) -> web.Response:
        if not auth_manager or not auth_manager.enabled:
            return web.json_response({"error": "Auth not configured"}, status=501)
        username = request.get("auth_user")
        if not username:
            return web.json_response({"error": "Unauthorized"}, status=401)
        tokens = auth_manager.list_device_tokens(username)
        return web.json_response({"tokens": tokens})

    @routes.delete("/api/auth/device-tokens/{token_id}")
    async def delete_device_token(request: web.Request) -> web.Response:
        if not auth_manager or not auth_manager.enabled:
            return web.json_response({"error": "Auth not configured"}, status=501)
        username = request.get("auth_user")
        if not username:
            return web.json_response({"error": "Unauthorized"}, status=401)
        token_id = request.match_info["token_id"]
        if auth_manager.revoke_device_token(username, token_id):
            return web.json_response({"ok": True})
        return web.json_response({"error": "Token not found"}, status=404)

    # ── Unified device pairing ───────────────────────────────────────

    @routes.post("/api/pairing/request")
    async def pairing_request(request: web.Request) -> web.Response:
        """Public — device requests a pairing code to display."""
        if not auth_manager or not auth_manager.enabled:
            return web.json_response({"error": "Auth not configured"}, status=501)
        ip = request.remote or "unknown"
        if auth_manager.check_pairing_rate_limit(ip):
            return web.json_response({"error": "Too many requests"}, status=429)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        device_type = body.get("type", "native")
        if device_type not in ("native", "second-screen"):
            return web.json_response({"error": "type must be 'native' or 'second-screen'"}, status=400)
        client_id = body.get("clientId", "")
        if device_type == "second-screen" and not client_id:
            return web.json_response({"error": "clientId required for second-screen"}, status=400)
        result = auth_manager.request_pairing(device_type, ip, client_id)
        return web.json_response(result)

    @routes.post("/api/pairing/confirm")
    async def pairing_confirm(request: web.Request) -> web.Response:
        """Authenticated — user confirms the code shown on their device."""
        if not auth_manager or not auth_manager.enabled:
            return web.json_response({"error": "Auth not configured"}, status=501)
        username = request.get("auth_user")
        if not username:
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        code = body.get("code", "").strip()
        name = body.get("name", "").strip()
        if not code or not name:
            return web.json_response({"error": "code and name are required"}, status=400)
        result = auth_manager.confirm_pairing(code, username, name)
        if not result:
            return web.json_response({"error": "Invalid or expired code"}, status=400)
        # Handle second-screen registration
        if result["type"] == "second-screen":
            client_id = result["clientId"]
            _second_screen_pairings[client_id] = {
                "pairedUser": username,
                "pairedAt": time.time(),
                "enabled": True,
            }
            _save_pairings()
            # Push notification to second screen via WebSocket
            ws = ws_bridge._ws_by_client.get(client_id)
            if ws and not ws.closed:
                await ws.send_str(json.dumps({
                    "type": "second_screen_paired",
                    "pairedUser": username,
                }))
            from server.ring0_events import Ring0Event
            await ws_bridge.emit_ring0_event(Ring0Event(fields={
                "type": "second_screen_paired",
                "clientId": client_id[:8], "user": username,
            }))
            logger.info("[pairing] Paired second-screen %s → user=%s", client_id[:8], username)
        else:
            logger.info("[pairing] Paired native device '%s' → user=%s", name, username)
        return web.json_response({"ok": True, "type": result["type"]})

    @routes.get("/api/pairing/status/{code}")
    async def pairing_status_unified(request: web.Request) -> web.Response:
        """Public — device polls to check if pairing was confirmed."""
        if not auth_manager or not auth_manager.enabled:
            return web.json_response({"error": "Auth not configured"}, status=501)
        ip = request.remote or "unknown"
        if auth_manager.check_pairing_brute_force(ip):
            return web.json_response({"error": "Too many requests"}, status=429)
        code = request.match_info["code"]
        status = auth_manager.get_pairing_status(code, ip)
        return web.json_response(status)

    # ── Admin ─────────────────────────────────────────────────────────────

    @routes.post("/api/admin/restart")
    async def admin_restart(request: web.Request) -> web.Response:
        if os.environ.get("NODE_ENV") == "production":
            return web.json_response({"error": "Not available in production"}, status=403)
        logger.info("[routes] Server restart requested via API")
        # Access restart function from app — avoids __main__ vs server.main dual-import issue
        do_restart = request.app.get("request_restart")
        if not do_restart:
            return web.json_response({"error": "Restart not available"}, status=500)
        asyncio.get_event_loop().call_later(0.2, do_restart)
        return web.json_response({"ok": True, "message": "Restarting..."})

    # ── SDK Sessions ─────────────────────────────────────────────────────

    @routes.post("/api/sessions/create")
    async def create_session(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}

        # Remote node — forward create request via tunnel
        # (computer-use sessions always run on the hub, targeting a remote display)
        target_node = body.get("nodeId", "")
        backend = body.get("backend", "claude")
        if target_node and node_registry and backend != "computer-use":
            node = node_registry.get_node(target_node)
            if node and node.tunnel and node.tunnel.connected:
                result = await node.tunnel.send_command({
                    "type": "create_session",
                    "options": {k: v for k, v in body.items() if k != "nodeId"},
                })
                if result.get("error"):
                    return web.json_response({"error": result["error"]}, status=500)
                # Qualify the returned session ID at the hub boundary
                raw_sid = result.get("sessionId")
                if raw_sid:
                    from server.ws_bridge import WsBridge
                    result["sessionId"] = WsBridge.qualify_session_id(target_node, raw_sid)
                return web.json_response(result)

        try:
            if backend not in ("claude", "codex", "terminal", "computer-use"):
                return web.json_response({"error": f"Invalid backend: {backend}"}, status=400)

            if backend == "terminal":
                if not terminal_manager:
                    return web.json_response({"error": "Terminal not available"}, status=501)
                import uuid, time
                session_id = str(uuid.uuid4())
                cwd = body.get("cwd") or os.getcwd()
                terminal_manager.create(session_id, cwd=cwd)
                name = session_names.generate_random_name()
                session_names.set_name(session_id, name)
                return web.json_response({
                    "sessionId": session_id,
                    "state": "connected",
                    "cwd": cwd,
                    "backendType": "terminal",
                    "createdAt": time.time() * 1000,
                    "name": name,
                })

            if backend == "computer-use":
                # Computer-use is launched via CliLauncher (no subprocess, callback creates agent)
                # nodeId tells the agent which node's desktop to target
                opts = LaunchOptions(
                    model=body.get("model"),
                    cwd=body.get("cwd") or os.getcwd(),
                    backendType="computer-use",
                    nodeId=target_node or None,
                )
                session = launcher.launch(opts)
                name = body.get("name") or session_names.generate_random_name()
                session_names.set_name(session.sessionId, name)
                result = session.to_dict()
                result["name"] = name
                return web.json_response(result)

            # Resolve environment variables
            env_vars: dict[str, str] | None = body.get("env")
            env_slug = body.get("envSlug")
            if env_slug:
                resolved_env = env_manager.get_env(env_slug)
                if resolved_env:
                    logger.info(f"[routes] Injecting env \"{resolved_env.name}\" ({len(resolved_env.variables)} vars)")
                    env_vars = {**resolved_env.variables, **(body.get("env") or {})}
                else:
                    logger.warning(f"[routes] Environment \"{env_slug}\" not found, ignoring")

            cwd = body.get("cwd")
            worktree_info = None

            if body.get("useWorktree") and body.get("branch") and cwd:
                repo_info = git_utils.get_repo_info(cwd)
                if repo_info:
                    result = git_utils.ensure_worktree(
                        repo_info.repo_root,
                        body["branch"],
                        base_branch=repo_info.default_branch,
                        create_branch=body.get("createBranch"),
                        force_new=True,
                    )
                    cwd = result.worktree_path
                    worktree_info = {
                        "isWorktree": True,
                        "repoRoot": repo_info.repo_root,
                        "branch": body["branch"],
                        "actualBranch": result.actual_branch,
                        "worktreePath": result.worktree_path,
                    }
            elif body.get("branch") and cwd:
                repo_info = git_utils.get_repo_info(cwd)
                if repo_info and repo_info.current_branch != body["branch"]:
                    git_utils.checkout_branch(repo_info.repo_root, body["branch"])

            wt_info = None
            if worktree_info:
                wt_info = WorktreeInfo(
                    isWorktree=worktree_info["isWorktree"],
                    repoRoot=worktree_info["repoRoot"],
                    branch=worktree_info["branch"],
                    actualBranch=worktree_info["actualBranch"],
                    worktreePath=worktree_info["worktreePath"],
                )

            opts = LaunchOptions(
                model=body.get("model"),
                permissionMode=body.get("permissionMode"),
                cwd=cwd,
                claudeBinary=body.get("claudeBinary"),
                codexBinary=body.get("codexBinary"),
                allowedTools=body.get("allowedTools"),
                env=env_vars,
                backendType=backend,
                worktreeInfo=wt_info,
            )
            session = launcher.launch(opts)

            session_dict = session.to_dict()

            if worktree_info:
                worktree_tracker.add_mapping(WorktreeMapping(
                    sessionId=session.sessionId,
                    repoRoot=worktree_info["repoRoot"],
                    branch=worktree_info["branch"],
                    worktreePath=worktree_info["worktreePath"],
                    createdAt=session.createdAt,
                    actualBranch=worktree_info["actualBranch"],
                ))

            return web.json_response(session_dict)
        except Exception as e:
            logger.exception(f"[routes] Failed to create session: {e}")
            return web.json_response({"error": str(e)}, status=500)

    @routes.get("/api/sessions")
    async def list_sessions(request: web.Request) -> web.Response:
        requested_node = request.rel_url.query.get("nodeId", "")

        # Remote node — fetch sessions via tunnel
        if requested_node and node_registry:
            node = node_registry.get_node(requested_node)
            if node and node.tunnel and node.tunnel.connected:
                result = await node.tunnel.send_command({"type": "list_sessions"})
                remote_sessions = result.get("sessions", [])
                # Qualify session IDs at the hub boundary
                from server.ws_bridge import WsBridge
                for s in remote_sessions:
                    raw_id = s.get("sessionId", "")
                    if raw_id:
                        s["sessionId"] = WsBridge.qualify_session_id(requested_node, raw_id)
                # Sort: Ring0 pinned first, then MRU
                remote_sessions.sort(
                    key=lambda s: (
                        0 if s.get("isRing0") else 1,
                        -(s.get("lastPromptedAt") or s.get("createdAt") or 0),
                    )
                )
                return web.json_response(remote_sessions)
            elif node and node.tunnel is None:
                pass  # Local node — fall through to local handling
            else:
                return web.json_response([])  # Offline remote node

        # Local node — existing logic
        sessions = launcher.list_sessions()
        names = session_names.get_all_names()
        r0_id = ring0_manager.session_id if ring0_manager else None
        enriched = []
        for s in sessions:
            s_dict = s.to_dict() if hasattr(s, "to_dict") else (s if isinstance(s, dict) else s.__dict__)
            sid = s_dict.get("sessionId", "")
            s_dict["name"] = names.get(sid, s_dict.get("name"))
            # Enrich with MRU timestamp from WsBridge
            lpa = ws_bridge.get_last_prompted_at(sid)
            if lpa:
                s_dict["lastPromptedAt"] = lpa
            if r0_id and sid == r0_id:
                s_dict["isRing0"] = True
            # Enrich with agent runtime state from WsBridge
            bridge_session = ws_bridge._sessions.get(sid)
            if bridge_session:
                s_dict["agentState"] = ws_bridge._derive_agent_status(bridge_session)
            enriched.append(s_dict)
        # Include terminal sessions
        if terminal_manager:
            import time
            for sid in terminal_manager.get_all_ids():
                term = terminal_manager.get(sid)
                if term:
                    enriched.append({
                        "sessionId": sid,
                        "state": "connected",
                        "cwd": term._cwd,
                        "backendType": "terminal",
                        "name": names.get(sid),
                        "createdAt": time.time() * 1000,
                    })
        # Sort: Ring0 pinned first, then MRU (fallback to createdAt)
        enriched.sort(
            key=lambda s: (
                0 if s.get("sessionId") == r0_id else 1,
                -(s.get("lastPromptedAt") or s.get("createdAt") or 0),
            )
        )
        return web.json_response(enriched)

    @routes.get("/api/sessions/{id}")
    async def get_session(request: web.Request) -> web.Response:
        sid = request.match_info["id"]
        session = launcher.get_session(sid)
        if not session:
            return web.json_response({"error": "Session not found"}, status=404)
        return web.json_response(session.to_dict() if hasattr(session, "to_dict") else session)

    @routes.patch("/api/sessions/{id}/name")
    async def rename_session(request: web.Request) -> web.Response:
        sid = request.match_info["id"]
        try:
            body = await request.json()
        except Exception:
            body = {}
        name = body.get("name", "").strip()
        if not name:
            return web.json_response({"error": "name is required"}, status=400)
        remote = await _forward_to_remote(sid, "rename_session", {"name": name})
        if remote:
            return remote
        if ring0_manager and ring0_manager.session_id == sid:
            return web.json_response({"error": "Ring0 session cannot be renamed"}, status=403)
        session_names.set_name(sid, name, unique=False)
        # Broadcast to all browsers so other tabs/devices see the rename
        await ws_bridge.broadcast_name_update(sid, name, user_renamed=True)
        return web.json_response({"ok": True, "name": name})

    @routes.post("/api/sessions/{id}/kill")
    async def kill_session(request: web.Request) -> web.Response:
        sid = request.match_info["id"]
        remote = await _forward_to_remote(sid, "kill_session")
        if remote:
            return remote
        killed = await launcher.kill(sid)
        if not killed:
            return web.json_response({"error": "Session not found or already exited"}, status=404)
        return web.json_response({"ok": True})

    @routes.post("/api/sessions/{id}/relaunch")
    async def relaunch_session(request: web.Request) -> web.Response:
        sid = request.match_info["id"]
        remote = await _forward_to_remote(sid, "relaunch_session")
        if remote:
            return remote
        ok = await launcher.relaunch(sid)
        if not ok:
            return web.json_response({"error": "Session not found"}, status=404)
        return web.json_response({"ok": True})

    @routes.delete("/api/sessions/{id}")
    async def delete_session(request: web.Request) -> web.Response:
        sid = request.match_info["id"]
        remote = await _forward_to_remote(sid, "delete_session")
        if remote:
            await ws_bridge.close_session(sid)
            return remote
        # Close terminal session if it exists
        if terminal_manager:
            terminal_manager.close(sid)
        await launcher.kill(sid)
        worktree_result = _cleanup_worktree(sid, worktree_tracker, force=True)
        launcher.remove_session(sid)
        await ws_bridge.close_session(sid)
        return web.json_response({"ok": True, "worktree": worktree_result})

    @routes.post("/api/sessions/{id}/archive")
    async def archive_session(request: web.Request) -> web.Response:
        sid = request.match_info["id"]
        try:
            body = await request.json()
        except Exception:
            body = {}
        remote = await _forward_to_remote(sid, "archive_session", {"force": body.get("force", False)})
        if remote:
            return remote
        await launcher.kill(sid)
        worktree_result = _cleanup_worktree(sid, worktree_tracker, force=body.get("force"))
        launcher.set_archived(sid, True)
        session_store.set_archived(sid, True)
        return web.json_response({"ok": True, "worktree": worktree_result})

    @routes.post("/api/sessions/{id}/unarchive")
    async def unarchive_session(request: web.Request) -> web.Response:
        sid = request.match_info["id"]
        remote = await _forward_to_remote(sid, "unarchive_session")
        if remote:
            return remote
        launcher.set_archived(sid, False)
        session_store.set_archived(sid, False)
        return web.json_response({"ok": True})

    # ── Message history archive ──────────────────────────────────────────

    @routes.get("/api/sessions/{id}/history-archive")
    async def get_session_history_archive(request: web.Request) -> web.Response:
        """Retrieve archived (rolled-off) messages with optional date filter and pagination."""
        sid = request.match_info["id"]
        date = request.query.get("date")
        offset = int(request.query.get("offset", "0"))
        limit = int(request.query.get("limit", "100"))
        messages, total = session_store.load_archive(sid, date=date, offset=offset, limit=limit)
        return web.json_response({
            "messages": messages,
            "total": total,
            "offset": offset,
            "limit": limit,
        })

    @routes.get("/api/sessions/{id}/history-archive/dates")
    async def get_session_history_archive_dates(request: web.Request) -> web.Response:
        """List available archive dates with message counts and sizes."""
        sid = request.match_info["id"]
        dates = session_store.list_archive_dates(sid)
        return web.json_response({"dates": dates})

    # ── Backends ─────────────────────────────────────────────────────────

    @routes.get("/api/backends")
    async def list_backends(request: web.Request) -> web.Response:
        import shutil
        backends = []
        backends.append({"id": "claude", "name": "Claude Code", "available": shutil.which("claude") is not None})
        backends.append({"id": "codex", "name": "Codex", "available": shutil.which("codex") is not None})
        backends.append({"id": "computer-use", "name": "Computer Use", "available": True})
        backends.append({"id": "terminal", "name": "Terminal", "available": True})
        return web.json_response(backends)

    @routes.get("/api/backends/{id}/models")
    async def list_models(request: web.Request) -> web.Response:
        backend_id = request.match_info["id"]
        if backend_id == "codex":
            cache_path = Path.home() / ".codex" / "models_cache.json"
            if not cache_path.exists():
                return web.json_response({"error": "Codex models cache not found"}, status=404)
            try:
                cache = json.loads(cache_path.read_text())
                models = sorted(
                    [m for m in cache.get("models", []) if m.get("visibility") == "list"],
                    key=lambda m: m.get("priority", 99),
                )
                result = [{"value": m["slug"], "label": m.get("display_name", m["slug"]), "description": m.get("description", "")} for m in models]
                return web.json_response(result)
            except Exception:
                return web.json_response({"error": "Failed to parse Codex models cache"}, status=500)
        return web.json_response({"error": "Use frontend defaults for this backend"}, status=404)

    # ── Filesystem browsing ──────────────────────────────────────────────

    @routes.get("/api/fs/list")
    async def fs_list(request: web.Request) -> web.Response:
        raw_path = request.query.get("path") or str(Path.home())
        base = Path(raw_path).resolve()
        try:
            dirs = []
            for entry in sorted(base.iterdir(), key=lambda e: e.name):
                if entry.is_dir() and not entry.name.startswith("."):
                    dirs.append({"name": entry.name, "path": str(entry)})
            return web.json_response({"path": str(base), "dirs": dirs, "home": str(Path.home())})
        except Exception:
            return web.json_response({"error": "Cannot read directory", "path": str(base), "dirs": [], "home": str(Path.home())}, status=400)

    @routes.get("/api/fs/home")
    async def fs_home(request: web.Request) -> web.Response:
        return web.json_response({"home": str(Path.home()), "cwd": os.getcwd()})

    @routes.get("/api/fs/tree")
    async def fs_tree(request: web.Request) -> web.Response:
        raw_path = request.query.get("path")
        if not raw_path:
            return web.json_response({"error": "path required"}, status=400)
        base = Path(raw_path).resolve()

        def build_tree(d: Path, depth: int) -> list[dict[str, Any]]:
            if depth > 10:
                return []
            try:
                nodes: list[dict[str, Any]] = []
                for entry in sorted(d.iterdir(), key=lambda e: (not e.is_dir(), e.name)):
                    if entry.name.startswith(".") or entry.name == "node_modules":
                        continue
                    if entry.is_dir():
                        nodes.append({"name": entry.name, "path": str(entry), "type": "directory", "children": build_tree(entry, depth + 1)})
                    elif entry.is_file():
                        nodes.append({"name": entry.name, "path": str(entry), "type": "file"})
                return nodes
            except Exception:
                return []

        tree = build_tree(base, 0)
        return web.json_response({"path": str(base), "tree": tree})

    @routes.get("/api/fs/read")
    async def fs_read(request: web.Request) -> web.Response:
        file_path = request.query.get("path")
        if not file_path:
            return web.json_response({"error": "path required"}, status=400)
        p = Path(file_path).resolve()
        try:
            if p.stat().st_size > 2 * 1024 * 1024:
                return web.json_response({"error": "File too large (>2MB)"}, status=413)
            content = p.read_text()
            return web.json_response({"path": str(p), "content": content})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=404)

    @routes.get("/api/fs/raw")
    async def fs_raw(request: web.Request) -> web.Response:
        """Serve a file with its native MIME type (for images, etc.)."""
        file_path = request.query.get("path")
        if not file_path:
            return web.json_response({"error": "path required"}, status=400)
        p = Path(file_path).resolve()
        if not p.is_file():
            return web.json_response({"error": "File not found"}, status=404)
        try:
            if p.stat().st_size > 20 * 1024 * 1024:
                return web.json_response({"error": "File too large (>20MB)"}, status=413)
            return web.FileResponse(p)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    @routes.put("/api/fs/write")
    async def fs_write(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        file_path = body.get("path")
        content = body.get("content")
        if not file_path or not isinstance(content, str):
            return web.json_response({"error": "path and content required"}, status=400)
        p = Path(file_path).resolve()
        try:
            p.write_text(content)
            return web.json_response({"ok": True, "path": str(p)})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    @routes.post("/api/fs/mkdir")
    async def fs_mkdir(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        dir_path = body.get("path")
        if not dir_path:
            return web.json_response({"error": "path required"}, status=400)
        p = Path(dir_path).resolve()
        try:
            p.mkdir(parents=True, exist_ok=True)
            return web.json_response({"ok": True, "path": str(p)})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    @routes.post("/api/fs/rename")
    async def fs_rename(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        old_path = body.get("oldPath")
        new_path = body.get("newPath")
        if not old_path or not new_path:
            return web.json_response({"error": "oldPath and newPath required"}, status=400)
        src = Path(old_path).resolve()
        dst = Path(new_path).resolve()
        if not src.exists():
            return web.json_response({"error": "source not found"}, status=404)
        if dst.exists():
            return web.json_response({"error": "destination already exists"}, status=409)
        try:
            src.rename(dst)
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    @routes.post("/api/fs/delete")
    async def fs_delete(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        target = body.get("path")
        if not target:
            return web.json_response({"error": "path required"}, status=400)
        p = Path(target).resolve()
        if not p.exists():
            return web.json_response({"error": "not found"}, status=404)
        try:
            if p.is_dir():
                import shutil
                shutil.rmtree(p)
            else:
                p.unlink()
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    @routes.get("/api/fs/diff")
    async def fs_diff(request: web.Request) -> web.Response:
        file_path = request.query.get("path")
        if not file_path:
            return web.json_response({"error": "path required"}, status=400)
        p = Path(file_path).resolve()
        try:
            result = subprocess.run(
                ["git", "diff", "HEAD", "--", str(p)],
                cwd=str(p.parent), capture_output=True, text=True, timeout=5,
            )
            return web.json_response({"path": str(p), "diff": result.stdout})
        except Exception:
            return web.json_response({"path": str(p), "diff": ""})

    # ── Environments ─────────────────────────────────────────────────────

    @routes.get("/api/envs")
    async def list_envs(request: web.Request) -> web.Response:
        try:
            envs = env_manager.list_envs()
            return web.json_response([e.to_dict() for e in envs])
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    @routes.get("/api/envs/{slug}")
    async def get_env(request: web.Request) -> web.Response:
        env = env_manager.get_env(request.match_info["slug"])
        if not env:
            return web.json_response({"error": "Environment not found"}, status=404)
        return web.json_response(env.to_dict())

    @routes.post("/api/envs")
    async def create_env(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            env = env_manager.create_env(body.get("name", ""), body.get("variables", {}))
            return web.json_response(env.to_dict(), status=201)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    @routes.put("/api/envs/{slug}")
    async def update_env(request: web.Request) -> web.Response:
        slug = request.match_info["slug"]
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            env = env_manager.update_env(slug, name=body.get("name"), variables=body.get("variables"))
            if not env:
                return web.json_response({"error": "Environment not found"}, status=404)
            return web.json_response(env.to_dict())
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    @routes.delete("/api/envs/{slug}")
    async def delete_env(request: web.Request) -> web.Response:
        deleted = env_manager.delete_env(request.match_info["slug"])
        if not deleted:
            return web.json_response({"error": "Environment not found"}, status=404)
        return web.json_response({"ok": True})

    # ── Git operations ───────────────────────────────────────────────────

    @routes.get("/api/git/repo-info")
    async def git_repo_info(request: web.Request) -> web.Response:
        path = request.query.get("path")
        if not path:
            return web.json_response({"error": "path required"}, status=400)
        info = git_utils.get_repo_info(path)
        if not info:
            return web.json_response({"error": "Not a git repository"}, status=400)
        return web.json_response(_camel_dict(info))

    @routes.get("/api/git/branches")
    async def git_branches(request: web.Request) -> web.Response:
        repo_root = request.query.get("repoRoot")
        if not repo_root:
            return web.json_response({"error": "repoRoot required"}, status=400)
        try:
            branches = git_utils.list_branches(repo_root)
            return web.json_response([_camel_dict(b) for b in branches])
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    @routes.get("/api/git/worktrees")
    async def git_worktrees(request: web.Request) -> web.Response:
        repo_root = request.query.get("repoRoot")
        if not repo_root:
            return web.json_response({"error": "repoRoot required"}, status=400)
        try:
            wts = git_utils.list_worktrees(repo_root)
            return web.json_response([_camel_dict(w) for w in wts])
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    @routes.post("/api/git/worktree")
    async def git_create_worktree(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        repo_root = body.get("repoRoot")
        branch = body.get("branch")
        if not repo_root or not branch:
            return web.json_response({"error": "repoRoot and branch required"}, status=400)
        try:
            result = git_utils.ensure_worktree(repo_root, branch, base_branch=body.get("baseBranch"), create_branch=body.get("createBranch"))
            return web.json_response(_camel_dict(result))
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    @routes.delete("/api/git/worktree")
    async def git_delete_worktree(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        repo_root = body.get("repoRoot")
        worktree_path = body.get("worktreePath")
        if not repo_root or not worktree_path:
            return web.json_response({"error": "repoRoot and worktreePath required"}, status=400)
        result = git_utils.remove_worktree(repo_root, worktree_path, force=body.get("force"))
        return web.json_response(result)

    @routes.post("/api/git/fetch")
    async def git_fetch(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        repo_root = body.get("repoRoot")
        if not repo_root:
            return web.json_response({"error": "repoRoot required"}, status=400)
        return web.json_response(git_utils.git_fetch(repo_root))

    @routes.post("/api/git/pull")
    async def git_pull(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        cwd = body.get("cwd")
        if not cwd:
            return web.json_response({"error": "cwd required"}, status=400)
        result = git_utils.git_pull(cwd)
        git_ahead = 0
        git_behind = 0
        try:
            counts = subprocess.run(
                ["git", "rev-list", "--left-right", "--count", "@{upstream}...HEAD"],
                cwd=cwd, capture_output=True, text=True, timeout=3,
            ).stdout.strip()
            parts = counts.split()
            if len(parts) == 2:
                git_behind = int(parts[0])
                git_ahead = int(parts[1])
        except Exception:
            pass
        return web.json_response({**result, "git_ahead": git_ahead, "git_behind": git_behind})

    # ── Usage Limits ─────────────────────────────────────────────────────

    @routes.get("/api/usage-limits")
    async def usage_limits(request: web.Request) -> web.Response:
        limits = await get_usage_limits()
        return web.json_response(limits)

    # ── WebRTC ────────────────────────────────────────────────────────────

    # ── Client-scoped audio control ──────────────────────────────────

    @routes.get("/api/clients/{clientId}/guard")
    async def get_guard_by_client(request: web.Request) -> web.Response:
        if webrtc_manager is None:
            return web.json_response({"error": "WebRTC not available"}, status=501)
        client_id = request.match_info["clientId"]
        enabled = webrtc_manager.is_guard_enabled(client_id)
        return web.json_response({"enabled": enabled})

    @routes.post("/api/clients/{clientId}/guard")
    async def set_guard_by_client(request: web.Request) -> web.Response:
        if webrtc_manager is None:
            return web.json_response({"error": "WebRTC not available"}, status=501)
        client_id = request.match_info["clientId"]
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        enabled = bool(body.get("enabled", False))
        webrtc_manager.set_guard_enabled(client_id, enabled)
        return web.json_response({"ok": True, "enabled": enabled})

    @routes.post("/api/clients/{clientId}/tts-mute")
    async def set_tts_muted_by_client(request: web.Request) -> web.Response:
        if webrtc_manager is None:
            return web.json_response({"error": "WebRTC not available"}, status=501)
        client_id = request.match_info["clientId"]
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        muted = bool(body.get("muted", False))
        webrtc_manager.set_tts_muted(client_id, muted)
        return web.json_response({"ok": True, "muted": muted})

    # ── Session-scoped guard/tts-mute (backward compat wrappers) ──

    @routes.get("/api/sessions/{id}/guard")
    async def get_guard(request: web.Request) -> web.Response:
        """Backward-compat wrapper — looks up client_id from active connection."""
        if webrtc_manager is None:
            return web.json_response({"error": "WebRTC not available"}, status=501)
        pair = webrtc_manager.get_any_outgoing_track()
        client_id = pair[0] if pair else ""
        enabled = webrtc_manager.is_guard_enabled(client_id)
        return web.json_response({"enabled": enabled})

    @routes.post("/api/sessions/{id}/guard")
    async def set_guard(request: web.Request) -> web.Response:
        """Backward-compat wrapper — looks up client_id from active connection."""
        if webrtc_manager is None:
            return web.json_response({"error": "WebRTC not available"}, status=501)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        enabled = bool(body.get("enabled", False))
        pair = webrtc_manager.get_any_outgoing_track()
        client_id = pair[0] if pair else ""
        webrtc_manager.set_guard_enabled(client_id, enabled)
        return web.json_response({"ok": True, "enabled": enabled})

    @routes.post("/api/sessions/{id}/tts-mute")
    async def set_tts_muted(request: web.Request) -> web.Response:
        """Backward-compat wrapper — looks up client_id from active connection."""
        if webrtc_manager is None:
            return web.json_response({"error": "WebRTC not available"}, status=501)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        muted = bool(body.get("muted", False))
        pair = webrtc_manager.get_any_outgoing_track()
        client_id = pair[0] if pair else ""
        webrtc_manager.set_tts_muted(client_id, muted)
        return web.json_response({"ok": True, "muted": muted})

    @routes.post("/api/sessions/{id}/pen")
    async def set_pen(request: web.Request) -> web.Response:
        sid = request.match_info["id"]
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        controlled_by = body.get("controlledBy", "ring0")
        if controlled_by not in ("ring0", "user"):
            return web.json_response({"error": "controlledBy must be 'ring0' or 'user'"}, status=400)
        session = ws_bridge._sessions.get(sid)
        if not session:
            return web.json_response({"error": "Session not found"}, status=404)
        if controlled_by == "ring0":
            import asyncio
            asyncio.ensure_future(ws_bridge._release_pen(session))
        else:
            import time as _time
            session.controlled_by = "user"
            session.pen_taken_at = _time.time()
            session.state["controlledBy"] = "user"
            ws_bridge._schedule_pen_release(session)
            asyncio.ensure_future(ws_bridge._broadcast_to_browsers(session, {"type": "session_update", "session": {"controlledBy": "user"}}))
        return web.json_response({"ok": True, "controlledBy": controlled_by})

    @routes.get("/api/webrtc/ice-servers")
    async def get_ice_servers(request: web.Request) -> web.Response:
        if webrtc_manager is None:
            return web.json_response({"iceServers": []})
        return web.json_response({"iceServers": webrtc_manager.get_client_ice_servers()})

    @routes.post("/api/webrtc/offer")
    async def webrtc_offer(request: web.Request) -> web.Response:
        if webrtc_manager is None:
            return web.json_response({"error": "WebRTC not available"}, status=501)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        client_id = body.get("clientId", "")
        sdp = body.get("sdp")
        sdp_type = body.get("type", "offer")
        session_id = body.get("sessionId", "")
        playground = bool(body.get("playground", False))
        desktop = bool(body.get("desktop", False))
        desktop_role = body.get("desktopRole", "controller")
        profile_id = body.get("profileId")
        username = _get_username(request)

        if not client_id or not sdp:
            return web.json_response(
                {"error": "clientId and sdp required"}, status=400
            )

        # Desktop offers: route to remote node if client specified one
        target_node_id = body.get("nodeId", "")
        if desktop and target_node_id and node_registry:
            node = node_registry.get_node(target_node_id) or node_registry.get_node_by_name(target_node_id)
            if node and node.tunnel and node.tunnel.connected:
                # Remote node — forward via tunnel
                try:
                    result = await node.tunnel.send_command({
                        "type": "webrtc_offer",
                        "clientId": client_id,
                        "sdp": sdp,
                        "sdpType": sdp_type,
                        "desktopRole": desktop_role,
                        "iceServers": webrtc_manager.get_client_ice_servers(),
                    }, timeout=30.0)
                    if result.get("error"):
                        return web.json_response(
                            {"error": result["error"]}, status=500
                        )
                    return web.json_response(result)
                except Exception as e:
                    logger.error("[webrtc] Failed to forward desktop offer to node %s: %s", node.name, e)
                    return web.json_response({"error": str(e)}, status=500)
            elif not node or node.tunnel:
                # Unknown node, or remote node with disconnected tunnel
                return web.json_response(
                    {"error": "Remote node unavailable"}, status=503
                )
            # Local node (no tunnel) — fall through to local handling

        try:
            answer = await webrtc_manager.handle_offer(
                client_id, sdp, sdp_type,
                session_id=session_id,
                playground=playground,
                profile_id=profile_id,
                username=username,
                desktop=desktop,
                desktop_role=desktop_role,
            )
            return web.json_response(answer)
        except Exception as e:
            logger.error("[webrtc] Failed to handle offer: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    # ── Ring0 ─────────────────────────────────────────────────────────────

    @routes.get("/api/ring0/sessions")
    async def ring0_sessions(request: web.Request) -> web.Response:
        """List sessions — proxied for Ring0 MCP server (auth-exempt)."""
        r0_id = ring0_manager.session_id if ring0_manager else None
        sessions = []
        for sid in launcher.get_all_session_ids():
            info = launcher.get_session(sid)
            if not info:
                continue
            name = session_names.get_name(sid) or info.name or "unnamed"
            bridge_session = ws_bridge._sessions.get(sid)
            entry = {
                "sessionId": sid,
                "name": name,
                "state": info.state,
                "cwd": info.cwd,
                "backendType": info.backendType,
                "archived": info.archived,
                "pendingPermissions": ws_bridge.get_pending_permission_count(sid),
                "controlledBy": bridge_session.controlled_by if bridge_session else "ring0",
            }
            if r0_id and sid == r0_id:
                entry["isRing0"] = True
            sessions.append(entry)
        return web.json_response(sessions)

    @routes.get("/api/ring0/node-environment")
    async def ring0_node_environment(request: web.Request) -> web.Response:
        """Return environment metadata for the node this Ring0 runs on."""
        info = {
            "nodeName": node_registry.hub_name if node_registry else "local",
            "platform": plat.system().lower(),
            "arch": plat.machine(),
            "hostname": plat.node(),
            "containerized": Path("/.dockerenv").exists(),
            "display": bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
        }
        return web.json_response(info)

    @routes.get("/api/ring0/status")
    async def ring0_status(request: web.Request) -> web.Response:
        if ring0_manager is None:
            return web.json_response({"enabled": False, "sessionId": None})
        return web.json_response({
            "enabled": ring0_manager.is_enabled,
            "eventsMuted": ring0_manager.events_muted,
            "sessionId": ring0_manager.session_id,
        })

    @routes.post("/api/ring0/toggle")
    async def ring0_toggle(request: web.Request) -> web.Response:
        if ring0_manager is None:
            return web.json_response({"error": "Ring0 not available"}, status=501)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        enabled = bool(body.get("enabled", False))
        ring0_manager.toggle(enabled)
        # Ensure session exists when enabling
        if enabled:
            session_id = await ring0_manager.ensure_session(launcher, ws_bridge)
            return web.json_response({"ok": True, "enabled": True, "sessionId": session_id})
        return web.json_response({"ok": True, "enabled": False, "sessionId": ring0_manager.session_id})

    @routes.post("/api/ring0/mute-events")
    async def ring0_mute_events(request: web.Request) -> web.Response:
        if ring0_manager is None:
            return web.json_response({"error": "Ring0 not available"}, status=501)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        muted = bool(body.get("muted", False))
        ring0_manager.set_events_muted(muted)
        return web.json_response({"ok": True, "eventsMuted": muted})

    @routes.post("/api/ring0/send-message")
    async def ring0_send_message(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        session_id = body.get("sessionId")
        message = body.get("message")
        if not session_id or not message:
            return web.json_response({"error": "sessionId and message required"}, status=400)
        # Resolve short session ID prefix
        resolved = _resolve_session_id(session_id, launcher)
        if not resolved:
            return web.json_response({"error": f"Session not found: {session_id}"}, status=404)
        err = await ws_bridge.submit_user_message(resolved, message)
        if err:
            return web.json_response({"error": err, "controlledBy": "user"}, status=409)
        return web.json_response({"ok": True, "sessionId": resolved})

    @routes.post("/api/ring0/switch-ui")
    async def ring0_switch_ui(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        session_id = body.get("sessionId")
        if not session_id:
            return web.json_response({"error": "sessionId required"}, status=400)
        resolved = _resolve_session_id(session_id, launcher)
        if not resolved:
            return web.json_response({"error": f"Session not found: {session_id}"}, status=404)
        client_id = body.get("clientId", "")
        sent = await ws_bridge.broadcast_ring0_switch_ui(resolved, client_id=client_id)
        if client_id and not sent:
            return web.json_response({"error": f"Client {client_id} not connected"}, status=400)
        return web.json_response({"ok": True, "sessionId": resolved})

    # ── Client metadata ────────────────────────────────────────────────

    @routes.get("/api/clients")
    async def list_clients(request: web.Request) -> web.Response:
        clients = ws_bridge._build_client_list()
        return web.json_response(clients)

    @routes.get("/api/clients/{clientId}")
    async def get_client(request: web.Request) -> web.Response:
        client_id = request.match_info["clientId"]
        meta = ws_bridge.get_client_metadata(client_id)
        if not meta:
            return web.json_response({"error": "Client not found"}, status=404)
        result = dict(meta)
        result["clientId"] = client_id
        result["online"] = client_id in ws_bridge._client_sessions
        return web.json_response(result)

    @routes.put("/api/clients/{clientId}")
    async def update_client(request: web.Request) -> web.Response:
        client_id = request.match_info["clientId"]
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        updates = {k: v for k, v in body.items() if k in ("name", "description", "role")}
        if not updates:
            return web.json_response({"error": "No valid fields to update"}, status=400)
        entry = ws_bridge.set_client_metadata(client_id, updates)
        result = dict(entry)
        result["clientId"] = client_id
        return web.json_response(result)

    @routes.post("/api/clients/{clientId}/device-info")
    async def report_device_info(request: web.Request) -> web.Response:
        client_id = request.match_info["clientId"]
        try:
            device_info = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        entry = ws_bridge.register_device_info(client_id, device_info)
        result = dict(entry)
        result["clientId"] = client_id
        return web.json_response(result)

    # ── Ring0 API ────────────────────────────────────────────────────────

    @routes.get("/api/ring0/session-output/{id}")
    async def ring0_session_output(request: web.Request) -> web.Response:
        session_id = request.match_info["id"]
        resolved = _resolve_session_id(session_id, launcher)
        if not resolved:
            return web.json_response({"error": f"Session not found: {session_id}"}, status=404)
        messages = ws_bridge.get_message_history(resolved)
        pending = ws_bridge.get_pending_permissions(resolved)
        formatted = _format_session_output(messages, pending)
        return web.json_response({"messages": messages, "pendingPermissions": pending, "formatted": formatted})

    @routes.get("/api/ring0/clients")
    async def ring0_clients(request: web.Request) -> web.Response:
        clients = ws_bridge.get_all_clients()
        return web.json_response(clients)

    @routes.post("/api/ring0/query-client")
    async def ring0_query_client(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        client_id = body.get("clientId", "")
        method = body.get("method", "")
        params = body.get("params")
        if not client_id or not method:
            return web.json_response({"error": "clientId and method required"}, status=400)
        # Longer timeout for methods that may trigger browser permission prompts
        interactive_methods = {"get_location", "send_notification", "read_clipboard", "write_clipboard", "capture_screenshot"}
        timeout = 30.0 if method in interactive_methods else 5.0
        try:
            result = await ws_bridge.rpc_call(client_id, method, params, timeout=timeout)
            return web.json_response({"ok": True, "result": result})
        except RuntimeError as e:
            err_str = str(e)
            status = 504 if "timed out" in err_str else 400
            return web.json_response({"error": err_str}, status=status)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    @routes.post("/api/ring0/respond-permission")
    async def ring0_respond_permission(request: web.Request) -> web.Response:
        """Allow or deny a pending permission request (used by Ring0 MCP)."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        session_id = body.get("sessionId", "")
        request_id = body.get("requestId", "")
        behavior = body.get("behavior", "")
        message = body.get("message", "")
        if not session_id or not request_id or behavior not in ("allow", "deny"):
            return web.json_response(
                {"error": "sessionId, requestId, and behavior (allow/deny) required"}, status=400
            )
        resolved = _resolve_session_id(session_id, launcher)
        if not resolved:
            return web.json_response({"error": f"Session not found: {session_id}"}, status=404)
        ok = await ws_bridge.respond_to_permission(resolved, request_id, behavior, message)
        if not ok:
            return web.json_response({"error": f"Permission {request_id} not found"}, status=404)
        return web.json_response({"ok": True})

    @routes.post("/api/ring0/interrupt")
    async def ring0_interrupt(request: web.Request) -> web.Response:
        """Interrupt a running session (used by Ring0 MCP, auth-exempt)."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        session_id = body.get("sessionId", "")
        if not session_id:
            return web.json_response({"error": "sessionId required"}, status=400)
        resolved = _resolve_session_id(session_id, launcher)
        if not resolved:
            return web.json_response({"error": f"Session not found: {session_id}"}, status=404)
        ok = ws_bridge.interrupt_session(resolved)
        if not ok:
            return web.json_response({"error": f"Session {session_id} not found in bridge"}, status=404)
        return web.json_response({"ok": True, "sessionId": resolved})

    @routes.post("/api/ring0/create-session")
    async def ring0_create_session(request: web.Request) -> web.Response:
        """Create a new session (used by Ring0 MCP, auth-exempt)."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        backend = body.get("backend", "claude")
        if backend not in ("claude", "codex", "computer-use"):
            return web.json_response({"error": f"Invalid backend: {backend}"}, status=400)
        cwd = body.get("cwd")
        if not cwd and backend != "computer-use":
            return web.json_response({"error": "cwd is required"}, status=400)
        name = body.get("name", "").strip()
        opts = LaunchOptions(
            model=body.get("model"),
            permissionMode=body.get("permissionMode"),
            cwd=cwd,
            backendType=backend,
            nodeId=body.get("nodeId") or None,
        )
        try:
            session = launcher.launch(opts)
            if name:
                session_names.set_name(session.sessionId, name)
            result = session.to_dict()
            if name:
                result["name"] = name
            return web.json_response(result)
        except Exception as e:
            logger.exception(f"[routes] Ring0 create session failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    @routes.post("/api/ring0/rename-session")
    async def ring0_rename_session(request: web.Request) -> web.Response:
        """Rename a session (used by Ring0 MCP, auth-exempt)."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        sid = body.get("sessionId", "").strip()
        name = body.get("name", "").strip()
        if not sid or not name:
            return web.json_response({"error": "sessionId and name are required"}, status=400)
        resolved = _resolve_session_id(sid, launcher)
        if not resolved:
            return web.json_response({"error": f"Session {sid} not found"}, status=404)
        session_names.set_name(resolved, name, unique=False)
        await ws_bridge.broadcast_name_update(resolved, name, user_renamed=True)
        return web.json_response({"ok": True, "sessionId": resolved, "name": name})

    _ALLOWED_SESSION_MODES = {"plan", "acceptEdits"}

    @routes.post("/api/ring0/set-session-mode")
    async def ring0_set_session_mode(request: web.Request) -> web.Response:
        """Set a session's permission mode (used by Ring0 MCP, auth-exempt)."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        session_id = body.get("sessionId", "")
        mode = body.get("mode", "")
        if not session_id:
            return web.json_response({"error": "sessionId required"}, status=400)
        if mode not in _ALLOWED_SESSION_MODES:
            return web.json_response({"error": f"mode must be one of: {', '.join(sorted(_ALLOWED_SESSION_MODES))}"}, status=400)
        resolved = _resolve_session_id(session_id, launcher)
        if not resolved:
            return web.json_response({"error": f"Session not found: {session_id}"}, status=404)
        session = ws_bridge.get_session(resolved)
        if not session:
            return web.json_response({"error": f"Session not found in bridge: {session_id}"}, status=404)
        # Forward to CLI
        ws_bridge._handle_set_permission_mode(session, mode)
        # Update state and broadcast to browsers immediately
        session.state["permissionMode"] = mode
        await ws_bridge._broadcast_to_browsers(session, {"type": "session_update", "session": {"permissionMode": mode}})
        return web.json_response({"ok": True, "sessionId": resolved, "mode": mode})

    @routes.get("/api/ring0/get-session-mode")
    async def ring0_get_session_mode(request: web.Request) -> web.Response:
        """Get a session's current permission mode (used by Ring0 MCP, auth-exempt)."""
        session_id = request.query.get("sessionId", "")
        if not session_id:
            return web.json_response({"error": "sessionId query param required"}, status=400)
        resolved = _resolve_session_id(session_id, launcher)
        if not resolved:
            return web.json_response({"error": f"Session not found: {session_id}"}, status=404)
        session = ws_bridge.get_session(resolved)
        if not session:
            return web.json_response({"error": f"Session not found in bridge: {session_id}"}, status=404)
        mode = session.state.get("permissionMode", "default")
        return web.json_response({"sessionId": resolved, "mode": mode})

    @routes.post("/api/ring0/set-guard")
    async def ring0_set_guard(request: web.Request) -> web.Response:
        """Toggle guard mode (used by Ring0 MCP, auth-exempt).

        Uses the active WebRTC client.  Accepts optional ``clientId``
        in the body; falls back to whatever connection is active.
        """
        if webrtc_manager is None:
            return web.json_response({"error": "WebRTC not available"}, status=501)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        enabled = bool(body.get("enabled", False))
        client_id = body.get("clientId", "")
        if not client_id:
            pair = webrtc_manager.get_any_outgoing_track()
            client_id = pair[0] if pair else ""
        webrtc_manager.set_guard_enabled(client_id, enabled)
        return web.json_response({"ok": True, "clientId": client_id, "enabled": enabled})

    # ── Voice Profiles ──────────────────────────────────────────────────

    def _get_username(request: web.Request) -> str:
        return request.get("auth_user", "default")

    @routes.get("/api/voice/profiles")
    async def voice_profiles_list(request: web.Request) -> web.Response:
        username = _get_username(request)
        profiles = voice_profiles.list_profiles(username)
        return web.json_response(profiles)

    @routes.post("/api/voice/profiles")
    async def voice_profiles_create(request: web.Request) -> web.Response:
        username = _get_username(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        profile = voice_profiles.create_profile(username, body)
        return web.json_response(profile, status=201)

    @routes.put("/api/voice/profiles/{id}")
    async def voice_profiles_update(request: web.Request) -> web.Response:
        username = _get_username(request)
        profile_id = request.match_info["id"]
        try:
            body = await request.json()
        except Exception:
            body = {}
        profile = voice_profiles.update_profile(username, profile_id, body)
        if not profile:
            return web.json_response({"error": "Profile not found"}, status=404)
        return web.json_response(profile)

    @routes.delete("/api/voice/profiles/{id}")
    async def voice_profiles_delete(request: web.Request) -> web.Response:
        username = _get_username(request)
        profile_id = request.match_info["id"]
        deleted = voice_profiles.delete_profile(username, profile_id)
        if not deleted:
            return web.json_response({"error": "Profile not found"}, status=404)
        return web.json_response({"ok": True})

    @routes.post("/api/voice/profiles/{id}/activate")
    async def voice_profiles_activate(request: web.Request) -> web.Response:
        username = _get_username(request)
        profile_id = request.match_info["id"]
        profile = voice_profiles.activate_profile(username, profile_id)
        if not profile:
            return web.json_response({"error": "Profile not found"}, status=404)
        return web.json_response(profile)

    @routes.post("/api/voice/profiles/deactivate")
    async def voice_profiles_deactivate(request: web.Request) -> web.Response:
        username = _get_username(request)
        voice_profiles.deactivate_all(username)
        return web.json_response({"ok": True})

    @routes.get("/api/voice/profiles/active")
    async def voice_profiles_active(request: web.Request) -> web.Response:
        username = _get_username(request)
        profile = voice_profiles.get_active_profile(username)
        if not profile:
            # Return defaults as a virtual profile
            from server.stt import STTParams
            defaults = STTParams()
            return web.json_response({
                "id": None,
                "name": "Default",
                "user": username,
                "micGain": defaults.mic_gain,
                "vadThresholdDb": defaults.vad_threshold_db,
                "sileroVadThreshold": defaults.silero_vad_threshold,
                "eouThreshold": defaults.eou_threshold,
                "eouMaxRetries": defaults.eou_max_retries,
                "minSegmentDuration": defaults.min_segment_duration,
                "isActive": True,
            })
        return web.json_response(profile)

    # ── Voice Logs ───────────────────────────────────────────────────────

    @routes.get("/api/voice/logs")
    async def voice_logs_list(request: web.Request) -> web.Response:
        username = _get_username(request)
        q = request.query.get("q", "")
        offset = int(request.query.get("offset", "0"))
        limit = int(request.query.get("limit", "50"))
        segments = voice_logger.list_segments(username, query=q, offset=offset, limit=limit)
        return web.json_response(segments)

    @routes.get("/api/voice/logs/{id}/audio")
    async def voice_logs_audio(request: web.Request) -> web.Response:
        username = _get_username(request)
        segment_id = request.match_info["id"]
        audio_path = voice_logger.get_segment_audio_path(username, segment_id)
        if not audio_path:
            return web.json_response({"error": "Segment not found"}, status=404)
        return web.FileResponse(audio_path, headers={"Content-Type": "audio/wav"})

    @routes.get("/api/voice/recordings")
    async def voice_recordings_list(request: web.Request) -> web.Response:
        username = _get_username(request)
        recordings = voice_logger.list_recordings(username)
        return web.json_response(recordings)

    @routes.get("/api/voice/recordings/{id}/audio")
    async def voice_recordings_audio(request: web.Request) -> web.Response:
        username = _get_username(request)
        recording_id = request.match_info["id"]
        audio_path = voice_logger.get_recording_audio_path(username, recording_id)
        if not audio_path:
            return web.json_response({"error": "Recording not found"}, status=404)
        # Support HTTP Range requests for seeking
        return web.FileResponse(audio_path, headers={"Content-Type": "audio/wav"})

    @routes.get("/api/voice/seg-params/{id}")
    async def voice_seg_params_get(request: web.Request) -> web.Response:
        username = _get_username(request)
        seg_params_id = request.match_info["id"]
        result = voice_logger.get_seg_params(username, seg_params_id)
        if not result:
            return web.json_response({"error": "Seg params not found"}, status=404)
        return web.json_response(result)

    @routes.delete("/api/voice/logs/{id}")
    async def voice_logs_delete(request: web.Request) -> web.Response:
        username = _get_username(request)
        segment_id = request.match_info["id"]
        deleted = voice_logger.delete_segment(username, segment_id)
        if not deleted:
            return web.json_response({"error": "Segment not found"}, status=404)
        return web.json_response({"ok": True})

    @routes.delete("/api/voice/logs")
    async def voice_logs_clear(request: web.Request) -> web.Response:
        username = _get_username(request)
        voice_logger.clear_all_logs(username)
        return web.json_response({"ok": True})

    # ── Second Screen pairing ───────────────────────────────────────────

    # Ephemeral pairing codes: {code → {secondScreenClientId, expiresAt}}
    _pairing_codes: dict[str, dict[str, Any]] = {}
    # Durable pairings: {secondScreenClientId → {pairedUser, pairedAt, enabled}}
    _second_screen_pairings: dict[str, dict[str, Any]] = {}
    _PAIRINGS_PATH = Path.home() / ".vibr8" / "second-screens.json"

    def _default_username() -> str:
        """Get the single configured username for migration, or 'default'."""
        users_path = Path.home() / ".vibr8" / "users.json"
        if users_path.exists():
            try:
                data = json.loads(users_path.read_text())
                users = data.get("users", {})
                if len(users) == 1:
                    return next(iter(users))
            except Exception:
                pass
        return "default"

    def _load_pairings() -> None:
        nonlocal _second_screen_pairings
        if _PAIRINGS_PATH.exists():
            try:
                _second_screen_pairings = json.loads(_PAIRINGS_PATH.read_text())
                # Migrate legacy entries: pairedClientId → pairedUser
                migrated = False
                for info in _second_screen_pairings.values():
                    if "pairedClientId" in info and "pairedUser" not in info:
                        info["pairedUser"] = _default_username()
                        del info["pairedClientId"]
                        migrated = True
                if migrated:
                    _save_pairings()
                    logger.info("[second-screen] Migrated pairings to user-based model")
            except Exception:
                pass

    def _save_pairings() -> None:
        _PAIRINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PAIRINGS_PATH.write_text(json.dumps(_second_screen_pairings))

    _load_pairings()

    @routes.post("/api/second-screen/pair-code")
    async def second_screen_pair_code(request: web.Request) -> web.Response:
        """Legacy — delegates to unified pairing. Second screen requests a code."""
        if not auth_manager or not auth_manager.enabled:
            # No-auth fallback: use old ephemeral codes
            body = await request.json()
            client_id = body.get("clientId", "")
            if not client_id:
                return web.json_response({"error": "clientId required"}, status=400)
            code = secrets.token_hex(3).upper()
            _pairing_codes[code] = {"secondScreenClientId": client_id, "expiresAt": time.time() + 300}
            return web.json_response({"code": code})
        body = await request.json()
        client_id = body.get("clientId", "")
        if not client_id:
            return web.json_response({"error": "clientId required"}, status=400)
        ip = request.remote or "unknown"
        result = auth_manager.request_pairing("second-screen", ip, client_id)
        return web.json_response({"code": result["code"]})

    @routes.post("/api/second-screen/pair")
    async def second_screen_pair(request: web.Request) -> web.Response:
        """Legacy — delegates to unified pairing. Primary confirms a code."""
        body = await request.json()
        code = body.get("code", "").strip()
        if not code:
            return web.json_response({"error": "code required"}, status=400)
        username = request.get("auth_user") or body.get("username") or _default_username()
        name = body.get("name", "Second Screen")

        if auth_manager and auth_manager.enabled:
            result = auth_manager.confirm_pairing(code, username, name)
            if not result:
                return web.json_response({"error": "Invalid or expired code"}, status=400)
            client_id = result.get("clientId", "")
        else:
            # No-auth fallback: use old ephemeral codes
            code = code.upper()
            entry = _pairing_codes.get(code)
            if not entry or time.time() > entry["expiresAt"]:
                _pairing_codes.pop(code, None)
                return web.json_response({"error": "Invalid or expired code"}, status=400)
            client_id = entry["secondScreenClientId"]
            _pairing_codes.pop(code)

        _second_screen_pairings[client_id] = {
            "pairedUser": username,
            "pairedAt": time.time(),
            "enabled": True,
        }
        _save_pairings()
        ws = ws_bridge._ws_by_client.get(client_id)
        if ws and not ws.closed:
            await ws.send_str(json.dumps({
                "type": "second_screen_paired",
                "pairedUser": username,
            }))
        from server.ring0_events import Ring0Event
        await ws_bridge.emit_ring0_event(Ring0Event(fields={
            "type": "second_screen_paired",
            "clientId": client_id[:8], "user": username,
        }))
        logger.info(f"[second-screen] Paired {client_id[:8]} → user={username}")
        return web.json_response({"ok": True, "secondScreenClientId": client_id})

    @routes.get("/api/second-screen/status")
    async def second_screen_status(request: web.Request) -> web.Response:
        """Check pairing status for a client."""
        client_id = request.rel_url.query.get("clientId", "")
        if not client_id:
            return web.json_response({"error": "clientId required"}, status=400)

        # Check if this client is a paired second screen
        pairing = _second_screen_pairings.get(client_id)
        if pairing:
            return web.json_response({
                "paired": True,
                "role": "secondscreen",
                "pairedUser": pairing["pairedUser"],
                "pairedAt": pairing["pairedAt"],
            })

        # Check if the requesting user has any second screens paired
        username = request.get("auth_user")
        if username:
            screens = [
                {"clientId": scid, **info}
                for scid, info in _second_screen_pairings.items()
                if info.get("pairedUser") == username
            ]
            if screens:
                return web.json_response({"paired": True, "role": "primary", "screens": screens})

        return web.json_response({"paired": False})

    @routes.post("/api/second-screen/unpair")
    async def second_screen_unpair(request: web.Request) -> web.Response:
        """Unpair a second screen."""
        body = await request.json()
        client_id = body.get("clientId", "")
        if not client_id:
            return web.json_response({"error": "clientId required"}, status=400)

        removed = _second_screen_pairings.pop(client_id, None)
        if removed:
            _save_pairings()

            # Notify Ring0
            from server.ring0_events import Ring0Event
            await ws_bridge.emit_ring0_event(Ring0Event(
                fields={"type": "second_screen_unpaired", "clientId": client_id[:8]},
            ))

            logger.info(f"[second-screen] Unpaired {client_id[:8]}")
        return web.json_response({"ok": True})

    @routes.get("/api/second-screen/list")
    async def second_screen_list(request: web.Request) -> web.Response:
        """List all paired second screens with online status."""
        screens = []
        for scid, info in _second_screen_pairings.items():
            online = scid in ws_bridge._ws_by_client and not ws_bridge._ws_by_client[scid].closed
            entry: dict[str, Any] = {
                "clientId": scid,
                "pairedUser": info.get("pairedUser", "default"),
                "pairedAt": info["pairedAt"],
                "enabled": info.get("enabled", True),
                "online": online,
            }
            meta = ws_bridge.get_client_metadata(scid)
            if meta and meta.get("name"):
                entry["name"] = meta["name"]
            screens.append(entry)
        return web.json_response(screens)

    @routes.post("/api/second-screen/toggle")
    async def second_screen_toggle(request: web.Request) -> web.Response:
        """Enable or disable a second screen."""
        body = await request.json()
        client_id = body.get("clientId", "")
        enabled = body.get("enabled", True)
        if not client_id:
            return web.json_response({"error": "clientId required"}, status=400)

        pairing = _second_screen_pairings.get(client_id)
        if not pairing:
            return web.json_response({"error": "Unknown second screen"}, status=404)

        pairing["enabled"] = bool(enabled)
        _save_pairings()

        # Notify Ring0
        from server.ring0_events import Ring0Event
        action = "enabled" if enabled else "disabled"
        await ws_bridge.emit_ring0_event(Ring0Event(
            fields={"type": f"second_screen_{action}", "clientId": client_id[:8]},
        ))

        logger.info(f"[second-screen] {'Enabled' if enabled else 'Disabled'} {client_id[:8]}")
        return web.json_response({"ok": True, "enabled": bool(enabled)})

    # ── Nodes ─────────────────────────────────────────────────────────────

    @routes.post("/api/nodes/register")
    async def register_node(request: web.Request) -> web.Response:
        """Register a remote node (API-key auth, not cookie auth)."""
        if node_registry is None:
            return web.json_response({"error": "Node registry not available"}, status=503)
        body = await request.json()
        name = body.get("name", "").strip()
        api_key = body.get("apiKey", "")
        capabilities = body.get("capabilities", {})
        if not name or not api_key:
            return web.json_response({"error": "name and apiKey required"}, status=400)
        try:
            node = node_registry.register(name, api_key, capabilities)
        except PermissionError as e:
            return web.json_response({"error": str(e)}, status=403)
        return web.json_response({"ok": True, "nodeId": node.id})

    @routes.get("/api/nodes")
    async def list_nodes(request: web.Request) -> web.Response:
        """List all registered nodes (including the local node)."""
        if node_registry is None:
            import platform as _platform
            hub_name = _platform.node() or "Local"
            return web.json_response([{"id": "local", "name": hub_name, "status": "online", "platform": "", "hostname": "", "sessionCount": len(launcher.list_sessions()), "ring0Enabled": ring0_manager.is_enabled if ring0_manager else False}])
        # Update local node's dynamic fields before serializing
        local = node_registry.local_node
        local.session_ids = [s.sessionId for s in launcher.list_sessions()]
        local.ring0_enabled = ring0_manager.is_enabled if ring0_manager else False
        nodes = [n.to_api_dict() for n in node_registry.get_all_nodes()]
        return web.json_response(nodes)

    @routes.delete("/api/nodes/{node_id}")
    async def delete_node(request: web.Request) -> web.Response:
        """Remove a registered node."""
        if node_registry is None:
            return web.json_response({"error": "Node registry not available"}, status=503)
        node_id = request.match_info["node_id"]
        removed = node_registry.unregister(node_id)
        if not removed:
            return web.json_response({"error": "Node not found"}, status=404)
        return web.json_response({"ok": True})

    @routes.post("/api/nodes/{node_id}/activate")
    async def activate_node(request: web.Request) -> web.Response:
        """Switch the active node."""
        if node_registry is None:
            return web.json_response({"error": "Node registry not available"}, status=503)
        node_id = request.match_info["node_id"]
        try:
            node_registry.active_node_id = node_id
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=404)
        node = node_registry.get_node(node_id)
        name = node.name if node else node_id
        logger.info("[nodes] Active node switched to %r (%s)", name, node_id[:8])
        return web.json_response({"ok": True, "nodeId": node_id, "name": name})

    @routes.get("/api/nodes/active")
    async def get_active_node(request: web.Request) -> web.Response:
        """Get the currently active node."""
        if node_registry is None:
            return web.json_response({"nodeId": "local", "name": "Local", "status": "online"})
        node_id = node_registry.active_node_id
        node = node_registry.get_node(node_id)
        if not node:
            node = node_registry.local_node
        return web.json_response({"nodeId": node.id, "name": node.name, "status": node.status})

    @routes.post("/api/nodes/hub-name")
    async def set_hub_name(request: web.Request) -> web.Response:
        """Rename the hub node."""
        if node_registry is None:
            return web.json_response({"error": "Node registry not available"}, status=503)
        body = await request.json()
        name = body.get("name", "").strip()
        if not name:
            return web.json_response({"error": "name required"}, status=400)
        node_registry.hub_name = name
        return web.json_response({"ok": True, "name": node_registry.hub_name})

    @routes.post("/api/nodes/generate-key")
    async def generate_node_key(request: web.Request) -> web.Response:
        """Generate a new API key for node registration."""
        if node_registry is None:
            return web.json_response({"error": "Node registry not available"}, status=503)
        body = await request.json() if request.content_length else {}
        name = body.get("name", "")
        raw_key, entry = node_registry.generate_api_key(name)
        return web.json_response({"apiKey": raw_key, **entry.to_api_dict()})

    @routes.get("/api/nodes/keys")
    async def list_node_keys(request: web.Request) -> web.Response:
        """List all issued API keys (no raw keys)."""
        if node_registry is None:
            return web.json_response({"error": "Node registry not available"}, status=503)
        keys = node_registry.list_api_keys()
        return web.json_response([k.to_api_dict() for k in keys])

    @routes.delete("/api/nodes/keys/{key_id}")
    async def revoke_node_key(request: web.Request) -> web.Response:
        """Revoke an API key."""
        if node_registry is None:
            return web.json_response({"error": "Node registry not available"}, status=503)
        key_id = request.match_info["key_id"]
        if node_registry.revoke_api_key(key_id):
            return web.json_response({"ok": True})
        return web.json_response({"error": "Key not found"}, status=404)

    return routes


def _resolve_session_id(session_id: str, launcher: CliLauncher) -> str | None:
    """Resolve a full or prefix session ID to a full session ID."""
    info = launcher.get_session(session_id)
    if info:
        return session_id
    # Try prefix match
    for sid in launcher.get_all_session_ids():
        if sid.startswith(session_id):
            return sid
    return None


def _cleanup_worktree(
    session_id: str,
    worktree_tracker: WorktreeTracker,
    force: bool | None = None,
) -> dict[str, Any] | None:
    mapping = worktree_tracker.get_by_session(session_id)
    if not mapping:
        return None

    if worktree_tracker.is_worktree_in_use(mapping.worktreePath, session_id):
        worktree_tracker.remove_by_session(session_id)
        return {"cleaned": False, "path": mapping.worktreePath}

    dirty = git_utils.is_worktree_dirty(mapping.worktreePath)
    if dirty and not force:
        logger.info(f"[routes] Worktree {mapping.worktreePath} is dirty, not auto-removing")
        return {"cleaned": False, "dirty": True, "path": mapping.worktreePath}

    branch_to_delete = None
    if mapping.actualBranch and mapping.actualBranch != mapping.branch:
        branch_to_delete = mapping.actualBranch

    result = git_utils.remove_worktree(
        mapping.repoRoot,
        mapping.worktreePath,
        force=dirty,
        branch_to_delete=branch_to_delete,
    )
    if result.get("removed"):
        worktree_tracker.remove_by_session(session_id)
        logger.info(f"[routes] {'Force-removed dirty' if dirty else 'Auto-removed clean'} worktree {mapping.worktreePath}")
    return {"cleaned": result.get("removed", False), "path": mapping.worktreePath}
