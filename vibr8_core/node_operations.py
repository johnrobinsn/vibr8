"""NodeOperations — canonical implementation of all node-scoped operations.

Every operation a hub or remote client can perform against a node — listing
sessions, sending messages, controlling Ring0, managing CLI subprocesses, etc.
— lives here as a single method. Both the remote node tunnel dispatcher and
(eventually) the hub's NodeClient layer call into this class. There is one
implementation per operation; the only thing that varies is the transport.

Method naming is snake_case to match Python convention. The remote-node
tunnel dispatcher converts incoming camelCase payload keys to snake_case
before calling. See node_agent.py:_dispatch_command.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional

from vibr8_core.cli_launcher import CliLauncher, LaunchOptions
from vibr8_core.ws_bridge import WsBridge
from vibr8_core.session_store import SessionStore
from vibr8_core.ring0 import Ring0Manager
from vibr8_core import session_names

logger = logging.getLogger(__name__)


SessionsChangedCallback = Callable[[], Awaitable[None]]


class NodeOperations:
    """Canonical node operations. Backend-agnostic, transport-agnostic."""

    def __init__(
        self,
        *,
        launcher: CliLauncher,
        bridge: WsBridge,
        store: SessionStore,
        ring0: Ring0Manager | None,
        desktop_webrtc: Any = None,
        default_backend: str = "claude",
        work_dir: str = "",
        on_sessions_changed: Optional[SessionsChangedCallback] = None,
    ) -> None:
        self._launcher = launcher
        self._bridge = bridge
        self._store = store
        self._ring0 = ring0
        self._desktop_webrtc = desktop_webrtc
        self._default_backend = default_backend
        self._work_dir = work_dir
        self._on_sessions_changed = on_sessions_changed

    async def _notify_sessions_changed(self) -> None:
        if self._on_sessions_changed:
            try:
                await self._on_sessions_changed()
            except Exception:
                logger.exception("on_sessions_changed callback failed")

    # ── Session listing & lifecycle ───────────────────────────────────────

    async def list_sessions(self) -> dict:
        sessions = []
        names = session_names.get_all_names()
        for s in self._launcher.list_sessions():
            s_dict = s.to_dict() if hasattr(s, "to_dict") else s.__dict__
            sid = s_dict.get("sessionId", "")
            s_dict["name"] = names.get(sid, s_dict.get("name"))
            lpa = self._bridge.get_last_prompted_at(sid)
            if lpa:
                s_dict["lastPromptedAt"] = lpa
            if self._ring0 and sid == self._ring0.session_id:
                s_dict["isRing0"] = True
            # Surface pen state so the UI can show the indicator for sessions
            # on remote nodes too.
            bridge_session = self._bridge._sessions.get(sid)
            if bridge_session is not None:
                s_dict["controlledBy"] = bridge_session.controlled_by
            sessions.append(s_dict)
        return {"sessions": sessions}

    async def set_pen(self, session_id: str = "", controlled_by: str = "ring0") -> dict:
        if controlled_by not in ("ring0", "user"):
            return {"error": "controlledBy must be 'ring0' or 'user'"}
        session = self._bridge._sessions.get(session_id)
        if not session:
            return {"error": "Session not found"}
        if controlled_by == "ring0":
            asyncio.ensure_future(self._bridge._release_pen(session))
        else:
            import time as _time
            session.controlled_by = "user"
            session.pen_taken_at = _time.time()
            session.state["controlledBy"] = "user"
            self._bridge._schedule_pen_release(session)
            asyncio.ensure_future(self._bridge._broadcast_to_browsers(
                session, {"type": "session_update", "session": {"controlledBy": "user"}},
            ))
        return {"ok": True, "controlledBy": controlled_by}

    async def create_session(self, options: dict | None = None) -> dict:
        options = options or {}
        opts = LaunchOptions(
            model=options.get("model"),
            permissionMode=options.get("permissionMode"),
            cwd=options.get("cwd") or self._work_dir or None,
            backendType=options.get("backend", self._default_backend),
        )
        info = self._launcher.launch(opts)
        result = info.to_dict() if hasattr(info, "to_dict") else info.__dict__
        await self._notify_sessions_changed()
        return result

    async def kill_session(self, session_id: str = "") -> dict:
        killed = await self._launcher.kill(session_id)
        await self._notify_sessions_changed()
        return {"ok": killed}

    async def relaunch_session(self, session_id: str = "") -> dict:
        ok = await self._launcher.relaunch(session_id)
        await self._notify_sessions_changed()
        return {"ok": ok}

    async def delete_session(self, session_id: str = "") -> dict:
        await self._launcher.kill(session_id)
        self._launcher.remove_session(session_id)
        await self._bridge.close_session(session_id)
        await self._notify_sessions_changed()
        return {"ok": True}

    async def archive_session(self, session_id: str = "") -> dict:
        await self._launcher.kill(session_id)
        self._launcher.set_archived(session_id, True)
        await self._notify_sessions_changed()
        return {"ok": True}

    async def unarchive_session(self, session_id: str = "") -> dict:
        self._launcher.set_archived(session_id, False)
        await self._notify_sessions_changed()
        return {"ok": True}

    async def rename_session(self, session_id: str = "", name: str = "") -> dict:
        name = (name or "").strip()
        if not name:
            return {"error": "name is required"}
        session_names.set_name(session_id, name, unique=False)
        await self._notify_sessions_changed()
        return {"ok": True, "name": name}

    # ── Messaging & permission ────────────────────────────────────────────

    async def submit_message(
        self,
        session_id: str = "",
        content: str = "",
        source_client_id: str = "",
    ) -> dict:
        err = await self._bridge.submit_user_message(
            session_id, content, source_client_id=source_client_id,
        )
        if err:
            return {"error": err}
        return {"ok": True}

    async def cli_input(self, session_id: str = "", message: dict | None = None) -> dict:
        """Forward raw CLI input (NDJSON message) to local session."""
        session = self._bridge._sessions.get(session_id)
        if not session:
            return {"error": f"Session {session_id} not found"}
        ndjson = json.dumps(message or {})
        await self._bridge._send_to_cli(session, ndjson)
        return {"ok": True}

    async def interrupt(self, session_id: str = "") -> dict:
        ok = self._bridge.interrupt_session(session_id)
        return {"ok": ok}

    async def browser_message(
        self,
        session_id: str = "",
        message: dict | None = None,
        source_client_id: str = "",
    ) -> dict:
        session = self._bridge._sessions.get(session_id)
        if not session:
            return {"error": f"Session {session_id} not found"}
        await self._bridge._route_browser_message(
            session, message or {}, ws=None, source_client_id=source_client_id,
        )
        return {"ok": True}

    async def get_session_output(self, session_id: str = "") -> dict:
        session = self._bridge._sessions.get(session_id)
        if not session:
            return {"error": f"Session {session_id} not found"}
        return {"messages": session.message_history[-500:]}

    async def get_session(self, session_id: str = "") -> dict:
        info = self._launcher.get_session(session_id)
        if not info:
            return {"error": "Session not found"}
        d = info.to_dict() if hasattr(info, "to_dict") else (info if isinstance(info, dict) else info.__dict__)
        names = session_names.get_all_names()
        if session_id in names:
            d["name"] = names[session_id]
        if self._ring0 and session_id == self._ring0.session_id:
            d["isRing0"] = True
        bridge_session = self._bridge._sessions.get(session_id)
        if bridge_session is not None:
            d["controlledBy"] = bridge_session.controlled_by
        return d

    async def get_message_history(self, session_id: str = "", limit: int = 500) -> dict:
        session = self._bridge._sessions.get(session_id)
        if not session:
            return {"error": "Session not found"}
        return {"messages": session.message_history[-limit:]}

    async def get_pending_permissions(self, session_id: str = "") -> dict:
        return {"permissions": self._bridge.get_pending_permissions(session_id)}

    async def get_pending_permission_count(self, session_id: str = "") -> dict:
        return {"count": self._bridge.get_pending_permission_count(session_id)}

    async def respond_to_permission(
        self,
        session_id: str = "",
        request_id: str = "",
        behavior: str = "",
        message: str = "",
    ) -> dict:
        ok = await self._bridge.respond_to_permission(
            session_id, request_id, behavior, message,
        )
        if not ok:
            return {"error": f"Permission {request_id} not found"}
        return {"ok": True}

    async def get_session_archive(
        self,
        session_id: str = "",
        date: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict:
        messages, total = self._store.load_archive(
            session_id, date=date, offset=offset, limit=limit,
        )
        return {"messages": messages, "total": total, "offset": offset, "limit": limit}

    async def list_session_archive_dates(self, session_id: str = "") -> dict:
        return {"dates": self._store.list_archive_dates(session_id)}

    async def set_archived(self, session_id: str = "", archived: bool = True) -> dict:
        """Persist the archived flag in the session store (separate from
        the launcher-level archived flag handled by archive_session)."""
        self._store.set_archived(session_id, bool(archived))
        return {"ok": True}

    async def set_permission_mode(self, session_id: str = "", mode: str = "") -> dict:
        session = self._bridge._sessions.get(session_id)
        if not session:
            return {"error": f"Session {session_id} not found"}
        self._bridge._handle_set_permission_mode(session, mode)
        return {"ok": True}

    async def respond_permission(self, session_id: str = "", **payload) -> dict:
        session = self._bridge._sessions.get(session_id)
        if not session:
            return {"error": f"Session {session_id} not found"}
        # _handle_permission_response expects the original camelCase payload
        msg = {"sessionId": session_id, **_snake_to_camel_dict(payload)}
        await self._bridge._handle_permission_response(session, msg)
        return {"ok": True}

    # ── Ring0 ─────────────────────────────────────────────────────────────

    async def ring0_input(self, text: str = "", source_client_id: str = "") -> dict:
        if not self._ring0 or not self._ring0.is_enabled:
            return {"error": "Ring0 not enabled on this node"}
        if not text:
            return {"error": "Empty text"}
        r0sid = self._ring0.session_id
        if not r0sid:
            r0sid = await self._ring0.ensure_session(
                self._launcher, self._bridge, backend_type=self._default_backend,
            )
        if not r0sid:
            return {"error": "Ring0 session not available"}
        await self._bridge.submit_user_message(
            r0sid, text, source_client_id=source_client_id,
        )
        return {"ok": True}

    # ── Filesystem ────────────────────────────────────────────────────────
    #
    # Each method operates on this node's local filesystem. The hub forwards
    # /api/fs/* calls here when ?nodeId= targets a remote node.

    async def fs_list(self, path: str = "") -> dict:
        from pathlib import Path
        base = Path(path).resolve() if path else Path.home()
        try:
            dirs = []
            for entry in sorted(base.iterdir(), key=lambda e: e.name):
                if entry.is_dir() and not entry.name.startswith("."):
                    dirs.append({"name": entry.name, "path": str(entry)})
            return {"path": str(base), "dirs": dirs, "home": str(Path.home())}
        except Exception as e:
            return {"error": f"Cannot read directory: {e}", "path": str(base), "dirs": [], "home": str(Path.home())}

    async def fs_home(self) -> dict:
        import os
        from pathlib import Path
        return {"home": str(Path.home()), "cwd": os.getcwd()}

    async def fs_tree(self, path: str = "", max_depth: int = 10) -> dict:
        from pathlib import Path
        if not path:
            return {"error": "path required"}
        base = Path(path).resolve()

        def build(d: Path, depth: int) -> list[dict]:
            if depth > max_depth:
                return []
            try:
                nodes: list[dict] = []
                for entry in sorted(d.iterdir(), key=lambda e: (not e.is_dir(), e.name)):
                    if entry.name.startswith(".") or entry.name == "node_modules":
                        continue
                    if entry.is_dir():
                        nodes.append({"name": entry.name, "path": str(entry), "type": "directory", "children": build(entry, depth + 1)})
                    elif entry.is_file():
                        nodes.append({"name": entry.name, "path": str(entry), "type": "file"})
                return nodes
            except Exception:
                return []

        return {"path": str(base), "tree": build(base, 0)}

    async def fs_read(self, path: str = "", max_bytes: int = 2 * 1024 * 1024) -> dict:
        from pathlib import Path
        if not path:
            return {"error": "path required"}
        p = Path(path).resolve()
        try:
            if p.stat().st_size > max_bytes:
                return {"error": f"File too large (>{max_bytes} bytes)"}
            return {"path": str(p), "content": p.read_text()}
        except Exception as e:
            return {"error": str(e)}

    async def fs_write(self, path: str = "", content: str = "") -> dict:
        from pathlib import Path
        if not path:
            return {"error": "path required"}
        p = Path(path).resolve()
        try:
            p.write_text(content)
            return {"ok": True, "path": str(p)}
        except Exception as e:
            return {"error": str(e)}

    async def fs_mkdir(self, path: str = "") -> dict:
        from pathlib import Path
        if not path:
            return {"error": "path required"}
        p = Path(path).resolve()
        try:
            p.mkdir(parents=True, exist_ok=True)
            return {"ok": True, "path": str(p)}
        except Exception as e:
            return {"error": str(e)}

    async def fs_rename(self, old_path: str = "", new_path: str = "") -> dict:
        from pathlib import Path
        if not old_path or not new_path:
            return {"error": "oldPath and newPath required"}
        src = Path(old_path).resolve()
        dst = Path(new_path).resolve()
        if not src.exists():
            return {"error": "source not found"}
        if dst.exists():
            return {"error": "destination already exists"}
        try:
            src.rename(dst)
            return {"ok": True}
        except Exception as e:
            return {"error": str(e)}

    async def fs_delete(self, path: str = "") -> dict:
        from pathlib import Path
        import shutil
        if not path:
            return {"error": "path required"}
        p = Path(path).resolve()
        if not p.exists():
            return {"error": "not found"}
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            return {"ok": True}
        except Exception as e:
            return {"error": str(e)}

    # ── Git ───────────────────────────────────────────────────────────────
    #
    # Each operation runs against this node's working trees. The hub forwards
    # /api/git/* calls here when ?nodeId= targets a remote node.

    async def git_repo_info(self, path: str = "") -> dict:
        from vibr8_core import git_utils
        if not path:
            return {"error": "path required"}
        info = git_utils.get_repo_info(path)
        if not info:
            return {"error": "Not a git repository"}
        return _to_camel_dict(info)

    async def git_branches(self, repo_root: str = "") -> dict:
        from vibr8_core import git_utils
        if not repo_root:
            return {"error": "repoRoot required"}
        try:
            branches = git_utils.list_branches(repo_root)
            return {"branches": [_to_camel_dict(b) for b in branches]}
        except Exception as e:
            return {"error": str(e)}

    async def git_worktrees(self, repo_root: str = "") -> dict:
        from vibr8_core import git_utils
        if not repo_root:
            return {"error": "repoRoot required"}
        try:
            wts = git_utils.list_worktrees(repo_root)
            return {"worktrees": [_to_camel_dict(w) for w in wts]}
        except Exception as e:
            return {"error": str(e)}

    async def git_create_worktree(
        self,
        repo_root: str = "",
        branch: str = "",
        base_branch: str | None = None,
        create_branch: bool | None = None,
    ) -> dict:
        from vibr8_core import git_utils
        if not repo_root or not branch:
            return {"error": "repoRoot and branch required"}
        try:
            result = git_utils.ensure_worktree(
                repo_root, branch, base_branch=base_branch, create_branch=create_branch,
            )
            return _to_camel_dict(result)
        except Exception as e:
            return {"error": str(e)}

    async def git_delete_worktree(
        self,
        repo_root: str = "",
        worktree_path: str = "",
        force: bool | None = None,
    ) -> dict:
        from vibr8_core import git_utils
        if not repo_root or not worktree_path:
            return {"error": "repoRoot and worktreePath required"}
        return git_utils.remove_worktree(repo_root, worktree_path, force=force)

    async def git_fetch(self, repo_root: str = "") -> dict:
        from vibr8_core import git_utils
        if not repo_root:
            return {"error": "repoRoot required"}
        return git_utils.git_fetch(repo_root)

    async def git_pull(self, cwd: str = "") -> dict:
        import subprocess
        from vibr8_core import git_utils
        if not cwd:
            return {"error": "cwd required"}
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
        return {**result, "git_ahead": git_ahead, "git_behind": git_behind}

    # ── Environments ──────────────────────────────────────────────────────
    #
    # Each node reads/writes its own ~/.vibr8/envs/ directory (per-node since
    # remote nodes use isolated data dirs).

    async def env_list(self) -> dict:
        from vibr8_core import env_manager
        try:
            envs = env_manager.list_envs()
            return {"envs": [e.to_dict() for e in envs]}
        except Exception as e:
            return {"error": str(e)}

    async def env_get(self, slug: str = "") -> dict:
        from vibr8_core import env_manager
        env = env_manager.get_env(slug)
        if not env:
            return {"error": "Environment not found"}
        return env.to_dict()

    async def env_create(self, name: str = "", variables: dict | None = None) -> dict:
        from vibr8_core import env_manager
        try:
            env = env_manager.create_env(name, variables or {})
            return env.to_dict()
        except Exception as e:
            return {"error": str(e)}

    async def env_update(
        self, slug: str = "", name: str | None = None, variables: dict | None = None,
    ) -> dict:
        from vibr8_core import env_manager
        try:
            env = env_manager.update_env(slug, name=name, variables=variables)
            if not env:
                return {"error": "Environment not found"}
            return env.to_dict()
        except Exception as e:
            return {"error": str(e)}

    async def env_delete(self, slug: str = "") -> dict:
        from vibr8_core import env_manager
        deleted = env_manager.delete_env(slug)
        if not deleted:
            return {"error": "Environment not found"}
        return {"ok": True}

    # ── Artifacts ─────────────────────────────────────────────────────────
    #
    # Each node owns its own ~/.vibr8/artifacts/. The hub UI surfaces them
    # per-node.

    async def artifacts_list(self, session_id: str | None = None) -> dict:
        from vibr8_core import artifacts
        return {"artifacts": artifacts.list_artifacts(session_id)}

    async def artifacts_create(self, username: str = "", data: dict | None = None) -> dict:
        from vibr8_core import artifacts
        artifact = artifacts.create_artifact(username, data or {})
        await self._bridge.broadcast_to_all_browsers({"type": "artifacts_changed"})
        return artifact

    async def artifacts_delete(self, artifact_id: str = "") -> dict:
        from vibr8_core import artifacts
        deleted = artifacts.delete_artifact(artifact_id)
        if not deleted:
            return {"error": "Not found"}
        await self._bridge.broadcast_to_all_browsers({"type": "artifacts_changed"})
        return {"ok": True}

    async def artifacts_read_content(self, artifact_id: str = "") -> dict:
        """Read artifact bytes; encoded as base64 since the tunnel is JSON."""
        from vibr8_core import artifacts
        import base64
        result = artifacts.read_content(artifact_id)
        if result is None:
            return {"error": "Not found"}
        data, content_type, filename = result
        return {
            "contentBase64": base64.b64encode(data).decode("ascii"),
            "contentType": content_type,
            "filename": filename,
        }

    # ── Ring0 control ─────────────────────────────────────────────────────

    async def ring0_status(self) -> dict:
        if self._ring0 is None:
            return {"enabled": False, "sessionId": None}
        return {
            "enabled": self._ring0.is_enabled,
            "eventsMuted": getattr(self._ring0, "events_muted", False),
            "sessionId": self._ring0.session_id,
            "model": self._ring0.model,
            "backendType": self._ring0.backend_type,
        }

    async def ring0_toggle(
        self, enabled: bool = False, backend_type: str | None = None,
    ) -> dict:
        if self._ring0 is None:
            return {"error": "Ring0 not available"}
        if backend_type:
            if backend_type not in ("claude", "codex", "opencode", "hermes"):
                return {"error": f"Invalid backendType: {backend_type}"}
            running = self._launcher.is_alive(self._ring0.session_id)
            if running and backend_type != self._ring0.backend_type:
                return {
                    "error": "Ring0 is running with a different backend; use ring0_switch_backend",
                    "current": self._ring0.backend_type,
                }
            try:
                self._ring0.set_backend_type(backend_type)
            except ValueError as e:
                return {"error": str(e)}
        self._ring0.toggle(bool(enabled))
        if enabled:
            session_id = await self._ring0.ensure_session(self._launcher, self._bridge)
            return {
                "ok": True, "enabled": True, "sessionId": session_id,
                "backendType": self._ring0.backend_type,
            }
        return {
            "ok": True, "enabled": False, "sessionId": self._ring0.session_id,
            "backendType": self._ring0.backend_type,
        }

    async def ring0_switch_backend(self, backend_type: str = "") -> dict:
        if self._ring0 is None:
            return {"error": "Ring0 not available"}
        if backend_type not in ("claude", "codex", "opencode", "hermes"):
            return {"error": "backendType must be one of claude, codex, opencode, hermes"}
        previous = self._ring0.backend_type
        if backend_type == previous:
            return {
                "ok": True, "backendType": backend_type, "previous": previous,
                "changed": False,
            }

        async def _do_switch() -> None:
            await asyncio.sleep(1.5)
            self._ring0.set_backend_type(backend_type)
            await self._launcher.kill(self._ring0.session_id)
            await self._ring0.ensure_session(self._launcher, self._bridge)

        asyncio.create_task(_do_switch())
        return {
            "ok": True, "backendType": backend_type, "previous": previous, "changed": True,
        }

    async def ring0_switch_model(self, model: str = "") -> dict:
        if self._ring0 is None:
            return {"error": "Ring0 not available"}
        model = (model or "").strip()
        if not model:
            return {"error": "model is required"}

        backend_aliases: dict[str, dict[str, str]] = {
            "claude": {
                "haiku": "claude-haiku-4-5-20251001",
                "sonnet": "claude-sonnet-4-6",
                "opus": "claude-opus-4-6",
            },
            "hermes": {
                "opus": "claude-opus-4-20250514",
                "sonnet": "claude-sonnet-4-20250514",
                "gpt-5": "gpt-5.5",
                "gpt5": "gpt-5.5",
                "gpt-4": "gpt-4o",
                "gpt4": "gpt-4o",
                "deepseek": "deepseek-r1",
            },
            "codex": {
                "codex": "gpt-5.3-codex",
                "max": "gpt-5.1-codex-max",
                "mini": "gpt-5.1-codex-mini",
            },
            "opencode": {
                "gemini": "google/gemini-2.5-pro",
                "flash": "google/gemini-2.5-flash",
                "gpt-4o": "openai/gpt-4o",
                "sonnet": "anthropic/claude-sonnet-4-20250514",
                "grok": "xai/grok-3",
                "llama": "groq/llama-3.3-70b",
            },
        }
        aliases = backend_aliases.get(self._ring0.backend_type, {})
        resolved = aliases.get(model.lower(), model)
        previous = self._ring0.model

        async def _do_switch() -> None:
            await asyncio.sleep(1.5)
            await self._ring0.switch_model(resolved, self._launcher, self._bridge)

        asyncio.create_task(_do_switch())
        return {"ok": True, "model": resolved, "previous": previous}

    async def ring0_mute_events(self, muted: bool = False) -> dict:
        if self._ring0 is None:
            return {"error": "Ring0 not available"}
        self._ring0.set_events_muted(bool(muted))
        return {"ok": True, "eventsMuted": bool(muted)}

    # ── WebRTC ────────────────────────────────────────────────────────────

    async def webrtc_offer(
        self,
        client_id: str = "",
        sdp: str = "",
        sdp_type: str = "offer",
        desktop_role: str = "controller",
        ice_servers: list | None = None,
    ) -> dict:
        if not self._desktop_webrtc:
            return {"error": "Desktop WebRTC not available on this node"}
        if not client_id or not sdp:
            return {"error": "clientId and sdp required"}
        try:
            answer = await self._desktop_webrtc.handle_offer(
                client_id, sdp, sdp_type,
                desktop_role=desktop_role,
                ice_servers=ice_servers,
            )
            return answer
        except Exception as e:
            logger.exception("Failed to handle desktop WebRTC offer")
            return {"error": str(e)}


# ── Payload key translation helpers ────────────────────────────────────────

import re

_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")


def camel_to_snake(name: str) -> str:
    return _CAMEL_BOUNDARY.sub(r"\1_\2", name).lower()


def snake_to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


def _snake_to_camel_dict(d: dict) -> dict:
    return {snake_to_camel(k): v for k, v in d.items()}


def _to_camel_dict(d: Any) -> Any:
    """Recursively convert dict keys snake_case → camelCase."""
    if isinstance(d, dict):
        return {snake_to_camel(k): _to_camel_dict(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_to_camel_dict(x) for x in d]
    return d


def payload_to_kwargs(msg: dict, *, drop: tuple = ("type", "requestId")) -> dict:
    """Translate a wire payload (camelCase keys) to NodeOperations kwargs (snake_case)."""
    return {
        camel_to_snake(k): v
        for k, v in msg.items()
        if k not in drop
    }
