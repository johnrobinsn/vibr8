"""vibr8 — aiohttp server entry point.

Ported from companion/web/server/index.ts.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import aiohttp
from aiohttp import web

from server.cli_launcher import CliLauncher
from server.session_store import SessionStore
from server.worktree_tracker import WorktreeTracker
from server.ws_bridge import WsBridge
from server.auto_namer import generate_session_title, AutoNamerOptions
from server import session_names
from server.routes import create_routes
from server.webrtc import WebRTCManager

from dotenv import load_dotenv
load_dotenv()

class _AioIceFilter(logging.Filter):
    """Suppress noisy aioice STUN retry errors on closed transports."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "Transaction.__retry" in msg or ("sendto" in msg and "aioice" in msg):
            return False
        if record.exc_info and record.exc_info[1]:
            exc_str = str(record.exc_info[1])
            if "sendto" in exc_str or "call_exception_handler" in exc_str:
                return False
        return True


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/mntc/code/vibr8/server.log", mode="w"),
    ],
)
logging.getLogger("asyncio").addFilter(_AioIceFilter())
logger = logging.getLogger(__name__)

# Enable experimental agent teams (matches TS version)
os.environ["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "1"

# Remove CLAUDECODE from our own environment so child CLI processes
# don't think they're nested. The launcher sets it explicitly for each child.
os.environ.pop("CLAUDECODE", None)

PORT = int(os.environ.get("PORT", "3456"))
RECONNECT_GRACE_S = 10


# ── WebSocket route handlers ─────────────────────────────────────────────────


async def handle_cli_ws(request: web.Request) -> web.WebSocketResponse:
    """Handle WebSocket connections from Claude Code CLI (--sdk-url)."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    session_id = request.match_info["session_id"]

    bridge: WsBridge = request.app["bridge"]
    launcher: CliLauncher = request.app["launcher"]

    bridge.handle_cli_open(ws, session_id)
    launcher.mark_connected(session_id)

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await bridge.handle_cli_message(ws, msg.data)
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error(f"[ws] CLI ws error: {ws.exception()}")
    finally:
        await bridge.handle_cli_close(ws)

    return ws


async def handle_browser_ws(request: web.Request) -> web.WebSocketResponse:
    """Handle WebSocket connections from the browser UI."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    session_id = request.match_info["session_id"]

    bridge: WsBridge = request.app["bridge"]

    await bridge.handle_browser_open(ws, session_id)

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await bridge.handle_browser_message(ws, msg.data)
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error(f"[ws] Browser ws error: {ws.exception()}")
    finally:
        await bridge.handle_browser_close(ws)

    return ws


# ── Application factory ──────────────────────────────────────────────────────


def create_app() -> web.Application:
    app = web.Application()

    session_store = SessionStore()
    ws_bridge = WsBridge()
    launcher = CliLauncher(PORT)
    worktree_tracker = WorktreeTracker()
    webrtc_manager = WebRTCManager()

    # Track background tasks so we can cancel them on shutdown.
    background_tasks: set[asyncio.Task] = set()

    def spawn(coro) -> asyncio.Task:
        """Create a tracked background task that auto-removes on completion."""
        task = asyncio.ensure_future(coro)
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return task

    # Wire up stores and managers
    ws_bridge.set_store(session_store)
    ws_bridge.set_webrtc_manager(webrtc_manager)
    webrtc_manager.set_ws_bridge(ws_bridge)
    launcher.set_store(session_store)

    # Restore persisted state
    launcher.restore_from_disk()
    ws_bridge.restore_from_disk()

    logger.info(f"[server] Session persistence: {session_store.directory}")

    # ── Callbacks ─────────────────────────────────────────────────────────

    # When the CLI reports its internal session_id, store it for --resume
    def on_cli_session_id(session_id: str, cli_session_id: str) -> None:
        launcher.set_cli_session_id(session_id, cli_session_id)

    ws_bridge.on_cli_session_id_received(on_cli_session_id)

    # When a Codex adapter is created, attach it to the WsBridge
    def on_codex_adapter(session_id: str, adapter: object) -> None:
        ws_bridge.attach_codex_adapter(session_id, adapter)

    launcher.on_codex_adapter_created(on_codex_adapter)

    # Auto-relaunch CLI when a browser connects to a session with no CLI
    relaunching: set[str] = set()

    def on_cli_relaunch_needed(session_id: str) -> None:
        if session_id in relaunching:
            return
        info = launcher.get_session(session_id)
        if info and info.archived:
            return
        if info and info.state != "starting":
            relaunching.add(session_id)
            logger.info(f"[server] Auto-relaunching CLI for session {session_id}")

            async def _do_relaunch() -> None:
                try:
                    await launcher.relaunch(session_id)
                finally:
                    # Remove from set after a cooldown
                    await asyncio.sleep(5)
                    relaunching.discard(session_id)

            spawn(_do_relaunch())

    ws_bridge.on_cli_relaunch_needed_callback(on_cli_relaunch_needed)

    # Auto-generate session title after first turn completes
    def on_first_turn_completed(session_id: str, first_user_message: str) -> None:
        # Don't overwrite a name that was already set
        if session_names.get_name(session_id):
            return
        info = launcher.get_session(session_id)
        model = (info.model if info and info.model else None) or "claude-sonnet-4-5-20250929"
        backend_type = (info.backendType if info else None) or "claude"
        logger.info(f"[server] Auto-naming session {session_id} with model {model} ({backend_type})...")

        async def _do_auto_name() -> None:
            title = await generate_session_title(
                first_user_message, model,
                options=AutoNamerOptions(backend_type=backend_type),
            )
            # Re-check: a manual rename may have occurred while generating
            if title and not session_names.get_name(session_id):
                logger.info(f'[server] Auto-named session {session_id}: "{title}"')
                session_names.set_name(session_id, title)
                await ws_bridge.broadcast_name_update(session_id, title)

        spawn(_do_auto_name())

    ws_bridge.on_first_turn_completed_callback(on_first_turn_completed)

    # ── Routes ────────────────────────────────────────────────────────────

    # WebSocket endpoints
    app.router.add_get("/ws/cli/{session_id}", handle_cli_ws)
    app.router.add_get("/ws/browser/{session_id}", handle_browser_ws)

    # REST API
    api_routes = create_routes(launcher, ws_bridge, session_store, worktree_tracker, webrtc_manager)
    app.router.add_routes(api_routes)

    # Production static file serving
    if os.environ.get("NODE_ENV") == "production":
        dist_dir = Path(__file__).parent.parent / "web" / "dist"
        if dist_dir.is_dir():
            app.router.add_static("/assets", dist_dir / "assets")

            async def spa_fallback(request: web.Request) -> web.StreamResponse:
                return web.FileResponse(dist_dir / "index.html")

            # Catch-all for SPA routing
            app.router.add_get("/{path:.*}", spa_fallback)

    # Store references for handlers
    app["bridge"] = ws_bridge
    app["launcher"] = launcher
    app["session_store"] = session_store
    app["worktree_tracker"] = worktree_tracker
    app["webrtc_manager"] = webrtc_manager

    # ── Startup / Shutdown hooks ──────────────────────────────────────────

    async def on_startup(app: web.Application) -> None:
        # Preload STT models (Whisper, VAD, EOU) in a background thread
        # so we don't block the event loop during download/loading.
        async def _preload_stt() -> None:
            try:
                from server.stt import AsyncSTT
                logger.info("[server] Preloading STT models (background thread)...")
                await asyncio.to_thread(AsyncSTT.preload_shared_resources)
                logger.info("[server] STT models ready")
            except Exception:
                logger.exception("[server] Failed to preload STT models")

        spawn(_preload_stt())

        # Suppress noisy aioice STUN retry errors on closed transports.
        loop = asyncio.get_event_loop()

        def _ice_exception_handler(
            loop: asyncio.AbstractEventLoop, context: dict
        ) -> None:
            exc = context.get("exception")
            if exc and isinstance(exc, AttributeError):
                s = str(exc)
                if "sendto" in s or "call_exception_handler" in s:
                    return
            loop.default_exception_handler(context)

        loop.set_exception_handler(_ice_exception_handler)

        # Reconnection watchdog: give restored CLI processes time to reconnect
        starting = launcher.get_starting_sessions()
        if starting:
            logger.info(
                f"[server] Waiting {RECONNECT_GRACE_S}s for "
                f"{len(starting)} CLI process(es) to reconnect..."
            )

            async def _watchdog() -> None:
                await asyncio.sleep(RECONNECT_GRACE_S)
                stale = launcher.get_starting_sessions()
                for info in stale:
                    if info.archived:
                        continue
                    logger.info(
                        f"[server] CLI for session {info.sessionId} "
                        "did not reconnect, relaunching..."
                    )
                    await launcher.relaunch(info.sessionId)

            spawn(_watchdog())

    app.on_startup.append(on_startup)

    async def on_shutdown(app: web.Application) -> None:
        logger.info("[server] Shutting down...")

        # Cancel all tracked background tasks first.
        for task in list(background_tasks):
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)

        await webrtc_manager.close_all()
        await ws_bridge.close_all()
        await launcher.kill_all()
        logger.info("[server] Shutdown complete")

    app.on_shutdown.append(on_shutdown)

    return app


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    app = create_app()
    logger.info(f"Server running on http://localhost:{PORT}")
    logger.info(f"  CLI WebSocket:     ws://localhost:{PORT}/ws/cli/:sessionId")
    logger.info(f"  Browser WebSocket: ws://localhost:{PORT}/ws/browser/:sessionId")
    if os.environ.get("NODE_ENV") != "production":
        logger.info("Dev mode: frontend at http://localhost:5174")
    web.run_app(app, host="0.0.0.0", port=PORT, print=None, shutdown_timeout=2.0)


if __name__ == "__main__":
    main()
