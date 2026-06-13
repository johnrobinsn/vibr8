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

from vibr8_core import artifacts, env_manager, session_names
from vibr8_core import git_utils
from server import speaker_fingerprints, voice_profiles, voice_logger
from server.usage_limits import get_usage_limits
from server.rate_limit import check_rate_limit, get_client_rate_limit_key
from vibr8_core.cli_launcher import CliLauncher, LaunchOptions, WorktreeInfo
from vibr8_core.worktree_tracker import WorktreeTracker, WorktreeMapping

if TYPE_CHECKING:
    from vibr8_core.ws_bridge import WsBridge
    from vibr8_core.session_store import SessionStore
    from server.webrtc import WebRTCManager
    from server.terminal import TerminalManager
    from server.auth import AuthManager

from vibr8_core.ring0 import Ring0Manager

logger = logging.getLogger(__name__)

NODE_REGISTER_RATE_LIMIT = 10
NODE_REGISTER_RATE_WINDOW = 60.0
NODE_TOKEN_NAME_MAX_LENGTH = 256


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
    """Format session messages into readable text for Ring0. No per-message truncation.

    Permissions are placed FIRST so they're never lost to context truncation.
    """
    if not messages and not permissions:
        return "No messages in this session yet."

    lines: list[str] = []

    if permissions:
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
        lines.append("")

    trailing_tool_count = 0
    msg_lines: list[str] = []
    for msg in messages:
        role = msg.get("type", "?")
        if role == "user_message":
            content = msg.get("content", "")
            msg_lines.append(f"User: {content}")
            trailing_tool_count = 0
        elif role == "assistant":
            raw_message = msg.get("message", "")
            if isinstance(raw_message, dict) and "content" in raw_message:
                raw_message = raw_message["content"]
            text, has_real_text = _extract_assistant_text(raw_message)
            if has_real_text:
                msg_lines.append(f"Assistant: {text}")
                trailing_tool_count = 0
            else:
                trailing_tool_count += 1
        elif role == "result":
            data = msg.get("data", {})
            if data.get("is_error"):
                msg_lines.append(f"Error: {', '.join(data.get('errors', []))}")
                trailing_tool_count = 0
    # Keep last 30 entries
    msg_lines = msg_lines[-30:]
    # Guard total size — drop oldest if over 50k chars
    while len(msg_lines) > 1 and sum(len(l) for l in msg_lines) > 50000:
        msg_lines.pop(0)

    if trailing_tool_count > 3:
        msg_lines.append(f"[Session is actively working — {trailing_tool_count} tool calls since last text response]")

    lines.extend(msg_lines)

    return "\n".join(lines) if lines else "No readable messages."


def _to_camel(name: str) -> str:
    """Convert snake_case to camelCase."""
    parts = name.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _camel_dict(obj: Any) -> dict[str, Any]:
    """Convert a dataclass or dict with snake_case keys to camelCase."""
    d = obj if isinstance(obj, dict) else obj.__dict__
    return {_to_camel(k): v for k, v in d.items()}


