"""REST API routes — mirrors the Hono routes from the TypeScript version."""

from __future__ import annotations

import json
import logging
import os
import subprocess
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
) -> web.RouteTableDef:
    routes = web.RouteTableDef()

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

    # ── SDK Sessions ─────────────────────────────────────────────────────

    @routes.post("/api/sessions/create")
    async def create_session(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}

        try:
            backend = body.get("backend", "claude")
            if backend not in ("claude", "codex", "terminal"):
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
        sessions = launcher.list_sessions()
        names = session_names.get_all_names()
        enriched = []
        for s in sessions:
            s_dict = s.to_dict() if hasattr(s, "to_dict") else (s if isinstance(s, dict) else s.__dict__)
            s_dict["name"] = names.get(s_dict.get("sessionId", ""), s_dict.get("name"))
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
        session = launcher.get_session(sid)
        if not session:
            return web.json_response({"error": "Session not found"}, status=404)
        if ring0.session_id == sid:
            return web.json_response({"error": "Ring0 session cannot be renamed"}, status=403)
        session_names.set_name(sid, name, unique=False)
        return web.json_response({"ok": True, "name": name})

    @routes.post("/api/sessions/{id}/kill")
    async def kill_session(request: web.Request) -> web.Response:
        sid = request.match_info["id"]
        killed = await launcher.kill(sid)
        if not killed:
            return web.json_response({"error": "Session not found or already exited"}, status=404)
        return web.json_response({"ok": True})

    @routes.post("/api/sessions/{id}/relaunch")
    async def relaunch_session(request: web.Request) -> web.Response:
        sid = request.match_info["id"]
        ok = await launcher.relaunch(sid)
        if not ok:
            return web.json_response({"error": "Session not found"}, status=404)
        return web.json_response({"ok": True})

    @routes.delete("/api/sessions/{id}")
    async def delete_session(request: web.Request) -> web.Response:
        sid = request.match_info["id"]
        # Close terminal session if it exists
        if terminal_manager:
            terminal_manager.close(sid)
        await launcher.kill(sid)
        if webrtc_manager:
            await webrtc_manager.close_connection(sid)
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
        await launcher.kill(sid)
        if webrtc_manager:
            await webrtc_manager.close_connection(sid)
        worktree_result = _cleanup_worktree(sid, worktree_tracker, force=body.get("force"))
        launcher.set_archived(sid, True)
        session_store.set_archived(sid, True)
        return web.json_response({"ok": True, "worktree": worktree_result})

    @routes.post("/api/sessions/{id}/unarchive")
    async def unarchive_session(request: web.Request) -> web.Response:
        sid = request.match_info["id"]
        launcher.set_archived(sid, False)
        session_store.set_archived(sid, False)
        return web.json_response({"ok": True})

    # ── Backends ─────────────────────────────────────────────────────────

    @routes.get("/api/backends")
    async def list_backends(request: web.Request) -> web.Response:
        import shutil
        backends = []
        backends.append({"id": "claude", "name": "Claude Code", "available": shutil.which("claude") is not None})
        backends.append({"id": "codex", "name": "Codex", "available": shutil.which("codex") is not None})
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

    @routes.get("/api/sessions/{id}/guard")
    async def get_guard(request: web.Request) -> web.Response:
        if webrtc_manager is None:
            return web.json_response({"error": "WebRTC not available"}, status=501)
        sid = request.match_info["id"]
        enabled = webrtc_manager.is_guard_enabled(sid)
        return web.json_response({"enabled": enabled})

    @routes.post("/api/sessions/{id}/guard")
    async def set_guard(request: web.Request) -> web.Response:
        if webrtc_manager is None:
            return web.json_response({"error": "WebRTC not available"}, status=501)
        sid = request.match_info["id"]
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        enabled = bool(body.get("enabled", False))
        webrtc_manager.set_guard_enabled(sid, enabled)
        return web.json_response({"ok": True, "enabled": enabled})

    @routes.post("/api/sessions/{id}/tts-mute")
    async def set_tts_muted(request: web.Request) -> web.Response:
        if webrtc_manager is None:
            return web.json_response({"error": "WebRTC not available"}, status=501)
        sid = request.match_info["id"]
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        muted = bool(body.get("muted", False))
        webrtc_manager.set_tts_muted(sid, muted)
        return web.json_response({"ok": True, "muted": muted})

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

        session_id = body.get("sessionId")
        sdp = body.get("sdp")
        sdp_type = body.get("type", "offer")
        client_id = body.get("clientId", "")
        playground = bool(body.get("playground", False))
        profile_id = body.get("profileId")
        username = _get_username(request)

        if not session_id or not sdp:
            return web.json_response(
                {"error": "sessionId and sdp required"}, status=400
            )

        try:
            answer = await webrtc_manager.handle_offer(
                session_id, sdp, sdp_type,
                client_id=client_id,
                playground=playground,
                profile_id=profile_id,
                username=username,
            )
            return web.json_response(answer)
        except Exception as e:
            logger.error("[webrtc] Failed to handle offer: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    # ── Ring0 ─────────────────────────────────────────────────────────────

    @routes.get("/api/ring0/sessions")
    async def ring0_sessions(request: web.Request) -> web.Response:
        """List sessions — proxied for Ring0 MCP server (auth-exempt)."""
        sessions = []
        for sid in launcher.get_all_session_ids():
            info = launcher.get_session(sid)
            if not info:
                continue
            name = session_names.get_name(sid) or info.name or "unnamed"
            sessions.append({
                "sessionId": sid,
                "name": name,
                "state": info.state,
                "cwd": info.cwd,
                "backendType": info.backendType,
                "archived": info.archived,
                "pendingPermissions": ws_bridge.get_pending_permission_count(sid),
            })
        return web.json_response(sessions)

    @routes.get("/api/ring0/status")
    async def ring0_status(request: web.Request) -> web.Response:
        if ring0_manager is None:
            return web.json_response({"enabled": False, "sessionId": None})
        return web.json_response({
            "enabled": ring0_manager.is_enabled,
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
        await ws_bridge.submit_user_message(resolved, message)
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
        await ws_bridge.broadcast_ring0_switch_ui(resolved, client_id=client_id)
        return web.json_response({"ok": True, "sessionId": resolved})

    @routes.get("/api/ring0/session-output/{id}")
    async def ring0_session_output(request: web.Request) -> web.Response:
        session_id = request.match_info["id"]
        resolved = _resolve_session_id(session_id, launcher)
        if not resolved:
            return web.json_response({"error": f"Session not found: {session_id}"}, status=404)
        messages = ws_bridge.get_message_history(resolved)
        pending = ws_bridge.get_pending_permissions(resolved)
        return web.json_response({"messages": messages, "pendingPermissions": pending})

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
        interactive_methods = {"get_location", "send_notification", "read_clipboard", "write_clipboard"}
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
