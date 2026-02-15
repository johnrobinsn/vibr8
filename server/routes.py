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
from server.usage_limits import get_usage_limits
from server.cli_launcher import CliLauncher, LaunchOptions, WorktreeInfo
from server.worktree_tracker import WorktreeTracker, WorktreeMapping

if TYPE_CHECKING:
    from server.ws_bridge import WsBridge
    from server.session_store import SessionStore
    from server.webrtc import WebRTCManager

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
) -> web.RouteTableDef:
    routes = web.RouteTableDef()

    # ── SDK Sessions ─────────────────────────────────────────────────────

    @routes.post("/api/sessions/create")
    async def create_session(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}

        try:
            backend = body.get("backend", "claude")
            if backend not in ("claude", "codex"):
                return web.json_response({"error": f"Invalid backend: {backend}"}, status=400)

            # Resolve environment variables
            env_vars: dict[str, str] | None = body.get("env")
            env_slug = body.get("envSlug")
            if env_slug:
                companion_env = env_manager.get_env(env_slug)
                if companion_env:
                    logger.info(f"[routes] Injecting env \"{companion_env.name}\" ({len(companion_env.variables)} vars)")
                    env_vars = {**companion_env.variables, **(body.get("env") or {})}
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
            logger.error(f"[routes] Failed to create session: {e}")
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
        session_names.set_name(sid, name)
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

        if not session_id or not sdp:
            return web.json_response(
                {"error": "sessionId and sdp required"}, status=400
            )

        try:
            answer = await webrtc_manager.handle_offer(session_id, sdp, sdp_type)
            return web.json_response(answer)
        except Exception as e:
            logger.error("[webrtc] Failed to handle offer: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    return routes


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