# Backend model discovery moved to vibr8_core.backend_models so it works
# on remote nodes too (resolves against the *node's* home/PATH).


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
    self_node_name: str = "",
    local_node_ops: Any | None = None,
    hub_browser_bridge: Any | None = None,
) -> web.RouteTableDef:
    routes = web.RouteTableDef()
    node_register_rate: dict[str, list[float]] = {}

    # If the caller didn't pre-build a NodeOperations, derive one from the
    # in-process managers we already have. Lets older callers (and tests)
    # keep working without explicit wiring.
    if local_node_ops is None:
        from vibr8_core.node_operations import NodeOperations
        local_node_ops = NodeOperations(
            launcher=launcher,
            bridge=ws_bridge,
            store=session_store,
            ring0=ring0_manager,
        )

    # Same lazy-build pattern for the hub-only browser/client surface.
    if hub_browser_bridge is None:
        from vibr8_core.hub_browser_bridge import HubBrowserBridge
        hub_browser_bridge = HubBrowserBridge(ws_bridge)

    def _resolve_client(session_id: str):
        """Return (client, raw_sid, is_remote) for a (possibly prefixed) session id.

        Hub-local sessions return (local_node_ops, session_id, False).
        Remote sessions return (RemoteNodeClient, raw_sid, True).
        Raises RemoteNodeUnavailable if the node is offline.
        """
        from vibr8_core.node_client import resolve_node_client
        return resolve_node_client(
            session_id,
            local_ops=local_node_ops,
            node_registry=node_registry,
            ws_bridge=ws_bridge,
        )

    def _resolve_node_client(node_id: str):
        """Return a NodeClient for a node_id (empty string → local hub)."""
        from vibr8_core.node_client import RemoteNodeClient, RemoteNodeUnavailable
        if not node_id:
            return local_node_ops, False
        if not node_registry:
            raise RemoteNodeUnavailable(node_id)
        node = node_registry.get_node(node_id)
        if not node or not node.tunnel or not node.tunnel.connected:
            raise RemoteNodeUnavailable(node_id)
        return RemoteNodeClient(node_id, node.tunnel), True

    def _status_for_error(msg: str) -> int:
        m = (msg or "").lower()
        if "not found" in m:
            return 404
        if "not enabled" in m or "not available" in m:
            return 503
        return 500

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
        # `SESSION_COOKIE_MAX_AGE` honors `VIBR8_SESSION_MAX_AGE_DAYS`
        # (default 30 days). When that env is 0 — "never expire" mode —
        # the constant falls back to 10 years so the browser keeps the
        # cookie across restarts; the server side skips the expiry check.
        from server.auth import SESSION_COOKIE_MAX_AGE
        resp.set_cookie(
            "vibr8_session",
            token,
            httponly=True,
            samesite="Lax",
            secure=request.secure,
            max_age=SESSION_COOKIE_MAX_AGE,
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
        ip = get_client_rate_limit_key(request)
        if auth_manager.check_pairing_rate_limit(ip):
            logger.warning(
                "[audit] pairing request rate limited path=/api/pairing/request ip=%s",
                ip,
                extra={
                    "audit_event": "pairing_rate_limited",
                    "path": "/api/pairing/request",
                    "ip": ip,
                },
            )
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
            ip = get_client_rate_limit_key(request)
            logger.warning(
                "[audit] pairing confirmation rejected path=/api/pairing/confirm ip=%s user=%s",
                ip,
                username,
                extra={
                    "audit_event": "pairing_confirm_rejected",
                    "path": "/api/pairing/confirm",
                    "ip": ip,
                    "username": username,
                },
            )
            return web.json_response({"error": "Invalid or expired code"}, status=400)
        # Handle second-screen registration
        if result["type"] == "second-screen":
            client_id = result["clientId"]
            device_token = result.get("token", "")
            pairing_entry: dict[str, Any] = {
                "pairedUser": username,
                "pairedAt": time.time(),
                "enabled": True,
            }
            if device_token:
                pairing_entry["pendingToken"] = device_token
            _second_screen_pairings[client_id] = pairing_entry
            _save_pairings()
            # Push notification to second screen via WebSocket (includes device token)
            ws = ws_bridge._ws_by_client.get(client_id)
            if ws and not ws.closed:
                pair_msg: dict[str, Any] = {
                    "type": "second_screen_paired",
                    "pairedUser": username,
                }
                if device_token:
                    pair_msg["deviceToken"] = device_token
                await ws.send_str(json.dumps(pair_msg))
            from vibr8_core.ring0_events import Ring0Event
            await ws_bridge.emit_ring0_event(Ring0Event(
                fields={
                    "type": "second_screen_paired",
                    "clientId": client_id[:8], "user": username,
                },
                source_client_id=client_id,
            ))
            logger.info("[pairing] Paired second-screen %s → user=%s", client_id[:8], username)
        else:
            logger.info("[pairing] Paired native device '%s' → user=%s", name, username)
        return web.json_response({"ok": True, "type": result["type"]})

    @routes.get("/api/pairing/status/{code}")
    async def pairing_status_unified(request: web.Request) -> web.Response:
        """Public — device polls to check if pairing was confirmed."""
        if not auth_manager or not auth_manager.enabled:
            return web.json_response({"error": "Auth not configured"}, status=501)
        ip = get_client_rate_limit_key(request)
        if auth_manager.check_pairing_brute_force(ip):
            logger.warning(
                "[audit] pairing status brute-force cooldown path=/api/pairing/status ip=%s",
                ip,
                extra={
                    "audit_event": "pairing_bruteforce_limited",
                    "path": "/api/pairing/status",
                    "ip": ip,
                },
            )
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

        # Check if target is an Android node (can't run sessions locally)
        android_registry = request.app.get("android_registry")
        android_node = android_registry.get_node(target_node) if android_registry and target_node else None

        if android_node:
            if backend in ("claude", "codex", "opencode", "hermes"):
                # Android node can't run Claude/Codex/OpenCode/Hermes — create on host with associatedNodeId
                body.pop("nodeId", None)
                body["associatedNodeId"] = target_node
            # computer-use sessions for Android nodes also run on host (with nodeId passed through)

        elif target_node and node_registry and backend != "computer-use":
            node = node_registry.get_node(target_node) or node_registry.get_node_by_name(target_node)
            if not node:
                return web.json_response({"error": f"Node '{target_node}' not found"}, status=404)
            if node.tunnel is not None:
                # Real remote node: sessions are created through its own
                # vended UI (/nodes/{id}/api/sessions/create), not the hub.
                return web.json_response(
                    {"error": f"Create sessions on node '{node.name}' via its own UI"},
                    status=400,
                )
            # else: the seeded "local" node (no tunnel) — fall through to
            # local session creation.

        try:
            if backend not in ("claude", "codex", "opencode", "hermes", "terminal", "computer-use"):
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
                    agentType=body.get("agentType"),
                    agentConfig=body.get("agentConfig"),
                )
                name = body.get("name") or session_names.generate_random_name()
                return web.json_response(await local_node_ops.launch_with_options(
                    opts=opts, backend_type="computer-use", name=name,
                ))

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

            # Derive an initial session name (directory basename when cwd is
            # known, otherwise a random one) before handing off.
            effective_cwd = opts.cwd or cwd or ""
            dir_name = os.path.basename(effective_cwd) if effective_cwd else ""
            name = body.get("name") or dir_name or session_names.generate_random_name()

            return web.json_response(await local_node_ops.launch_with_options(
                opts=opts,
                backend_type=backend,
                name=name,
                worktree_mapping=worktree_info,
            ))
        except Exception as e:
            logger.exception(f"[routes] Failed to create session: {e}")
            return web.json_response({"error": str(e)}, status=500)

    @routes.get("/api/agents")
    async def list_agents(request: web.Request) -> web.Response:
        from server.agent_registry import list_agent_types
        agents = []
        for info in list_agent_types():
            agents.append({
                "id": info.type_id,
                "name": info.display_name,
                "resourceType": info.resource_type,
                "configSchema": info.config_schema,
                "defaultConfig": info.default_config,
            })
        return web.json_response(agents)

    @routes.get("/api/sessions")
    async def list_sessions(request: web.Request) -> web.Response:
        requested_node = request.rel_url.query.get("nodeId", "")

        # Android node — return host sessions associated with this node
        android_registry = request.app.get("android_registry")
        if requested_node and android_registry and android_registry.get_node(requested_node):
            r0_id = ring0_manager.session_id if ring0_manager else None
            list_result = await local_node_ops.list_sessions()
            associated = []
            for s_dict in list_result.get("sessions", []):
                sid = s_dict.get("sessionId", "")
                # Sessions associated with this Android node (via bridge),
                # or CU sessions targeting it.
                bridge_session = ws_bridge._sessions.get(sid)
                associated_nid = bridge_session.associated_node_id if bridge_session else ""
                cu_node = s_dict.get("nodeId", "")
                if associated_nid != requested_node and cu_node != requested_node:
                    continue
                s_dict["associatedNodeId"] = requested_node
                associated.append(s_dict)
            associated.sort(key=lambda s: (0 if s.get("isRing0") else 1, -(s.get("lastPromptedAt") or s.get("createdAt") or 0)))
            return web.json_response(associated)

        # Remote nodes are reached through their own vended UI
        # (/nodes/{id}/api/sessions), not the hub API. Only the seeded
        # "local" node (no tunnel) is served here, by falling through to
        # local handling; any real or unknown node returns empty.
        if requested_node and node_registry:
            node = node_registry.get_node(requested_node)
            if not (node and node.tunnel is None):
                return web.json_response([])

        # Local node — pull sessions through NodeClient. NodeOperations
        # already does name/lastPromptedAt/isRing0/controlledBy/agentState
        # enrichment, so we only filter out CU sessions targeting other
        # nodes and graft terminal sessions on top here on the hub.
        r0_id = ring0_manager.session_id if ring0_manager else None
        list_result = await local_node_ops.list_sessions()
        enriched = []
        for s_dict in list_result.get("sessions", []):
            cu_node = s_dict.get("nodeId")
            if s_dict.get("backendType") == "computer-use" and cu_node and cu_node != "local":
                continue
            enriched.append(s_dict)
        # Include terminal sessions (hub-only concept; TerminalManager isn't
        # node-scoped).
        if terminal_manager:
            import time
            names = session_names.get_all_names()
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
        try:
            client, raw_sid, _ = _resolve_client(sid)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.get_session(session_id=raw_sid)
        if "error" in result:
            return web.json_response(result, status=404)
        return web.json_response(result)

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
        try:
            client, raw_sid, is_remote = _resolve_client(sid)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        if not is_remote and ring0_manager and ring0_manager.session_id == sid:
            return web.json_response({"error": "Ring0 session cannot be renamed"}, status=403)
        result = await client.rename_session(session_id=raw_sid, name=name)
        if "error" in result:
            return web.json_response({"error": result["error"]}, status=_status_for_error(result["error"]))
        # Broadcast to all browsers so other tabs/devices see the rename
        # (hub-only side effect — remote nodes broadcast independently)
        if not is_remote:
            await hub_browser_bridge.broadcast_name_update(sid, name, user_renamed=True)
        return web.json_response(result)

    @routes.post("/api/sessions/{id}/kill")
    async def kill_session(request: web.Request) -> web.Response:
        sid = request.match_info["id"]
        try:
            client, raw_sid, _ = _resolve_client(sid)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.kill_session(session_id=raw_sid)
        if "error" in result:
            return web.json_response({"error": result["error"]}, status=_status_for_error(result["error"]))
        if not result.get("ok"):
            return web.json_response({"error": "Session not found or already exited"}, status=404)
        return web.json_response({"ok": True})

    @routes.post("/api/sessions/{id}/relaunch")
    async def relaunch_session(request: web.Request) -> web.Response:
        sid = request.match_info["id"]
        try:
            client, raw_sid, _ = _resolve_client(sid)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.relaunch_session(session_id=raw_sid)
        if "error" in result:
            return web.json_response({"error": result["error"]}, status=_status_for_error(result["error"]))
        if not result.get("ok"):
            return web.json_response({"error": "Session not found"}, status=404)
        return web.json_response({"ok": True})

    @routes.delete("/api/sessions/{id}")
    async def delete_session(request: web.Request) -> web.Response:
        sid = request.match_info["id"]
        try:
            client, raw_sid, is_remote = _resolve_client(sid)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        # Terminal sessions live on the hub side (TerminalManager isn't
        # node-scoped); always clean them up regardless of session location.
        if terminal_manager:
            terminal_manager.close(sid)
        result = await client.delete_session(session_id=raw_sid)
        if is_remote:
            # Hub also drops its proxy-session state for remote-prefixed sids.
            await ws_bridge.close_session(sid)
        if "error" in result:
            return web.json_response({"error": result["error"]}, status=_status_for_error(result["error"]))
        return web.json_response(result)

    @routes.post("/api/sessions/{id}/archive")
    async def archive_session(request: web.Request) -> web.Response:
        sid = request.match_info["id"]
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            client, raw_sid, _ = _resolve_client(sid)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.archive_session(
            session_id=raw_sid, force=body.get("force"),
        )
        if "error" in result:
            return web.json_response({"error": result["error"]}, status=_status_for_error(result["error"]))
        return web.json_response(result)

    @routes.post("/api/sessions/{id}/unarchive")
    async def unarchive_session(request: web.Request) -> web.Response:
        sid = request.match_info["id"]
        try:
            client, raw_sid, _ = _resolve_client(sid)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.unarchive_session(session_id=raw_sid)
        if "error" in result:
            return web.json_response({"error": result["error"]}, status=_status_for_error(result["error"]))
        return web.json_response({"ok": True})

    # ── Message history archive ──────────────────────────────────────────

    @routes.get("/api/sessions/{id}/history-archive")
    async def get_session_history_archive(request: web.Request) -> web.Response:
        """Retrieve archived (rolled-off) messages with optional date filter and pagination."""
        sid = request.match_info["id"]
        date = request.query.get("date")
        offset = int(request.query.get("offset", "0"))
        limit = int(request.query.get("limit", "100"))
        try:
            client, raw_sid, _ = _resolve_client(sid)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        return web.json_response(await client.get_session_archive(
            session_id=raw_sid, date=date, offset=offset, limit=limit,
        ))

    @routes.get("/api/sessions/{id}/history-archive/dates")
    async def get_session_history_archive_dates(request: web.Request) -> web.Response:
        """List available archive dates with message counts and sizes."""
        sid = request.match_info["id"]
        try:
            client, raw_sid, _ = _resolve_client(sid)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        return web.json_response(await client.list_session_archive_dates(session_id=raw_sid))

    # ── Backends ─────────────────────────────────────────────────────────

    @routes.get("/api/backends")
    async def list_backends(request: web.Request) -> web.Response:
        """List CLI backends available on the target node.

        Pass `?nodeId=<id>` to query a specific node (defaults to the
        hub's self-node). The set of installed binaries is per-node.
        """
        node_id = request.query.get("nodeId", "")
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.list_backends()
        return web.json_response(result.get("backends", []))

    @routes.get("/api/backends/{id}/models")
    async def list_models(request: web.Request) -> web.Response:
        backend_id = request.match_info["id"]
        node_id = request.query.get("nodeId", "")
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.get_backend_models(backend_id=backend_id)
        if result.get("error"):
            status = 404 if result["error"] in ("Codex models cache not found", "Use frontend defaults for this backend") else 500
            return web.json_response({"error": result["error"]}, status=status)
        return web.json_response(result.get("models", []))

    # ── Filesystem browsing ──────────────────────────────────────────────

    @routes.get("/api/fs/list")
    async def fs_list(request: web.Request) -> web.Response:
        node_id = request.query.get("nodeId", "")
        path = request.query.get("path", "")
        try:
            client, _is_remote = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.fs_list(path=path)
        if "error" in result and not result.get("dirs"):
            return web.json_response(result, status=400)
        return web.json_response(result)

    @routes.get("/api/fs/home")
    async def fs_home(request: web.Request) -> web.Response:
        node_id = request.query.get("nodeId", "")
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        return web.json_response(await client.fs_home())

    @routes.get("/api/fs/tree")
    async def fs_tree(request: web.Request) -> web.Response:
        node_id = request.query.get("nodeId", "")
        path = request.query.get("path", "")
        if not path:
            return web.json_response({"error": "path required"}, status=400)
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        return web.json_response(await client.fs_tree(path=path))

    @routes.get("/api/fs/read")
    async def fs_read(request: web.Request) -> web.Response:
        node_id = request.query.get("nodeId", "")
        path = request.query.get("path", "")
        if not path:
            return web.json_response({"error": "path required"}, status=400)
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.fs_read(path=path)
        if "error" in result:
            status = 413 if "too large" in result["error"].lower() else 404
            return web.json_response(result, status=status)
        return web.json_response(result)

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
        node_id = body.get("nodeId", "") or request.query.get("nodeId", "")
        path = body.get("path")
        content = body.get("content")
        if not path or not isinstance(content, str):
            return web.json_response({"error": "path and content required"}, status=400)
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.fs_write(path=path, content=content)
        if "error" in result:
            return web.json_response(result, status=500)
        return web.json_response(result)

    @routes.post("/api/sessions/{session_id}/upload")
    async def session_upload(request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        reader = await request.multipart()
        field = await reader.next()
        if not field or field.name != "file":
            return web.json_response({"error": "No file field"}, status=400)
        filename = field.filename or f"upload-{int(time.time())}"
        buf = bytearray()
        while True:
            chunk = await field.read_chunk()
            if not chunk:
                break
            buf.extend(chunk)
        import base64
        content_b64 = base64.b64encode(bytes(buf)).decode("ascii")
        try:
            client, raw_sid, _ = _resolve_client(session_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.upload_to_session(
            session_id=raw_sid, filename=filename, content_b64=content_b64,
        )
        if result.get("error"):
            status = 404 if result["error"] == "Session not found" else 400
            return web.json_response({"error": result["error"]}, status=status)
        return web.json_response(result)

    @routes.post("/api/fs/mkdir")
    async def fs_mkdir(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        node_id = body.get("nodeId", "")
        path = body.get("path")
        if not path:
            return web.json_response({"error": "path required"}, status=400)
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.fs_mkdir(path=path)
        if "error" in result:
            return web.json_response(result, status=500)
        return web.json_response(result)

    @routes.post("/api/fs/rename")
    async def fs_rename(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        node_id = body.get("nodeId", "")
        old_path = body.get("oldPath")
        new_path = body.get("newPath")
        if not old_path or not new_path:
            return web.json_response({"error": "oldPath and newPath required"}, status=400)
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.fs_rename(old_path=old_path, new_path=new_path)
        if "error" in result:
            err = result["error"].lower()
            status = 404 if "not found" in err else (409 if "already exists" in err else 500)
            return web.json_response(result, status=status)
        return web.json_response(result)

    @routes.post("/api/fs/delete")
    async def fs_delete(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        node_id = body.get("nodeId", "")
        path = body.get("path")
        if not path:
            return web.json_response({"error": "path required"}, status=400)
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.fs_delete(path=path)
        if "error" in result:
            return web.json_response(result, status=404 if "not found" in result["error"].lower() else 500)
        return web.json_response(result)

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
        node_id = request.query.get("nodeId", "")
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.env_list()
        if "error" in result:
            return web.json_response(result, status=500)
        return web.json_response(result.get("envs", []))

    @routes.get("/api/envs/{slug}")
    async def get_env(request: web.Request) -> web.Response:
        node_id = request.query.get("nodeId", "")
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.env_get(slug=request.match_info["slug"])
        if "error" in result:
            return web.json_response(result, status=404)
        return web.json_response(result)

    @routes.post("/api/envs")
    async def create_env(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        node_id = body.get("nodeId", "")
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.env_create(
            name=body.get("name", ""),
            variables=body.get("variables", {}),
        )
        if "error" in result:
            return web.json_response(result, status=400)
        return web.json_response(result, status=201)

    @routes.put("/api/envs/{slug}")
    async def update_env(request: web.Request) -> web.Response:
        slug = request.match_info["slug"]
        try:
            body = await request.json()
        except Exception:
            body = {}
        node_id = body.get("nodeId", "")
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.env_update(
            slug=slug,
            name=body.get("name"),
            variables=body.get("variables"),
        )
        if "error" in result:
            status = 404 if "not found" in result["error"].lower() else 400
            return web.json_response(result, status=status)
        return web.json_response(result)

    @routes.delete("/api/envs/{slug}")
    async def delete_env(request: web.Request) -> web.Response:
        node_id = request.query.get("nodeId", "")
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.env_delete(slug=request.match_info["slug"])
        if "error" in result:
            return web.json_response(result, status=404)
        return web.json_response(result)

    # ── Git operations ───────────────────────────────────────────────────

    @routes.get("/api/git/repo-info")
    async def git_repo_info(request: web.Request) -> web.Response:
        node_id = request.query.get("nodeId", "")
        path = request.query.get("path")
        if not path:
            return web.json_response({"error": "path required"}, status=400)
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.git_repo_info(path=path)
        if "error" in result:
            return web.json_response(result, status=400)
        return web.json_response(result)

    @routes.get("/api/git/branches")
    async def git_branches(request: web.Request) -> web.Response:
        node_id = request.query.get("nodeId", "")
        repo_root = request.query.get("repoRoot")
        if not repo_root:
            return web.json_response({"error": "repoRoot required"}, status=400)
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.git_branches(repo_root=repo_root)
        if "error" in result:
            return web.json_response(result, status=500)
        return web.json_response(result.get("branches", []))

    @routes.get("/api/git/worktrees")
    async def git_worktrees(request: web.Request) -> web.Response:
        node_id = request.query.get("nodeId", "")
        repo_root = request.query.get("repoRoot")
        if not repo_root:
            return web.json_response({"error": "repoRoot required"}, status=400)
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.git_worktrees(repo_root=repo_root)
        if "error" in result:
            return web.json_response(result, status=500)
        return web.json_response(result.get("worktrees", []))

    @routes.post("/api/git/worktree")
    async def git_create_worktree(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        node_id = body.get("nodeId", "")
        repo_root = body.get("repoRoot")
        branch = body.get("branch")
        if not repo_root or not branch:
            return web.json_response({"error": "repoRoot and branch required"}, status=400)
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.git_create_worktree(
            repo_root=repo_root,
            branch=branch,
            base_branch=body.get("baseBranch"),
            create_branch=body.get("createBranch"),
        )
        if "error" in result:
            return web.json_response(result, status=500)
        return web.json_response(result)

    @routes.delete("/api/git/worktree")
    async def git_delete_worktree(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        node_id = body.get("nodeId", "")
        repo_root = body.get("repoRoot")
        worktree_path = body.get("worktreePath")
        if not repo_root or not worktree_path:
            return web.json_response({"error": "repoRoot and worktreePath required"}, status=400)
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.git_delete_worktree(
            repo_root=repo_root,
            worktree_path=worktree_path,
            force=body.get("force"),
        )
        return web.json_response(result)

    @routes.post("/api/git/fetch")
    async def git_fetch(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        node_id = body.get("nodeId", "")
        repo_root = body.get("repoRoot")
        if not repo_root:
            return web.json_response({"error": "repoRoot required"}, status=400)
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        return web.json_response(await client.git_fetch(repo_root=repo_root))

    @routes.post("/api/git/pull")
    async def git_pull(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        node_id = body.get("nodeId", "")
        cwd = body.get("cwd")
        if not cwd:
            return web.json_response({"error": "cwd required"}, status=400)
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        return web.json_response(await client.git_pull(cwd=cwd))

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

    @routes.post("/api/clients/{clientId}/speaker-gate")
    async def set_speaker_gate_by_client(request: web.Request) -> web.Response:
        if webrtc_manager is None:
            return web.json_response({"error": "WebRTC not available"}, status=501)
        client_id = request.match_info["clientId"]
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        speaker_name = body.get("speakerName")
        threshold = float(body.get("threshold", 0.45))
        tse_enabled = bool(body.get("tseEnabled", False))
        tse_threshold = float(body.get("tseThreshold", 0.35))
        username = _get_username(request)
        found = webrtc_manager.set_speaker_gate_for_client(
            client_id, speaker_name, threshold, username,
            tse_enabled=tse_enabled, tse_threshold=tse_threshold,
        )
        return web.json_response({
            "ok": True,
            "speakerName": speaker_name if found else None,
            "threshold": threshold,
            "tseEnabled": tse_enabled,
            "tseThreshold": tse_threshold,
        })

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
        try:
            client, raw_sid, _ = _resolve_client(sid)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.set_pen(session_id=raw_sid, controlled_by=controlled_by)
        if "error" in result:
            err = result["error"].lower()
            status = 404 if "not found" in err else (400 if "must be" in err else 500)
            return web.json_response(result, status=status)
        return web.json_response(result)

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
        tab_id = body.get("tabId", "")
        sdp = body.get("sdp")
        sdp_type = body.get("type", "offer")
        session_id = body.get("sessionId", "")
        playground = bool(body.get("playground", False))
        desktop = bool(body.get("desktop", False))
        desktop_role = body.get("desktopRole", "controller")
        profile_id = body.get("profileId")
        speaker_gate_name = body.get("speakerGateName") or None
        speaker_gate_threshold = float(body.get("speakerGateThreshold", 0.45))
        speaker_gate_tse_enabled = bool(body.get("speakerGateTseEnabled", False))
        speaker_gate_tse_threshold = float(body.get("speakerGateTseThreshold", 0.35))
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
                speaker_gate_name=speaker_gate_name,
                speaker_gate_threshold=speaker_gate_threshold,
                speaker_gate_tse_enabled=speaker_gate_tse_enabled,
                speaker_gate_tse_threshold=speaker_gate_tse_threshold,
                tab_id=tab_id,
            )
            return web.json_response(answer)
        except Exception as e:
            logger.error("[webrtc] Failed to handle offer: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    # ── Ring0 ─────────────────────────────────────────────────────────────

    @routes.get("/api/ring0/sessions")
    async def ring0_sessions(request: web.Request) -> web.Response:
        """List sessions — proxied for Ring0 MCP server (auth-exempt).

        Per-node Ring0: this lists only the local node's sessions. Cross-
        node session visibility was removed with the qualified-id
        machinery (docs/node-vended-ui.md, Phase 4)."""
        list_result = await local_node_ops.list_sessions()
        sessions = []
        for s in list_result.get("sessions", []):
            sid = s.get("sessionId", "")
            perm_count = (await local_node_ops.get_pending_permission_count(session_id=sid)).get("count", 0)
            perms = (await local_node_ops.get_pending_permissions(session_id=sid)).get("permissions", [])
            entry = {
                "sessionId": sid,
                "name": s.get("name") or "unnamed",
                "state": s.get("state"),
                "cwd": s.get("cwd"),
                "backendType": s.get("backendType"),
                "model": s.get("model"),
                "modelInfo": s.get("modelInfo"),
                "archived": s.get("archived"),
                "pendingPermissionCount": perm_count,
                "pendingPermissions": perms,
                "controlledBy": s.get("controlledBy", "ring0"),
            }
            if s.get("isRing0"):
                entry["isRing0"] = True
            sessions.append(entry)
        return web.json_response(sessions)

    @routes.get("/api/ring0/node-environment")
    async def ring0_node_environment(request: web.Request) -> web.Response:
        """Return environment metadata for the node this Ring0 runs on."""
        if self_node_name:
            node_name = self_node_name
        elif node_registry:
            node_name = node_registry.hub_name
        else:
            node_name = "local"
        info = {
            "nodeName": node_name,
            "platform": plat.system().lower(),
            "arch": plat.machine(),
            "hostname": plat.node(),
            "containerized": Path("/.dockerenv").exists(),
            "display": bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
        }
        return web.json_response(info)

    @routes.get("/api/ring0/status")
    async def ring0_status(request: web.Request) -> web.Response:
        node_id = request.query.get("nodeId", "")
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        return web.json_response(await client.ring0_status())

    @routes.post("/api/ring0/toggle")
    async def ring0_toggle(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        node_id = body.get("nodeId", "")
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.ring0_toggle(
            enabled=bool(body.get("enabled", False)),
            backend_type=body.get("backendType"),
        )
        if "error" in result:
            err = result["error"].lower()
            status = 409 if "different backend" in err else (400 if "invalid" in err or "must be" in err else 501)
            return web.json_response(result, status=status)
        return web.json_response(result)

    @routes.post("/api/ring0/switch-backend")
    async def ring0_switch_backend(request: web.Request) -> web.Response:
        """Switch Ring0 to a different backend. Kills session and starts fresh."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        node_id = body.get("nodeId", "")
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.ring0_switch_backend(
            backend_type=body.get("backendType", "").strip(),
        )
        if "error" in result:
            err = result["error"].lower()
            status = 400 if "must be" in err else 501
            return web.json_response(result, status=status)
        return web.json_response(result)

    @routes.post("/api/ring0/mute-events")
    async def ring0_mute_events(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        node_id = body.get("nodeId", "")
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.ring0_mute_events(muted=bool(body.get("muted", False)))
        if "error" in result:
            return web.json_response(result, status=501)
        return web.json_response(result)

    @routes.post("/api/ring0/switch-model")
    async def ring0_switch_model(request: web.Request) -> web.Response:
        """Switch Ring0 to a different model. Kills session and starts fresh."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        node_id = body.get("nodeId", "")
        model = body.get("model", "").strip()
        if not model:
            return web.json_response({"error": "model is required"}, status=400)
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.ring0_switch_model(model=model)
        if "error" in result:
            return web.json_response(result, status=501 if "not available" in result["error"].lower() else 400)
        return web.json_response(result)

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
        on_behalf_of = body.get("onBehalfOfClient", "")
        try:
            client, raw_sid, _ = _resolve_client(session_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.submit_message(
            session_id=raw_sid, content=message, source_client_id=on_behalf_of,
        )
        if "error" in result:
            return web.json_response(
                {"error": result["error"], "controlledBy": "user"}, status=409,
            )
        return web.json_response({"ok": True, "sessionId": raw_sid})

    @routes.post("/api/ring0/switch-ui")
    async def ring0_switch_ui(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        session_id = body.get("sessionId")
        if not session_id:
            return web.json_response({"error": "sessionId required"}, status=400)
        try:
            _client, raw_sid, _ = _resolve_client(session_id)
            # Confirm the session exists on the target node before broadcasting.
            sess = await _client.get_session(session_id=raw_sid)
            if "error" in sess:
                return web.json_response({"error": f"Session not found: {session_id}"}, status=404)
            resolved = raw_sid
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        client_id = body.get("clientId", "")
        if not client_id:
            client_id = hub_browser_bridge.get_ring0_prompt_client()
        if not client_id:
            return web.json_response({"error": "clientId required — specify which client to switch"}, status=400)
        sent = await hub_browser_bridge.broadcast_ring0_switch_ui(resolved, client_id=client_id)
        if not sent:
            return web.json_response({"error": f"Client {client_id} not connected"}, status=400)
        return web.json_response({"ok": True, "sessionId": resolved})

    @routes.get("/api/ring0/prompt-context")
    async def ring0_prompt_context(request: web.Request) -> web.Response:
        client_id = request.query.get("clientId", "")
        session_id = request.query.get("sessionId", "")
        if not client_id and not session_id:
            client_id = hub_browser_bridge.get_ring0_prompt_client()
        result = await node_ops.prompt_context(
            client_id=client_id,
            session_id=session_id,
        )
        return web.json_response(result)

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
        entry = hub_browser_bridge.set_client_metadata(client_id, updates)
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
        entry = hub_browser_bridge.register_device_info(client_id, device_info)
        result = dict(entry)
        result["clientId"] = client_id
        return web.json_response(result)

    # ── Ring0 API ────────────────────────────────────────────────────────

    @routes.get("/api/ring0/session-output/{id}")
    async def ring0_session_output(request: web.Request) -> web.Response:
        session_id = request.match_info["id"]
        try:
            client, raw_sid, _ = _resolve_client(session_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        msg_result = await client.get_message_history(session_id=raw_sid)
        if "error" in msg_result:
            return web.json_response({"error": msg_result["error"]}, status=404)
        perm_result = await client.get_pending_permissions(session_id=raw_sid)
        messages = msg_result.get("messages", [])
        pending = perm_result.get("permissions", [])
        formatted = _format_session_output(messages, pending)
        return web.json_response({"messages": messages, "pendingPermissions": pending, "formatted": formatted})

    @routes.get("/api/ring0/clients")
    async def ring0_clients(request: web.Request) -> web.Response:
        clients = hub_browser_bridge.get_all_clients()
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
        # Mirroring a session means displaying a node's live session over the
        # ui/v1 vended path (/nodes/{id}/ws/browser/{sid}). Tell the second
        # screen which node owns the session: the calling Ring0's node.
        # Node service tokens are named "node-{nodeId}"; any other caller
        # (local Ring0, UI) maps to the self-node.
        if method == "mirror_session" and isinstance(params, dict) and not params.get("nodeId"):
            auth_user = request.get("auth_user", "") or ""
            if auth_user.startswith("node-"):
                params["nodeId"] = auth_user[len("node-"):]
            elif node_registry:
                self_node = node_registry.get_node_by_name("self")
                if self_node:
                    params["nodeId"] = self_node.id
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
        try:
            client, raw_sid, _ = _resolve_client(session_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.respond_to_permission(
            session_id=raw_sid, request_id=request_id, behavior=behavior, message=message,
        )
        if "error" in result:
            return web.json_response(result, status=404)
        return web.json_response(result)

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
        try:
            client, raw_sid, _ = _resolve_client(session_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.interrupt(session_id=raw_sid)
        if not result.get("ok"):
            return web.json_response(
                {"error": f"Session {session_id} not found in bridge"}, status=404,
            )
        return web.json_response({"ok": True, "sessionId": raw_sid})

    @routes.post("/api/ring0/create-session")
    async def ring0_create_session(request: web.Request) -> web.Response:
        """Create a new session (used by Ring0 MCP, auth-exempt)."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        backend = body.get("backend", "claude")
        if backend not in ("claude", "codex", "opencode", "hermes", "computer-use"):
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
            return web.json_response(await local_node_ops.launch_with_options(
                opts=opts, backend_type=backend, name=name or None,
            ))
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
        # Migrate via NodeClient; rename_session() updates the session-state
        # half. The hub still drives the user-facing broadcast since the
        # browsers connect here, not to the node (this comes apart cleanly
        # in 4c-3 when the HubBrowserBridge split lands).
        try:
            client, raw_sid, is_remote = _resolve_client(sid)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.rename_session(session_id=raw_sid, name=name)
        if "error" in result:
            return web.json_response(result, status=_status_for_error(result["error"]))
        resolved = sid  # qualified id stays as caller supplied it
        if not is_remote:
            await hub_browser_bridge.broadcast_name_update(resolved, name, user_renamed=True)
        return web.json_response({"ok": True, "sessionId": resolved, "name": name})

    _ALLOWED_SESSION_MODES = {"plan", "acceptEdits", "bypassPermissions"}

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
        try:
            client, raw_sid, is_remote = _resolve_client(session_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.set_permission_mode(session_id=raw_sid, mode=mode)
        if "error" in result:
            return web.json_response(result, status=_status_for_error(result["error"]))
        # Hub-side browser broadcast — for hub-local sessions only; remote
        # nodes broadcast to their own bridge. (Will move to HubBrowserBridge
        # in 4c-3.)
        if not is_remote:
            session = ws_bridge.get_session(raw_sid)
            if session:
                await hub_browser_bridge._broadcast_to_browsers(
                    session, {"type": "session_update", "session": {"permissionMode": mode}},
                )
        return web.json_response({"ok": True, "sessionId": session_id, "mode": mode})

    @routes.get("/api/ring0/get-session-mode")
    async def ring0_get_session_mode(request: web.Request) -> web.Response:
        """Get a session's current permission mode (used by Ring0 MCP, auth-exempt)."""
        session_id = request.query.get("sessionId", "")
        if not session_id:
            return web.json_response({"error": "sessionId query param required"}, status=400)
        try:
            client, raw_sid, _ = _resolve_client(session_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        sess_result = await client.get_session(session_id=raw_sid)
        if "error" in sess_result:
            return web.json_response(
                {"error": f"Session not found: {session_id}"}, status=404,
            )
        mode = sess_result.get("permissionMode", "default")
        resolved = session_id
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

    # ── Scheduled Tasks & Queue ────────────────────────────────────────

    @routes.post("/api/ring0/tasks")
    async def ring0_create_task(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        name = body.get("name", "")
        prompt = body.get("prompt", "")
        if not name or not prompt:
            return web.json_response({"error": "name and prompt are required"}, status=400)
        node_id = body.get("nodeId", "")
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.scheduler_create_task(
            name=name, prompt=prompt,
            schedule=body.get("schedule", "daily"),
            priority=body.get("priority", "normal"),
            schedule_hour=int(body.get("schedule_hour", 9)),
            schedule_minute=int(body.get("schedule_minute", 0)),
            schedule_day=int(body.get("schedule_day", 0)),
            project_dir=body.get("project_dir", ""),
            model=body.get("model", ""),
            run_if_missed=bool(body.get("run_if_missed", True)),
        )
        if "error" in result:
            return web.json_response(result, status=_status_for_error(result["error"]))
        return web.json_response(result)

    @routes.get("/api/ring0/tasks")
    async def ring0_list_tasks(request: web.Request) -> web.Response:
        node_id = request.query.get("nodeId", "")
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.scheduler_list_tasks()
        return web.json_response(result.get("tasks", []))

    @routes.put("/api/ring0/tasks/{task_id}")
    async def ring0_update_task(request: web.Request) -> web.Response:
        task_id = request.match_info["task_id"]
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        node_id = body.pop("nodeId", "") if isinstance(body, dict) else ""
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.scheduler_update_task(task_id=task_id, **body)
        if "error" in result:
            return web.json_response(result, status=_status_for_error(result["error"]))
        return web.json_response(result)

    @routes.delete("/api/ring0/tasks/{task_id}")
    async def ring0_delete_task(request: web.Request) -> web.Response:
        task_id = request.match_info["task_id"]
        node_id = request.query.get("nodeId", "")
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.scheduler_delete_task(task_id=task_id)
        if "error" in result:
            return web.json_response(result, status=_status_for_error(result["error"]))
        return web.json_response(result)

    @routes.post("/api/ring0/tasks/{task_id}/run")
    async def ring0_run_task(request: web.Request) -> web.Response:
        task_id = request.match_info["task_id"]
        node_id = request.query.get("nodeId", "")
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.scheduler_run_task(task_id=task_id)
        if "error" in result:
            return web.json_response(result, status=400)
        return web.json_response(result)

    @routes.get("/api/ring0/queue")
    async def ring0_list_queue(request: web.Request) -> web.Response:
        node_id = request.query.get("nodeId", "")
        status_filter = request.query.get("status", "pending")
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.scheduler_list_queue(status=status_filter)
        return web.json_response(result.get("results", []))

    @routes.get("/api/ring0/queue/{result_id}")
    async def ring0_get_queue_item(request: web.Request) -> web.Response:
        result_id = request.match_info["result_id"]
        node_id = request.query.get("nodeId", "")
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.scheduler_get_queue_item(result_id=result_id)
        if "error" in result:
            return web.json_response(result, status=_status_for_error(result["error"]))
        return web.json_response(result)

    @routes.post("/api/ring0/queue/{result_id}/review")
    async def ring0_review_queue_item(request: web.Request) -> web.Response:
        result_id = request.match_info["result_id"]
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        node_id = body.get("nodeId", "")
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.scheduler_review_queue_item(
            result_id=result_id, action=body.get("action", "done"),
        )
        if "error" in result:
            return web.json_response(result, status=_status_for_error(result["error"]))
        return web.json_response(result)

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
                "promptTimeoutMs": defaults.prompt_timeout_ms,
                "isActive": True,
            })
        return web.json_response(profile)

    # ── Speaker Fingerprints ─────────────────────────────────────────────

    @routes.get("/api/voice/fingerprints")
    async def fingerprints_list(request: web.Request) -> web.Response:
        username = _get_username(request)
        fps = speaker_fingerprints.list_fingerprints(username)
        return web.json_response(fps)

    @routes.post("/api/voice/fingerprints")
    async def fingerprints_create(request: web.Request) -> web.Response:
        username = _get_username(request)
        body = await request.json()
        name = body.get("name", "Untitled")
        embedding = body.get("embedding")
        embedding_ws = body.get("embeddingWespeaker")
        label = body.get("label", "Default")
        if not embedding or not isinstance(embedding, list):
            return web.json_response({"error": "embedding required"}, status=400)
        if embedding_ws is not None and not isinstance(embedding_ws, list):
            return web.json_response({"error": "embeddingWespeaker must be a list"}, status=400)
        import base64
        audio = None
        audio_b64 = body.get("audio")
        if audio_b64:
            import numpy as np
            audio = np.frombuffer(base64.b64decode(audio_b64), dtype=np.int16)
        fp = speaker_fingerprints.create_fingerprint(
            username, name, embedding, label=label, audio=audio,
            embedding_wespeaker=embedding_ws,
        )
        embs = fp.get("embeddings", [])
        return web.json_response({
            "id": fp["id"], "name": fp["name"], "user": fp.get("user", username),
            "createdAt": fp.get("createdAt", 0),
            "embeddingCount": len(embs),
            "embeddingLabels": [e.get("label", "") for e in embs],
        }, status=201)

    @routes.delete("/api/voice/fingerprints/{id}")
    async def fingerprints_delete(request: web.Request) -> web.Response:
        username = _get_username(request)
        fp_id = request.match_info["id"]
        deleted = speaker_fingerprints.delete_fingerprint(username, fp_id)
        if not deleted:
            return web.json_response({"error": "Fingerprint not found"}, status=404)
        if webrtc_manager:
            webrtc_manager.refresh_speaker_gates(username)
        return web.json_response({"ok": True})

    @routes.get("/api/voice/fingerprints/active")
    async def fingerprints_active_get(request: web.Request) -> web.Response:
        username = _get_username(request)
        active = speaker_fingerprints.get_active(username)
        if not active:
            return web.json_response({
                "speakerName": None,
                "threshold": speaker_fingerprints.DEFAULT_THRESHOLD,
                "tseEnabled": False,
                "tseThreshold": speaker_fingerprints.DEFAULT_TSE_THRESHOLD,
            })
        return web.json_response(active)

    @routes.put("/api/voice/fingerprints/active")
    async def fingerprints_active_set(request: web.Request) -> web.Response:
        username = _get_username(request)
        body = await request.json()
        threshold = float(body.get("threshold", speaker_fingerprints.DEFAULT_THRESHOLD))
        tse_enabled = bool(body.get("tseEnabled", False))
        tse_threshold = float(body.get("tseThreshold", speaker_fingerprints.DEFAULT_TSE_THRESHOLD))
        speaker_name = body.get("speakerName")
        if speaker_name is None:
            speaker_fingerprints.clear_active(username)
            if webrtc_manager:
                webrtc_manager.refresh_speaker_gates(username)
            return web.json_response({
                "speakerName": None, "threshold": threshold,
                "tseEnabled": tse_enabled, "tseThreshold": tse_threshold,
            })
        config = speaker_fingerprints.set_active(
            username, speaker_name, threshold,
            tse_enabled=tse_enabled, tse_threshold=tse_threshold,
        )
        if not config:
            return web.json_response({"error": "Speaker profile not found"}, status=404)
        if webrtc_manager:
            webrtc_manager.refresh_speaker_gates(username)
        return web.json_response(config)

    @routes.get("/api/voice/tse/available")
    async def tse_available(request: web.Request) -> web.Response:
        """Report whether target-speaker extraction can be enabled.

        Requires CUDA + the WeSep BSRNN checkpoint on disk.
        """
        from server import tse_processor
        return web.json_response({"available": tse_processor.is_available()})

    @routes.post("/api/voice/fingerprints/refresh")
    async def fingerprints_refresh(request: web.Request) -> web.Response:
        """Re-apply speaker gate for the current user's active connections."""
        username = _get_username(request)
        if webrtc_manager:
            webrtc_manager.refresh_speaker_gates(username)
        return web.json_response({"ok": True})

    @routes.post("/api/voice/fingerprints/test")
    async def fingerprints_test(request: web.Request) -> web.Response:
        """Accept audio (float32 mono 16kHz), return similarity scores vs all fingerprints."""
        import numpy as np
        username = _get_username(request)
        body = await request.json()
        audio = body.get("audio")
        if not audio or not isinstance(audio, list):
            return web.json_response({"error": "audio required (list of float32 samples)"}, status=400)
        wav = np.array(audio, dtype=np.float32)
        loop = asyncio.get_event_loop()
        from server.speaker_model import embed, cosine_sim
        emb = await loop.run_in_executor(None, embed, wav)
        fps = speaker_fingerprints.list_fingerprints(username)
        scores = []
        for fp_meta in fps:
            fp = speaker_fingerprints.get_fingerprint(username, fp_meta["id"])
            if fp and fp.get("embeddings"):
                best_sim = -1.0
                best_label = ""
                for e in fp["embeddings"]:
                    if "embedding" not in e:
                        continue
                    sim = cosine_sim(emb, np.array(e["embedding"], dtype=np.float32))
                    if sim > best_sim:
                        best_sim = sim
                        best_label = e.get("label", "")
                scores.append({"id": fp["id"], "name": fp["name"], "similarity": round(best_sim, 4), "bestVoiceprint": best_label})
        return web.json_response({"scores": scores})

    @routes.post("/api/voice/fingerprints/{id}/embeddings")
    async def fingerprints_add_embedding(request: web.Request) -> web.Response:
        username = _get_username(request)
        profile_id = request.match_info["id"]
        body = await request.json()
        embedding = body.get("embedding")
        embedding_ws = body.get("embeddingWespeaker")
        label = body.get("label", "Default")
        if not embedding or not isinstance(embedding, list):
            return web.json_response({"error": "embedding required"}, status=400)
        if embedding_ws is not None and not isinstance(embedding_ws, list):
            return web.json_response({"error": "embeddingWespeaker must be a list"}, status=400)
        import base64
        audio = None
        audio_b64 = body.get("audio")
        if audio_b64:
            import numpy as np
            audio = np.frombuffer(base64.b64decode(audio_b64), dtype=np.int16)
        try:
            fp = speaker_fingerprints.add_embedding(
                username, profile_id, embedding, label=label, audio=audio,
                embedding_wespeaker=embedding_ws,
            )
        except ValueError:
            return web.json_response({"error": "Profile not found"}, status=404)
        embs = fp.get("embeddings", [])
        if webrtc_manager:
            webrtc_manager.refresh_speaker_gates(username)
        return web.json_response({
            "id": fp["id"], "name": fp["name"],
            "embeddingCount": len(embs),
            "embeddingLabels": [e.get("label", "") for e in embs],
        })

    @routes.delete("/api/voice/fingerprints/{id}/embeddings/{emb_id}")
    async def fingerprints_remove_embedding(request: web.Request) -> web.Response:
        username = _get_username(request)
        profile_id = request.match_info["id"]
        emb_id = request.match_info["emb_id"]
        result = speaker_fingerprints.remove_embedding(username, profile_id, emb_id)
        if webrtc_manager:
            webrtc_manager.refresh_speaker_gates(username)
        if result is None:
            return web.json_response({"ok": True, "deleted": True})
        embs = result.get("embeddings", [])
        return web.json_response({
            "ok": True, "deleted": False,
            "embeddingCount": len(embs),
            "embeddingLabels": [e.get("label", "") for e in embs],
        })

    # ── Artifacts ─────────────────────────────────────────────────────────

    @routes.get("/api/artifacts")
    async def artifacts_list(request: web.Request) -> web.Response:
        node_id = request.query.get("nodeId", "")
        session_id = request.query.get("sessionId")
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.artifacts_list(session_id=session_id)
        return web.json_response(result.get("artifacts", []))

    @routes.post("/api/artifacts")
    async def artifacts_create(request: web.Request) -> web.Response:
        username = _get_username(request)
        body = await request.json()
        node_id = body.get("nodeId", "")
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        artifact = await client.artifacts_create(username=username, data=body)
        return web.json_response(artifact)

    @routes.delete("/api/artifacts/{id}")
    async def artifacts_delete(request: web.Request) -> web.Response:
        artifact_id = request.match_info["id"]
        node_id = request.query.get("nodeId", "")
        try:
            client, _ = _resolve_node_client(node_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)
        result = await client.artifacts_delete(artifact_id=artifact_id)
        if "error" in result:
            return web.json_response(result, status=404)
        return web.json_response(result)

    @routes.get("/api/artifacts/{id}/content")
    async def artifacts_get_content(request: web.Request) -> web.Response:
        """Serve an artifact's raw bytes with the correct Content-Type.

        Decouples payload size from the MCP/websocket transport: large
        artifacts (audio, PDFs, images) ride this endpoint, not the
        artifact-list response. Artifacts are immutable, so the response is
        long-cached.
        """
        artifact_id = request.match_info["id"]
        node_id = request.query.get("nodeId", "")

        if node_id:
            # Remote artifact — fetch bytes via tunnel (base64-encoded).
            try:
                client, _ = _resolve_node_client(node_id)
            except Exception as e:
                return web.json_response({"error": str(e)}, status=503)
            result = await client.artifacts_read_content(artifact_id=artifact_id)
            if "error" in result:
                return web.Response(status=404, text="Not found")
            import base64
            body = base64.b64decode(result["contentBase64"])
            mime = result.get("contentType", "application/octet-stream")
            filename = result.get("filename")
            headers = {
                "Content-Type": mime,
                "Cache-Control": "public, max-age=31536000, immutable",
                "Content-Disposition": (
                    f'inline; filename="{filename}"' if filename else "inline"
                ),
            }
            return web.Response(body=body, headers=headers)

        result = artifacts.read_content(artifact_id)
        if result is None:
            return web.Response(status=404, text="Not found")
        body, mime, filename = result

        # Download-type artifacts get `attachment` so the browser saves them
        # instead of trying to render inline. The right MIME on top (e.g.
        # application/vnd.android.package-archive for an .apk) is what makes
        # mobile Chrome show the install prompt when the user taps.
        artifact = artifacts.get_artifact(artifact_id)
        disposition = "attachment" if artifact and artifacts.is_download_type(artifact.get("type", "")) else "inline"

        headers = {
            "Content-Type": mime,
            "Cache-Control": "public, max-age=31536000, immutable",
        }
        if filename:
            safe = filename.replace('"', "").replace("\r", "").replace("\n", "")
            headers["Content-Disposition"] = f'{disposition}; filename="{safe}"'
        else:
            headers["Content-Disposition"] = disposition
        return web.Response(body=body, headers=headers)

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
        ip = get_client_rate_limit_key(request)
        if auth_manager.check_pairing_rate_limit(ip):
            logger.warning(
                "[audit] pairing request rate limited path=/api/second-screen/pair-code ip=%s",
                ip,
                extra={
                    "audit_event": "pairing_rate_limited",
                    "path": "/api/second-screen/pair-code",
                    "ip": ip,
                },
            )
            return web.json_response({"error": "Too many requests"}, status=429)
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

        # Extract device token if auth created one
        device_token = result.get("token", "") if auth_manager and auth_manager.enabled else ""

        pairing_entry: dict[str, Any] = {
            "pairedUser": username,
            "pairedAt": time.time(),
            "enabled": True,
        }
        if device_token:
            pairing_entry["pendingToken"] = device_token  # single-use, cleared after delivery
        _second_screen_pairings[client_id] = pairing_entry
        _save_pairings()
        ws = ws_bridge._ws_by_client.get(client_id)
        if ws and not ws.closed:
            pair_msg: dict[str, Any] = {
                "type": "second_screen_paired",
                "pairedUser": username,
            }
            if device_token:
                pair_msg["deviceToken"] = device_token
            await ws.send_str(json.dumps(pair_msg))
        from vibr8_core.ring0_events import Ring0Event
        await ws_bridge.emit_ring0_event(Ring0Event(
            fields={
                "type": "second_screen_paired",
                "clientId": client_id[:8], "user": username,
            },
            source_client_id=client_id,
        ))
        logger.info(f"[second-screen] Paired {client_id[:8]} → user={username}")
        return web.json_response({"ok": True, "secondScreenClientId": client_id})

    @routes.get("/api/second-screen/status")
    async def second_screen_status(request: web.Request) -> web.Response:
        """Check pairing status for a client."""
        client_id = request.rel_url.query.get("clientId", "")
        if not client_id:
            return web.json_response({"error": "clientId required"}, status=400)

        # This status path polls by caller-held clientId, not a short pairing code.
        # Keep token delivery single-use without applying pairing-code brute-force limits.
        pairing = _second_screen_pairings.get(client_id)
        if pairing:
            resp: dict[str, Any] = {
                "paired": True,
                "role": "secondscreen",
                "pairedUser": pairing["pairedUser"],
                "pairedAt": pairing["pairedAt"],
            }
            # Default ambient view: the self-node's Ring0 conversation,
            # mirrored over the ui/v1 vended path. The screen falls back to
            # this whenever nothing is explicitly pushed/mirrored.
            try:
                r0 = await local_node_ops.ring0_status()
                if r0.get("enabled") and r0.get("sessionId"):
                    resp["ring0"] = {"nodeId": "local", "sessionId": r0["sessionId"]}
            except Exception:
                pass
            # Deliver device token (single-use: cleared after first delivery)
            pending_token = pairing.pop("pendingToken", None)
            if pending_token:
                resp["deviceToken"] = pending_token
                _save_pairings()
            return web.json_response(resp)

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
            from vibr8_core.ring0_events import Ring0Event
            await ws_bridge.emit_ring0_event(Ring0Event(
                fields={"type": "second_screen_unpaired", "clientId": client_id[:8]},
                source_client_id=client_id,
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
        from vibr8_core.ring0_events import Ring0Event
        action = "enabled" if enabled else "disabled"
        await ws_bridge.emit_ring0_event(Ring0Event(
            fields={"type": f"second_screen_{action}", "clientId": client_id[:8]},
            source_client_id=client_id,
        ))

        logger.info(f"[second-screen] {'Enabled' if enabled else 'Disabled'} {client_id[:8]}")
        return web.json_response({"ok": True, "enabled": bool(enabled)})

    # ── Nodes ─────────────────────────────────────────────────────────────

    @routes.post("/api/nodes/register")
    async def register_node(request: web.Request) -> web.Response:
        """Register a remote node (API-key auth, not cookie auth)."""
        if node_registry is None:
            return web.json_response({"error": "Node registry not available"}, status=503)
        ip = get_client_rate_limit_key(request)
        if check_rate_limit(
            node_register_rate,
            ip,
            limit=NODE_REGISTER_RATE_LIMIT,
            window=NODE_REGISTER_RATE_WINDOW,
        ):
            logger.warning(
                "[audit] node registration rate limited path=/api/nodes/register ip=%s",
                ip,
                extra={
                    "audit_event": "node_register_rate_limited",
                    "path": "/api/nodes/register",
                    "ip": ip,
                },
            )
            return web.json_response({"error": "Too many requests"}, status=429)
        body = await request.json()
        name = body.get("name", "").strip()
        api_key = body.get("apiKey", "")
        capabilities = body.get("capabilities", {})
        if not name or not api_key:
            return web.json_response({"error": "name and apiKey required"}, status=400)
        existing_node = node_registry.get_node_by_name(name)
        previous_api_key_id = existing_node.api_key_id if existing_node else ""
        try:
            node = node_registry.register(name, api_key, capabilities)
        except PermissionError as e:
            error_message = str(e)
            reason = (
                "bound_elsewhere"
                if error_message == "API key is already bound to another node"
                else "invalid_token"
            )
            public_error = (
                "Invalid API key for existing node"
                if node_registry.get_node_by_name(name)
                else "Invalid API key for new node"
            )
            logger.warning(
                "[audit] node registration rejected path=/api/nodes/register ip=%s node=%s reason=%s error=%s",
                ip,
                name[:32],
                reason,
                error_message,
                extra={
                    "audit_event": "node_register_rejected",
                    "path": "/api/nodes/register",
                    "ip": ip,
                    "node_name": name[:32],
                    "reason": reason,
                    "error_message": error_message,
                },
            )
            return web.json_response({"error": public_error}, status=403)
        logger.info(
            "[audit] node registered path=/api/nodes/register ip=%s node=%s node_id=%s",
            ip,
            node.name[:32],
            node.id[:8],
            extra={
                "audit_event": "node_registered",
                "path": "/api/nodes/register",
                "ip": ip,
                "node_name": node.name[:32],
                "node_id_prefix": node.id[:8],
                "api_key_id": node.api_key_id,
            },
        )
        if node.api_key_id and node.api_key_id != previous_api_key_id:
            logger.info(
                "[audit] node token bound path=/api/nodes/register ip=%s node=%s node_id=%s api_key_id=%s",
                ip,
                node.name[:32],
                node.id[:8],
                node.api_key_id,
                extra={
                    "audit_event": "node_token_bound",
                    "path": "/api/nodes/register",
                    "ip": ip,
                    "node_name": node.name[:32],
                    "node_id_prefix": node.id[:8],
                    "api_key_id": node.api_key_id,
                },
            )
        # Issue a service token so the node's Ring0 MCP can hit hub-side
        # client / second-screen / artifact endpoints over HTTP. Hub auth
        # accepts svc: tokens as a valid Bearer credential. When auth is
        # disabled on the hub there's nothing to issue and nothing to check.
        resp: dict[str, Any] = {"ok": True, "nodeId": node.id}
        if auth_manager and auth_manager.enabled:
            resp["serviceToken"] = auth_manager.create_service_token(f"node-{node.id}")
        return web.json_response(resp)

    @routes.get("/api/nodes")
    async def list_nodes(request: web.Request) -> web.Response:
        """List all registered nodes (including the local node)."""
        # Pull the hub-local session list through NodeClient so we don't
        # depend on the in-process launcher directly.
        local_sessions = (await local_node_ops.list_sessions()).get("sessions", []) if local_node_ops else []
        local_session_ids = [s.get("sessionId", "") for s in local_sessions if s.get("sessionId")]
        if node_registry is None:
            import platform as _platform
            hub_name = _platform.node() or "Local"
            return web.json_response([{"id": "local", "name": hub_name, "status": "online", "platform": "", "hostname": "", "sessionCount": len(local_session_ids), "ring0Enabled": ring0_manager.is_enabled if ring0_manager else False}])
        local = node_registry.local_node
        local.session_ids = local_session_ids
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
        """Switch a client's active node (Ring0-callable wrapper).

        Resolves the target client (from body `clientId` or the current
        Ring0 prompt context), updates that client's per-client active
        node, and broadcasts `ring0_switch_node` so the browser's UI
        flips to match. Per-client state is the source of truth — see
        `POST /api/clients/{client_id}/active-node` for the direct form.
        """
        if node_registry is None:
            return web.json_response({"error": "Node registry not available"}, status=503)
        node_id = request.match_info["node_id"]
        node = node_registry.get_node(node_id)
        if not node:
            return web.json_response({"error": f"Unknown node: {node_id}"}, status=404)
        name = node.name

        try:
            body = await request.json()
        except Exception:
            body = {}
        client_id = (body.get("clientId") if isinstance(body, dict) else "") or ""
        if not client_id:
            client_id = hub_browser_bridge.get_ring0_prompt_client()
        if client_id:
            hub_browser_bridge.set_client_active_node(client_id, node_id)
            await hub_browser_bridge.broadcast_ring0_switch_node(node_id, client_id=client_id)
            logger.info("[nodes] client %s active node → %r (%s)", client_id[:8], name, node_id[:8])
        else:
            logger.warning("[nodes] activate %s called without a client context — no-op", node_id[:8])

        return web.json_response({"ok": True, "nodeId": node_id, "name": name})

    @routes.post("/api/clients/{client_id}/active-node")
    async def set_client_active_node(request: web.Request) -> web.Response:
        """Set the active node for a specific browser client.

        Per-client (and eventually per-tab) active node replaces the
        hub-wide `node_registry.active_node_id`. Voice routing and UI
        operations should read from this map.
        """
        client_id = request.match_info["client_id"]
        body = await request.json() if request.content_length else {}
        node_id = (body.get("nodeId") if isinstance(body, dict) else "") or "local"
        hub_browser_bridge.set_client_active_node(client_id, node_id)
        logger.info("[nodes] client %s active node → %s", client_id[:8], node_id[:8] if node_id != "local" else "local")
        return web.json_response({"ok": True, "clientId": client_id, "nodeId": node_id})

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
    @routes.post("/api/nodes/tokens")
    async def generate_node_key(request: web.Request) -> web.Response:
        """Generate a new revocable node token for node registration."""
        if node_registry is None:
            return web.json_response({"error": "Node registry not available"}, status=503)
        body = await request.json() if request.content_length else {}
        name = body.get("name", "")
        username = request.get("auth_user")
        raw_key, entry = node_registry.generate_api_key(name, username=username)
        ip = request.remote or "unknown"
        path = request.path
        logger.info(
            "[audit] node token created path=%s ip=%s user=%s key_id=%s name=%s",
            path,
            ip,
            username or "",
            entry.id,
            entry.name[:32],
            extra={
                "audit_event": "node_token_created",
                "path": path,
                "ip": ip,
                "username": username or "",
                "api_key_id": entry.id,
                "token_name": entry.name[:32],
            },
        )
        return web.json_response({
            "apiKey": raw_key,
            "token": raw_key,
            "revocationNote": (
                "Revocation prevents new registrations with this token and blocks "
                "reconnect for nodes bound to it. Legacy nodes without a token binding "
                "retain stored-key behavior until re-registered."
            ),
            **entry.to_api_dict(),
        })

    @routes.get("/api/nodes/keys")
    @routes.get("/api/nodes/tokens")
    async def list_node_keys(request: web.Request) -> web.Response:
        """List issued node tokens for the authenticated user (no raw tokens)."""
        if node_registry is None:
            return web.json_response({"error": "Node registry not available"}, status=503)
        username = request.get("auth_user", None)
        keys = node_registry.list_api_keys(username=username)
        return web.json_response([k.to_api_dict() for k in keys])

    @routes.patch("/api/nodes/keys/{key_id}")
    @routes.patch("/api/nodes/tokens/{key_id}")
    async def update_node_key(request: web.Request) -> web.Response:
        """Update editable node token metadata."""
        if node_registry is None:
            return web.json_response({"error": "Node registry not available"}, status=503)
        key_id = request.match_info["key_id"]
        username = request.get("auth_user", None)
        ip = request.remote or "unknown"
        path = request.path
        try:
            body = await request.json() if request.content_length else {}
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        name = (body.get("name") if isinstance(body, dict) else None)
        if not isinstance(name, str) or not name.strip():
            return web.json_response({"error": "name required"}, status=400)
        name = name.strip()
        if len(name) > NODE_TOKEN_NAME_MAX_LENGTH:
            return web.json_response({"error": "name too long"}, status=400)

        entry = node_registry.update_api_key_metadata(
            key_id,
            username=username,
            name=name,
        )
        if entry:
            logger.info(
                "[audit] node token metadata updated path=%s ip=%s user=%s key_id=%s name=%s",
                path,
                ip,
                username or "",
                key_id,
                entry.name[:32],
                extra={
                    "audit_event": "node_token_metadata_updated",
                    "path": path,
                    "ip": ip,
                    "username": username or "",
                    "api_key_id": key_id,
                    "token_name": entry.name[:32],
                },
            )
            return web.json_response(entry.to_api_dict())

        logger.warning(
            "[audit] node token metadata update rejected path=%s ip=%s user=%s key_id=%s reason=not_found_or_forbidden",
            path,
            ip,
            username or "",
            key_id,
            extra={
                "audit_event": "node_token_metadata_update_rejected",
                "path": path,
                "ip": ip,
                "username": username or "",
                "api_key_id": key_id,
                "reason": "not_found_or_forbidden",
            },
        )
        return web.json_response({"error": "Key not found"}, status=404)

    @routes.delete("/api/nodes/keys/{key_id}")
    @routes.delete("/api/nodes/tokens/{key_id}")
    async def revoke_node_key(request: web.Request) -> web.Response:
        """Revoke a node token."""
        if node_registry is None:
            return web.json_response({"error": "Node registry not available"}, status=503)
        key_id = request.match_info["key_id"]
        username = request.get("auth_user", None)
        ip = request.remote or "unknown"
        path = request.path
        # The registry owns persisted revocation and offline state. Live
        # WebSocket closure stays in the route layer because aiohttp close()
        # is async and revoke_api_key() is intentionally synchronous.
        bound_ws = [
            node.ws
            for node in node_registry.get_nodes_by_api_key_id(key_id)
            if node.ws is not None
        ]
        if node_registry.revoke_api_key(key_id, username=username):
            for ws in bound_ws:
                if not ws.closed:
                    await ws.close(code=4001, message=b"Node token revoked")
            logger.warning(
                "[audit] node token revoked path=%s ip=%s user=%s key_id=%s closed_ws=%d",
                path,
                ip,
                username or "",
                key_id,
                len(bound_ws),
                extra={
                    "audit_event": "node_token_revoked",
                    "path": path,
                    "ip": ip,
                    "username": username or "",
                    "api_key_id": key_id,
                    "closed_ws_count": len(bound_ws),
                },
            )
            return web.json_response({"ok": True})
        logger.warning(
            "[audit] node token revocation rejected path=%s ip=%s user=%s key_id=%s reason=not_found_or_forbidden",
            path,
            ip,
            username or "",
            key_id,
            extra={
                "audit_event": "node_token_revoke_rejected",
                "path": path,
                "ip": ip,
                "username": username or "",
                "api_key_id": key_id,
                "reason": "not_found_or_forbidden",
            },
        )
        return web.json_response({"error": "Key not found"}, status=404)

    # ── Android Devices ──────────────────────────────────────────────────

    @routes.get("/api/android/devices")
    async def list_android_devices(request: web.Request) -> web.Response:
        """List all registered Android devices."""
        android_registry = request.app.get("android_registry")
        if not android_registry:
            return web.json_response({"error": "Android registry not available"}, status=503)
        return web.json_response([n.to_api_dict() for n in android_registry.get_all_nodes()])

    @routes.post("/api/android/devices")
    async def register_android_device(request: web.Request) -> web.Response:
        """Register a new Android device."""
        android_registry = request.app.get("android_registry")
        if not android_registry:
            return web.json_response({"error": "Android registry not available"}, status=503)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        name = body.get("name", "").strip()
        connection_mode = body.get("connectionMode", "usb")
        device_id = body.get("deviceId", "").strip()
        ip = body.get("ip", "").strip() or None
        port = body.get("port")
        if not name:
            return web.json_response({"error": "Name is required"}, status=400)
        if not device_id and connection_mode == "usb":
            return web.json_response({"error": "Device ID is required for USB"}, status=400)
        if not ip and connection_mode in ("ip", "mdns"):
            return web.json_response({"error": "IP address is required"}, status=400)

        # For IP/mDNS, construct device_id from ip:port
        if connection_mode in ("ip", "mdns") and ip:
            p = port or 5555
            device_id = f"{ip}:{p}"

        try:
            node = android_registry.register(name, connection_mode, device_id, ip=ip, port=port)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=409)

        # Try to connect and get device info
        await android_registry.check_device(node.id)
        return web.json_response(node.to_api_dict(), status=201)

    @routes.put("/api/android/devices/{node_id}")
    async def update_android_device(request: web.Request) -> web.Response:
        """Update an Android device's settings."""
        android_registry = request.app.get("android_registry")
        if not android_registry:
            return web.json_response({"error": "Android registry not available"}, status=503)
        node_id = request.match_info["node_id"]
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        node = android_registry.update(node_id, **body)
        if not node:
            return web.json_response({"error": "Device not found"}, status=404)
        return web.json_response(node.to_api_dict())

    @routes.delete("/api/android/devices/{node_id}")
    async def delete_android_device(request: web.Request) -> web.Response:
        """Remove an Android device."""
        android_registry = request.app.get("android_registry")
        if not android_registry:
            return web.json_response({"error": "Android registry not available"}, status=503)
        node_id = request.match_info["node_id"]
        if android_registry.unregister(node_id):
            return web.json_response({"ok": True})
        return web.json_response({"error": "Device not found"}, status=404)

    @routes.get("/api/android/discover")
    async def discover_android_devices(request: web.Request) -> web.Response:
        """Discover ADB devices (USB + mDNS) not yet registered."""
        android_registry = request.app.get("android_registry")
        if not android_registry:
            return web.json_response({"error": "Android registry not available"}, status=503)

        # USB devices
        usb_devices = await android_registry.discover_devices()

        # mDNS devices
        mdns = request.app.get("mdns_discovery")
        mdns_devices = []
        if mdns and mdns.available:
            registered_ips = set()
            for n in android_registry.get_all_nodes():
                if n.ip:
                    registered_ips.add(n.ip)
            for dev in mdns.get_discovered():
                if dev.ip not in registered_ips:
                    mdns_devices.append(dev.to_dict())

        return web.json_response({"usb": usb_devices, "mdns": mdns_devices})

    @routes.post("/api/android/connect")
    async def connect_android_device(request: web.Request) -> web.Response:
        """Connect to an Android device (for IP/mDNS nodes)."""
        android_registry = request.app.get("android_registry")
        if not android_registry:
            return web.json_response({"error": "Android registry not available"}, status=503)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        node_id = body.get("nodeId", "")
        node = android_registry.get_node(node_id)
        if not node:
            return web.json_response({"error": "Device not found"}, status=404)
        success = await android_registry.connect_device(node)
        return web.json_response({"ok": success, "status": node.status})

    @routes.post("/api/android/disconnect/{node_id}")
    async def disconnect_android_device(request: web.Request) -> web.Response:
        """Disconnect a wireless ADB device."""
        android_registry = request.app.get("android_registry")
        if not android_registry:
            return web.json_response({"error": "Android registry not available"}, status=503)
        node_id = request.match_info["node_id"]
        await android_registry.disconnect_device(node_id)
        return web.json_response({"ok": True})

    @routes.get("/api/android/devices/{node_id}/status")
    async def android_device_status(request: web.Request) -> web.Response:
        """Check connection health of an Android device."""
        android_registry = request.app.get("android_registry")
        if not android_registry:
            return web.json_response({"error": "Android registry not available"}, status=503)
        node_id = request.match_info["node_id"]
        node = android_registry.get_node(node_id)
        if not node:
            return web.json_response({"error": "Device not found"}, status=404)
        online = await android_registry.check_device(node_id)
        return web.json_response({"online": online, "status": node.status, "capabilities": node.capabilities})

    @routes.post("/api/android/devices/{node_id}/webrtc/offer")
    async def android_webrtc_offer(request: web.Request) -> web.Response:
        """Handle WebRTC offer for Android screen streaming.

        The hub creates a WebRTC peer connection with a ScrcpyVideoTrack
        as the video source, then returns the SDP answer.
        """
        android_registry = request.app.get("android_registry")
        if not android_registry:
            return web.json_response({"error": "Android registry not available"}, status=503)
        node_id = request.match_info["node_id"]
        node = android_registry.get_node(node_id)
        if not node:
            return web.json_response({"error": "Device not found"}, status=404)
        if node.status != "online":
            return web.json_response({"error": "Device is offline"}, status=503)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        sdp = body.get("sdp", "")
        sdp_type = body.get("sdpType", "offer")
        if not sdp:
            return web.json_response({"error": "SDP is required"}, status=400)

        try:
            from aiortc import RTCPeerConnection, RTCSessionDescription
            from server.scrcpy_client import ScrcpyClient
            from server.scrcpy_track import ScrcpyVideoTrack

            # Get or start scrcpy client for this device
            if not node.scrcpy_client:
                node.scrcpy_client = ScrcpyClient(
                    device_id=node.device_id,
                    max_size=1080,
                    max_fps=30,
                )
            if not node.scrcpy_client.running:
                await node.scrcpy_client.start()

            # Create WebRTC peer connection
            ice_servers = webrtc_manager.get_client_ice_servers() if webrtc_manager else []
            from aiortc import RTCConfiguration, RTCIceServer
            config = RTCConfiguration(
                iceServers=[RTCIceServer(urls=s.get("urls", []), username=s.get("username"), credential=s.get("credential"))
                            for s in ice_servers] if ice_servers else []
            )
            pc = RTCPeerConnection(configuration=config)

            # Add video track
            video_track = ScrcpyVideoTrack(node.scrcpy_client, target_fps=30)
            pc.addTrack(video_track)

            # Set remote description (browser's offer)
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=sdp_type))

            # Create and set local description (our answer)
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)

            return web.json_response({
                "sdp": pc.localDescription.sdp,
                "sdpType": pc.localDescription.type,
            })
        except Exception:
            logger.exception("[routes] Android WebRTC offer failed for %s", node_id)
            return web.json_response({"error": "WebRTC setup failed"}, status=500)

    # Also include Android nodes in the combined /api/nodes list
    @routes.get("/api/nodes/all")
    async def list_all_nodes(request: web.Request) -> web.Response:
        """List all nodes (desktop + Android) with their capabilities."""
        result: list[dict[str, Any]] = []

        # Local node
        if node_registry:
            local = node_registry.local_node
            local_dict = local.to_api_dict()
            local_dict["nodeType"] = "desktop"
            local_dict["canRunSessions"] = True
            local_dict["hasDisplay"] = True
            result.append(local_dict)

            # Remote desktop nodes
            for node in node_registry.get_all_nodes():
                if node.id == "local":
                    continue
                d = node.to_api_dict()
                d["nodeType"] = "desktop"
                d["canRunSessions"] = True
                d["hasDisplay"] = True
                result.append(d)

        # Android nodes
        android_registry = request.app.get("android_registry")
        if android_registry:
            for node in android_registry.get_all_nodes():
                result.append(node.to_api_dict())

        return web.json_response(result)

    return routes
