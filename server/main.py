"""vibr8 — aiohttp server entry point.

Originally ported from The Vibe Companion (index.ts).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import aiohttp
from aiohttp import web

from vibr8_core.cli_launcher import CliLauncher
from vibr8_core.session_store import SessionStore
from vibr8_core.worktree_tracker import WorktreeTracker
from vibr8_core.ws_bridge import WsBridge
from server.auto_namer import generate_session_title, AutoNamerOptions
from server.paths import VIBR8_DIR
from vibr8_core import session_names
from server.routes import create_routes
from server.rate_limit import check_rate_limit, get_client_rate_limit_key
from server.terminal import TerminalManager

try:
    from server.webrtc import WebRTCManager
    HAS_WEBRTC = True
except ImportError:
    WebRTCManager = None  # type: ignore[assignment,misc]
    HAS_WEBRTC = False
from vibr8_core.ring0 import Ring0Manager
from vibr8_core.ring0_scheduler import TaskScheduler
from vibr8_core.ring0_events import Ring0EventRouter
from vibr8_core.node_operations import NodeOperations
from vibr8_core.node_client import SwappableNodeClient, NOT_READY
from vibr8_core.hub_browser_bridge import HubBrowserBridge
from server.node_registry import NodeRegistry
from server.node_tunnel import NodeTunnel
import json as _json
from server.auth import AuthManager, auth_middleware

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
        logging.FileHandler(
            os.environ.get("VIBR8_LOG_FILE", str(Path(__file__).parent.parent / "server.log")),
            mode="a",
        ),
    ],
)
logger = logging.getLogger(__name__)
logger.info("=" * 60)
logger.info("[server] Process starting (PID %d)", os.getpid())
logging.getLogger("asyncio").addFilter(_AioIceFilter())

# Enable experimental agent teams (matches TS version)
os.environ["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "1"

# Remove CLAUDECODE from our own environment so child CLI processes
# don't think they're nested. The launcher sets it explicitly for each child.
os.environ.pop("CLAUDECODE", None)

PORT = int(os.environ.get("PORT", "3456"))
RECONNECT_GRACE_S = 10
NODE_WS_RATE_LIMIT = 10
NODE_WS_RATE_WINDOW = 60.0
ALLOW_NO_AUTH_ENV = "VIBR8_ALLOW_NO_AUTH"
ALLOW_PUBLIC_NO_AUTH_ENV = "VIBR8_ALLOW_PUBLIC_NO_AUTH"
HOST_ENV = "VIBR8_HOST"

# Typed key for the WsBridge instance in app state. Mirrors the same
# idiom used in server/tests/test_smoke_ws_path.py so the smoke gate
# and production lookup match shape, and silences aiohttp's
# NotAppKeyWarning for this key on startup.
BRIDGE_KEY = web.AppKey("bridge", WsBridge)
BIND_HOST_KEY = web.AppKey("bind_host", str)
NODE_WS_RATE_KEY = web.AppKey("node_ws_rate", dict)
LOCAL_NODE_OPS_KEY = web.AppKey("local_node_ops", SwappableNodeClient)


def _env_flag(environ: Mapping[str, str], name: str) -> bool:
    return environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _is_loopback_host(host: str) -> bool:
    return host.strip().lower() in {"localhost", "127.0.0.1", "::1"}


def resolve_bind_host(auth_enabled: bool, environ: Mapping[str, str] = os.environ) -> str:
    """Resolve and validate the server bind host for the current auth mode."""
    requested_host = environ.get(HOST_ENV, "0.0.0.0").strip() or "0.0.0.0"
    if auth_enabled:
        return requested_host

    if not _env_flag(environ, ALLOW_NO_AUTH_ENV):
        raise RuntimeError(
            "Authentication is disabled because no users.json exists. Refusing to start "
            "without auth. Run "
            "`uv run python -m server.manage_users add <username>` to create a user, or set "
            f"{ALLOW_NO_AUTH_ENV}=1 for explicit local development."
        )

    if _is_loopback_host(requested_host):
        return requested_host

    if _env_flag(environ, ALLOW_PUBLIC_NO_AUTH_ENV):
        logger.warning(
            "[server] Authentication is disabled and %s=1; binding no-auth server to %s",
            ALLOW_PUBLIC_NO_AUTH_ENV,
            requested_host,
        )
        return requested_host

    logger.warning(
        "[server] Authentication is disabled; forcing bind host to 127.0.0.1. "
        "Set %s=1 to allow the requested no-auth public bind host %s.",
        ALLOW_PUBLIC_NO_AUTH_ENV,
        requested_host,
    )
    return "127.0.0.1"


def wire_session_callbacks(
    *,
    ws_bridge: object,
    on_cli_relaunch_needed: Callable[[str], None],
) -> None:
    """Wire the hub-side WsBridge proxy callbacks.

    The hub no longer owns sessions — all CLI lifecycle, computer-use
    creation, and first-turn auto-naming happens on the node that owns
    the session. The hub keeps only the cross-node relaunch hint so a
    browser reconnect can ask the node to bring its CLI back up.
    """
    ws_bridge.on_cli_relaunch_needed_callback(on_cli_relaunch_needed)


# ── WebSocket route handlers ─────────────────────────────────────────────────


async def handle_cli_ws(request: web.Request) -> web.WebSocketResponse:
    """Handle WebSocket connections from Claude Code CLI (--sdk-url)."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    session_id = request.match_info["session_id"]

    bridge = request.app[BRIDGE_KEY]
    launcher: CliLauncher = request.app["launcher"]

    await bridge.handle_cli_open(ws, session_id)
    launcher.mark_connected(session_id)

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    await bridge.handle_cli_message(ws, msg.data)
                except Exception:
                    logger.exception("[ws] Exception handling CLI message for session %s", session_id)
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error(f"[ws] CLI ws error: {ws.exception()}")
    finally:
        logger.info("[ws] CLI ws closed for session %s close_code=%s exception=%s", session_id, ws.close_code, ws.exception())
        await bridge.handle_cli_close(ws)

    return ws


async def handle_browser_ws(request: web.Request) -> web.WebSocketResponse:
    """Handle WebSocket connections from the browser UI."""
    ws = web.WebSocketResponse(heartbeat=45)
    await ws.prepare(request)
    session_id = request.match_info["session_id"]

    bridge = request.app[BRIDGE_KEY]
    client_id = request.rel_url.query.get("clientId", "")
    role = request.rel_url.query.get("role", "primary")

    mirror = request.rel_url.query.get("mirror", "") == "true"
    await bridge.handle_browser_open(ws, session_id, client_id, role=role, mirror=mirror)

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await bridge.handle_browser_message(ws, msg.data)
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error(f"[ws] Browser ws error: {ws.exception()}")
    finally:
        await bridge.handle_browser_close(ws)

    return ws


async def handle_playground_ws(request: web.Request) -> web.WebSocketResponse:
    """Handle WebSocket connections for voice playground sessions."""
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    client_id = request.match_info["client_id"]

    webrtc_mgr: WebRTCManager = request.app["webrtc_manager"]
    webrtc_mgr.register_playground_ws(client_id, ws)

    logger.info("[playground] Client %s connected", client_id)

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                import json as _json2
                try:
                    data = _json2.loads(msg.data)
                    # Handle live param updates from playground sliders
                    if data.get("type") == "update_params":
                        from server.stt import STTParams
                        params = STTParams(
                            mic_gain=float(data.get("micGain", 1.0)),
                            vad_threshold_db=float(data.get("vadThresholdDb", -30.0)),
                            silero_vad_threshold=float(data.get("sileroVadThreshold", 0.4)),
                            eou_threshold=float(data.get("eouThreshold", 0.15)),
                            eou_max_retries=int(data.get("eouMaxRetries", 3)),
                            min_segment_duration=float(data.get("minSegmentDuration", 0.4)),
                            prompt_timeout_ms=int(data.get("promptTimeoutMs", 1500)),
                        )
                        # Update STT params for this client's connection
                        webrtc_mgr.update_stt_params(client_id, params)
                except Exception:
                    pass
            elif msg.type == aiohttp.WSMsgType.ERROR:
                break
    finally:
        webrtc_mgr.unregister_playground_ws(client_id)
        logger.info("[playground] Client %s disconnected", client_id)

    return ws


async def handle_enrollment_ws(request: web.Request) -> web.WebSocketResponse:
    """Handle WebSocket connections for speaker fingerprint enrollment."""
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    client_id = request.match_info["client_id"]

    webrtc_mgr: WebRTCManager = request.app["webrtc_manager"]
    webrtc_mgr.register_enrollment_ws(client_id, ws)

    logger.info("[enrollment] Client %s connected", client_id)

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.ERROR:
                break
    finally:
        webrtc_mgr.unregister_enrollment_ws(client_id)
        logger.info("[enrollment] Client %s disconnected", client_id)

    return ws


async def handle_native_ws(request: web.Request) -> web.WebSocketResponse:
    """Handle native WebSocket from Android foreground service.

    This connection bypasses the WebView and stays alive when the app is
    backgrounded, enabling bring-to-foreground and other native commands.
    """
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    client_id = request.match_info["client_id"]

    bridge = request.app[BRIDGE_KEY]
    bridge.register_native_ws(client_id, ws)

    logger.info("[native] Connection opened for client %s", client_id[:8])
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = _json.loads(msg.data)
                    await bridge.handle_native_message(client_id, data)
                except Exception:
                    logger.exception("[native] Error handling message from client %s", client_id[:8])
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.info("[native] WS error for client %s: %s", client_id[:8], ws.exception())
                break
    finally:
        logger.info("[native] Connection closed for client %s close_code=%s exception=%s", client_id[:8], ws.close_code, ws.exception())
        bridge.unregister_native_ws(client_id)

    return ws


async def handle_terminal_ws(request: web.Request) -> web.WebSocketResponse:
    """Handle WebSocket connections for terminal (PTY) sessions."""
    import json as _json

    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    session_id = request.match_info["session_id"]

    terminal_mgr: TerminalManager = request.app["terminal_manager"]
    term = terminal_mgr.get(session_id)
    if not term:
        await ws.close(code=4004, message=b"Terminal session not found")
        return ws

    # PTY output → browser
    async def on_pty_data(data: bytes) -> None:
        if not ws.closed:
            await ws.send_bytes(data)

    async def on_pty_exit(code: int) -> None:
        if not ws.closed:
            await ws.send_str(_json.dumps({"type": "exit", "code": code}))

    term._on_data = on_pty_data
    term._on_exit = on_pty_exit
    term.start_reading(asyncio.get_event_loop())

    logger.info("[terminal] Browser connected to terminal session %s", session_id)

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                # User input → PTY
                term.write(msg.data)
            elif msg.type == aiohttp.WSMsgType.TEXT:
                # Control message (resize, ping, etc.)
                ctrl = _json.loads(msg.data)
                if ctrl.get("type") == "ping":
                    pass  # keepalive — no action needed
                elif ctrl.get("type") == "resize":
                    term.resize(ctrl["cols"], ctrl["rows"])
            elif msg.type == aiohttp.WSMsgType.ERROR:
                break
    finally:
        # Don't close the terminal on WS disconnect (allows reconnection)
        term._on_data = None
        term._on_exit = None
        term.stop_reading()
        logger.info("[terminal] Browser disconnected from terminal session %s", session_id)

    return ws


async def handle_node_ws(request: web.Request) -> web.StreamResponse:
    """Handle persistent WebSocket tunnel from a remote vibr8-node."""
    node_id = request.match_info["node_id"]
    ip = get_client_rate_limit_key(request)
    node_ws_rate = request.app[NODE_WS_RATE_KEY]
    if check_rate_limit(
        node_ws_rate,
        ip,
        limit=NODE_WS_RATE_LIMIT,
        window=NODE_WS_RATE_WINDOW,
    ):
        logger.warning(
            "[audit] node tunnel rate limited node=%s ip=%s",
            node_id[:8],
            ip,
            extra={
                "audit_event": "node_ws_rate_limited",
                "node_id_prefix": node_id[:8],
                "ip": ip,
            },
        )
        return web.json_response({"error": "Too many requests"}, status=429)

    # max_msg_size raised for ui/v1: proxied asset responses travel as
    # single NDJSON lines with base64 bodies (default 4MB is too small).
    ws = web.WebSocketResponse(heartbeat=45, max_msg_size=64 * 1024 * 1024)
    await ws.prepare(request)

    registry: NodeRegistry = request.app["node_registry"]
    bridge = request.app[BRIDGE_KEY]

    # Authenticate via query param API key
    api_key = request.rel_url.query.get("apiKey", "")
    api_key_prefix = api_key[:16] + "..." if api_key else ""
    node = registry.get_node(node_id)
    if not node:
        logger.warning(
            "[audit] node tunnel rejected node=%s ip=%s reason=unknown_node",
            node_id[:8],
            ip,
            extra={
                "audit_event": "node_ws_rejected",
                "node_id_prefix": node_id[:8],
                "ip": ip,
                "attempted_api_key_prefix": api_key_prefix,
                "reason": "unknown_node",
            },
        )
        await ws.close(code=4001, message=b"Invalid node ID or API key")
        return ws
    if not registry.validate_api_key(node_id, api_key):
        logger.warning(
            "[audit] node tunnel rejected node=%s ip=%s reason=invalid_or_revoked_token",
            node_id[:8],
            ip,
            extra={
                "audit_event": "node_ws_rejected",
                "node_id_prefix": node_id[:8],
                "api_key_id": node.api_key_id,
                "ip": ip,
                "attempted_api_key_prefix": api_key_prefix,
                "reason": "invalid_or_revoked_token",
            },
        )
        await ws.close(code=4001, message=b"Invalid node ID or API key")
        return ws

    # Create tunnel and mark online
    tunnel = NodeTunnel(node_id, node.name, ws)
    node.tunnel = tunnel
    registry.set_online(node_id, ws)

    # Fire reconnect signals for any CU agents targeting this node
    reconnect_signals = request.app.get("node_reconnect_signals", {})
    for evt in reconnect_signals.get(node_id, []):
        evt.set()

    async def on_node_message(nid: str, msg: dict) -> None:
        """Handle node-initiated messages (heartbeat, session updates, etc.)."""
        msg_type = msg.get("type", "")
        if msg_type == "heartbeat":
            registry.heartbeat(
                nid,
                session_count=msg.get("sessionCount"),
                ring0_enabled=msg.get("ring0Enabled"),
            )
        elif msg_type == "sessions_update":
            # Session ids stay raw — sessions are node-internal; browsers
            # reach them through the node's vended UI (contract ui/v1).
            sessions = msg.get("sessions", [])
            session_ids = [s.get("sessionId", s.get("id", "")) for s in sessions]
            registry.update_sessions(nid, session_ids)
        elif msg_type == "speak":
            # Contract events/v1: the node's Ring0 wants this spoken (§B).
            text = msg.get("text", "")
            wm = request.app.get("webrtc_manager")
            if text and wm:
                pair = wm.get_any_outgoing_track()
                if pair:
                    audio_client_id, track = pair
                    if not wm.is_tts_muted(audio_client_id):
                        asyncio.ensure_future(
                            bridge._speak_text(f"{nid}:ring0", text, track)
                        )
        elif msg_type == "busy":
            n = registry.get_node(nid)
            if n:
                n.ring0_busy = bool(msg.get("busy"))
        elif msg_type == "attention":
            logger.info(
                "[nodes] Attention from node %r: %s",
                node.name, msg.get("reason", ""),
            )
        elif msg_type == "ring0_state":
            n = registry.get_node(nid)
            if n:
                n.ring0_enabled = msg.get("enabled", False)
        elif msg_type in ("ws_data", "ws_close"):
            # Proxied browser-WS channel traffic (contract ui/v1).
            from server.node_ui_proxy import dispatch_channel_message
            await dispatch_channel_message(request.app, msg)
        else:
            logger.debug("[nodes] Unknown message type %r from node %s", msg_type, node.name)

    tunnel.set_message_handler(on_node_message)

    logger.info("[nodes] WS tunnel opened for node %r (%s)", node.name, node_id[:8])

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await tunnel.handle_incoming(msg.data)
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error("[nodes] WS tunnel error for node %s: %s", node.name, ws.exception())
                break
    finally:
        logger.info("[nodes] WS tunnel closed for node %r (%s)", node.name, node_id[:8])
        tunnel.close()
        registry.set_offline(node_id)

    return ws


# ── Application factory ──────────────────────────────────────────────────────


def create_app() -> web.Application:
    auth_manager = AuthManager()
    bind_host = resolve_bind_host(auth_manager.enabled)
    middlewares = [auth_middleware] if auth_manager.enabled else []
    app = web.Application(middlewares=middlewares)
    app[BIND_HOST_KEY] = bind_host

    session_store = SessionStore()
    ws_bridge = WsBridge()
    launcher = CliLauncher(PORT)
    worktree_tracker = WorktreeTracker()
    # Load ICE server config for WebRTC (STUN/TURN)
    ice_servers: list[dict] = []
    ice_env = os.environ.get("VIBR8_ICE_SERVERS")
    if ice_env:
        ice_servers = _json.loads(ice_env)
    else:
        ice_config = VIBR8_DIR / "ice-servers.json"
        if ice_config.exists():
            ice_servers = _json.loads(ice_config.read_text())
    if ice_servers:
        logger.info("[server] Loaded %d ICE server(s)", len(ice_servers))

    webrtc_manager = WebRTCManager(ice_servers=ice_servers) if HAS_WEBRTC else None
    voice_service_url = os.environ.get("VIBR8_VOICE_SERVICE_URL", "").strip()
    if webrtc_manager and voice_service_url:
        from server.voice_service_client import VoiceServiceClient

        webrtc_manager = VoiceServiceClient(
            webrtc_manager,
            voice_service_url,
            api_token=os.environ.get("VIBR8_VOICE_SERVICE_API_TOKEN") or None,
            tenant_id=os.environ.get("VIBR8_VOICE_SERVICE_TENANT", "default"),
        )
        logger.info("[server] Audio WebRTC proxy enabled: %s", voice_service_url)
    terminal_manager = TerminalManager()
    ring0_manager = Ring0Manager(PORT, auth_manager=auth_manager)
    task_scheduler = TaskScheduler()
    node_registry = NodeRegistry()

    # The hub is a stateless router: it never owns sessions. There is no
    # "self/local" node baked in — every node, including the host, is a
    # separate vibr8_node process the operator runs and registers via API
    # key. local_node_ops stays NOT_READY (nothing to swap to); the very
    # few hub-side voice/event paths that still reach for a "default node"
    # resolve one per-client from the registry instead.
    local_node_ops = SwappableNodeClient(NOT_READY)
    hub_browser_bridge = HubBrowserBridge(ws_bridge)

    try:
        from server.android_registry import AndroidRegistry
        android_registry = AndroidRegistry()
    except ImportError:
        android_registry = None

    # Track background tasks so we can cancel them on shutdown.
    background_tasks: set[asyncio.Task] = set()

    def spawn(coro) -> asyncio.Task:
        """Create a tracked background task that auto-removes on completion.

        Uses `get_running_loop().create_task` rather than `ensure_future`
        so it raises `RuntimeError` immediately if called outside a
        running event loop. Previously, `ensure_future` silently fell
        back to `get_event_loop()` which, in Python 3.10+, creates a
        brand-new "current thread" loop. Tasks created that way live
        on a different loop than `web.run_app`'s loop, and `on_shutdown`'s
        `asyncio.gather(*background_tasks)` raises
        `ValueError: future belongs to a different loop`, aborting
        cleanup mid-flight and leaving the process zombied.
        """
        task = asyncio.get_running_loop().create_task(coro)
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return task

    _fast_startup = os.environ.get("VIBR8_FAST_STARTUP") == "1"

    # Wire up stores and managers
    ws_bridge.set_store(session_store)
    if webrtc_manager:
        ws_bridge.set_webrtc_manager(webrtc_manager)
    ws_bridge.set_ring0_manager(ring0_manager)
    ws_bridge.set_ring0_event_router(Ring0EventRouter())
    ws_bridge.set_node_registry(node_registry)
    ws_bridge.set_task_scheduler(task_scheduler)

    # Hub-side Ring0 event emissions (note_mode_ended, second_screen_*,
    # etc.) need to reach the *event source's active node* Ring0 — the
    # hub itself owns no Ring0. Source is resolved per-event via
    # `event.source_client_id` →
    # `hub_browser_bridge.get_client_active_node(client_id)`. Events
    # without a routable active node are dropped with a warning.
    async def _forward_event_to_active_node(event) -> None:
        try:
            source_cid = getattr(event, "source_client_id", None) or ""
            active_nid = hub_browser_bridge.get_client_active_node(source_cid)
            node = node_registry.get_node(active_nid) if (node_registry and active_nid) else None
            if node and node.tunnel and getattr(node.tunnel, "connected", False):
                await node.tunnel.send_fire_and_forget({
                    "type": "emit_ring0_event",
                    "eventFields": dict(event.fields),
                })
            else:
                logger.warning(
                    "[server] No routable active node for Ring0 event %s (client=%s) — dropped",
                    event.fields.get("type", "?"),
                    source_cid[:8] if source_cid else "?",
                )
        except Exception:
            logger.exception("[server] failed to forward Ring0 event")
    ws_bridge.set_event_forwarder(_forward_event_to_active_node)

    if webrtc_manager:
        webrtc_manager.set_ws_bridge(ws_bridge)
        webrtc_manager.set_hub_browser_bridge(hub_browser_bridge)
        webrtc_manager.set_local_node_ops(local_node_ops)
        webrtc_manager.set_node_registry(node_registry)

    # The hub no longer owns sessions — no restore_from_disk, no
    # launcher/ws_bridge session callbacks, no Ring0 auto-launch. Each
    # node owns its sessions and persists them under its own data dir.

    # When a computer-use session is created, spin up the agent and register it
    try:
        from server.desktop_target import DesktopTarget
        from server.adb_target import AdbTarget
        from server.agent_registry import get_agent_type
        import server.ui_tars_agent  # noqa: F401 — registers "ui-tars" agent type
        HAS_COMPUTER_USE = True
    except ImportError:
        HAS_COMPUTER_USE = False

    # Reconnect signals per node — set when a node tunnel reconnects.
    # Stored on app so handle_node_ws can access it via request.app.
    _node_reconnect_signals: dict[str, list[asyncio.Event]] = {}
    app["node_reconnect_signals"] = _node_reconnect_signals

    def on_computer_use_created(session_id: str, info: object) -> None:
        node_id = getattr(info, "nodeId", None) or ""
        agent_type_id = getattr(info, "agentType", None) or "ui-tars"
        agent_config = getattr(info, "agentConfig", None) or {}
        # Strip the {node_id}: prefix before truncating — qualified ids
        # share the first 8 chars on the same node and would collide.
        _raw_sid = session_id.split(":", 1)[1] if ":" in session_id else session_id
        agent_client_id = f"agent-{_raw_sid[:8]}"
        ice_servers = webrtc_manager.get_client_ice_servers() if webrtc_manager else []
        reconnect_signal: asyncio.Event | None = None

        # Look up agent type from registry
        agent_type = get_agent_type(agent_type_id)
        if not agent_type:
            logger.error("[server] Unknown agent type %r for session %s", agent_type_id, session_id[:8])
            return

        # Pre-create ws_bridge session with correct backend type so browser
        # connects don't trigger CLI relaunch while VLM is still loading
        ws_bridge.get_or_create_session(session_id, "computer-use")

        # Check if target is an Android device (ADB/scrcpy path)
        android_node = None
        android_registry = app.get("android_registry")
        if node_id and android_registry:
            android_node = android_registry.get_node(node_id)

        if android_node:
            # Android target — use scrcpy via AdbTarget
            if android_node.status != "online":
                logger.error("[server] Cannot create CU agent: Android device %s is offline", android_node.name)
                return

            # Get or start scrcpy client for this device
            if not android_node.scrcpy_client:
                from server.scrcpy_client import ScrcpyClient
                android_node.scrcpy_client = ScrcpyClient(
                    device_id=android_node.device_id,
                    max_size=1080,
                    max_fps=30,
                )

            target = AdbTarget(scrcpy_client=android_node.scrcpy_client)
            _factory = agent_type.factory

            async def _init_agent() -> None:
                try:
                    logger.info("[server] Initializing %s agent for %s (device=%s)...",
                                agent_type_id, session_id[:8], android_node.name)
                    status_cb = lambda msg: ws_bridge.send_to_browsers(session_id, msg)
                    agent = await _factory(session_id, target, agent_config, status_cb)
                    await agent.start()
                    logger.info("[server] Agent started, registering for %s", session_id[:8])
                    ws_bridge.register_computer_use_agent(session_id, agent)
                except Exception:
                    logger.exception("[server] Failed to start CU agent for %s", session_id[:8])

            spawn(_init_agent())
            return

        # Desktop target — existing WebRTC path
        # Build signaling function — same path as browser WebRTC offers
        if node_id and node_registry:
            node = node_registry.get_node(node_id)
            if not node or not node.tunnel or not node.tunnel.connected:
                logger.error("[server] Cannot create computer-use agent: node %s not connected", node_id)
                return

            # Dynamic tunnel lookup — gets the current tunnel at call time,
            # not a stale reference captured at creation time.
            _target_node_id = node_id

            async def signaling_fn(sdp: str, sdp_type: str) -> dict:
                n = node_registry.get_node(_target_node_id) if node_registry else None
                if not n or not n.tunnel or not n.tunnel.connected:
                    raise ConnectionError(f"Node {_target_node_id} not connected")
                return await n.tunnel.send_command({
                    "type": "webrtc_offer",
                    "clientId": agent_client_id,
                    "sdp": sdp,
                    "sdpType": sdp_type,
                    "desktopRole": "controller",
                    "iceServers": ice_servers,
                }, timeout=15.0)

            # Reconnect signal — set when this node's tunnel reconnects
            reconnect_signal = asyncio.Event()
            _node_reconnect_signals.setdefault(node_id, []).append(reconnect_signal)
        else:
            async def signaling_fn(sdp: str, sdp_type: str) -> dict:
                # Same local path as routes.py:1193 — WebRTCManager.handle_offer
                return await webrtc_manager.handle_offer(
                    agent_client_id, sdp, sdp_type,
                    desktop=True, desktop_role="controller",
                )

        target = DesktopTarget(
            signaling_fn=signaling_fn,
            ice_servers=ice_servers,
            reconnect_signal=reconnect_signal,
        )
        _factory = agent_type.factory

        async def _init_agent() -> None:
            try:
                logger.info("[server] Initializing %s agent for %s (node=%s)...", agent_type_id, session_id[:8], node_id or "hub")
                status_cb = lambda msg: ws_bridge.send_to_browsers(session_id, msg)
                agent = await _factory(session_id, target, agent_config, status_cb)
                await agent.start()
                logger.info("[server] Agent started, registering for %s", session_id[:8])
                ws_bridge.register_computer_use_agent(session_id, agent)
            except Exception:
                logger.exception("[server] Failed to start computer-use agent for %s", session_id[:8])

        spawn(_init_agent())

    # Auto-relaunch CLI when a browser connects to a session with no CLI
    relaunching: set[str] = set()

    def on_cli_relaunch_needed(session_id: str) -> None:
        # Only computer-use sessions are owned by the hub. Per-node CLI
        # sessions live entirely on their node (the browser reaches them
        # through that node's vended path, not the hub's /ws/browser/);
        # an own-session relaunch hint here would have no node to call.
        if session_id in relaunching:
            return
        if HAS_COMPUTER_USE:
            info = launcher.get_session(session_id)
            if info and not info.archived and info.backendType == "computer-use":
                relaunching.add(session_id)
                logger.info("[server] Re-creating CU agent for session %s", session_id[:8])
                on_computer_use_created(session_id, info)

                async def _cu_cooldown() -> None:
                    await asyncio.sleep(15)  # longer cooldown for VLM loading
                    relaunching.discard(session_id)

                spawn(_cu_cooldown())

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

    wire_session_callbacks(
        ws_bridge=ws_bridge,
        on_cli_relaunch_needed=on_cli_relaunch_needed,
    )

    # ── Routes ────────────────────────────────────────────────────────────

    # WebSocket endpoints
    app.router.add_get("/ws/cli/{session_id}", handle_cli_ws)
    app.router.add_get("/ws/browser/{session_id}", handle_browser_ws)
    app.router.add_get("/ws/native/{client_id}", handle_native_ws)
    app.router.add_get("/ws/terminal/{session_id}", handle_terminal_ws)
    if HAS_WEBRTC:
        app.router.add_get("/ws/playground/{client_id}", handle_playground_ws)
        app.router.add_get("/ws/enrollment/{client_id}", handle_enrollment_ws)
    app.router.add_get("/ws/node/{node_id}", handle_node_ws)

    # REST API
    api_routes = create_routes(launcher, ws_bridge, session_store, worktree_tracker, webrtc_manager, terminal_manager, auth_manager, ring0_manager, node_registry, local_node_ops=local_node_ops, hub_browser_bridge=hub_browser_bridge)
    app.router.add_routes(api_routes)

    # Node-vended UI proxy (contract ui/v1): /nodes/{id}/{ui,api,ws}/* over
    # the node's tunnel. Must register before the SPA catch-all.
    from server.node_ui_proxy import register_node_ui_routes
    register_node_ui_routes(app)

    # Production static file serving
    if os.environ.get("NODE_ENV") == "production":
        dist_dir = Path(__file__).parent.parent / "web" / "dist"
        if dist_dir.is_dir():
            app.router.add_static("/assets", dist_dir / "assets")

            async def spa_fallback(request: web.Request) -> web.StreamResponse:
                # Serve static files from dist/ if they exist on disk
                rel = request.match_info.get("path", "")
                if rel:
                    candidate = dist_dir / rel
                    if candidate.is_file() and dist_dir in candidate.resolve().parents:
                        return web.FileResponse(candidate)
                return web.FileResponse(dist_dir / "index.html")

            # Catch-all for SPA routing
            app.router.add_get("/{path:.*}", spa_fallback)

    # Store references for handlers
    app[BRIDGE_KEY] = ws_bridge
    app[NODE_WS_RATE_KEY] = {}
    app["launcher"] = launcher
    app["session_store"] = session_store
    app["worktree_tracker"] = worktree_tracker
    app["webrtc_manager"] = webrtc_manager
    app["terminal_manager"] = terminal_manager
    app["auth_manager"] = auth_manager
    app["ring0_manager"] = ring0_manager
    app["task_scheduler"] = task_scheduler
    app["node_registry"] = node_registry
    app[LOCAL_NODE_OPS_KEY] = local_node_ops
    app["android_registry"] = android_registry
    app["request_restart"] = request_restart

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

        async def _preload_tts() -> None:
            try:
                import os
                engine = os.getenv("VIBR8_TTS_ENGINE", "kokoro").lower()
                if engine != "openai":
                    from server.tts_kokoro import _ensure_pipeline
                    logger.info("[server] Preloading Kokoro TTS model (background thread)...")
                    await asyncio.to_thread(_ensure_pipeline)
                    logger.info("[server] Kokoro TTS model ready")
            except ImportError:
                logger.info("[server] Kokoro not installed — skipping TTS preload")
            except Exception:
                logger.exception("[server] Failed to preload Kokoro TTS model")

        if HAS_WEBRTC and not _fast_startup:
            spawn(_preload_stt())
            # Warm the voice pipeline (Whisper + Silero VAD + EOU + ECAPA + TSE)
            # in the background so the first utterance after the user enables
            # voice doesn't pay 9-13s of first-inference latency. Runs entirely
            # on worker threads inside warmup_voice_models so it does NOT block
            # the event loop — the server is fully responsive while this runs.
            try:
                from server.stt import warmup_voice_models
                spawn(warmup_voice_models())
            except Exception:
                logger.exception("[server] Failed to schedule voice-model warmup")
        if not _fast_startup:
            spawn(_preload_tts())

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

        # Periodic heartbeat checker for remote nodes
        async def _heartbeat_checker() -> None:
            while True:
                await asyncio.sleep(30)
                try:
                    newly_offline = node_registry.check_heartbeats()
                    for node_id in newly_offline:
                        logger.info("[nodes] Node %s missed heartbeat — marked offline", node_id[:8])
                except Exception:
                    logger.exception("[nodes] Error checking heartbeats")

        spawn(_heartbeat_checker())

        # Start Android device status polling
        if android_registry:
            await android_registry.start_polling(interval=5.0)

        # Start mDNS discovery for ADB devices (if zeroconf is installed)
        from server.mdns_discovery import MdnsDiscovery
        mdns = MdnsDiscovery()
        app["mdns_discovery"] = mdns
        if mdns.available:
            await mdns.start()

        # Start scheduled task runner
        await task_scheduler.start()

    app.on_startup.append(on_startup)

    async def on_shutdown(app: web.Application) -> None:
        logger.info("[server] Shutting down (restart=%s)...", _restart["requested"])

        # Flush any pending debounced session saves to disk before anything else.
        ws_bridge.flush_to_disk()

        # Cancel all tracked background tasks first.
        for task in list(background_tasks):
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)

        # Stop scheduled task runner
        await task_scheduler.stop()

        # Always kill CLI processes so they respawn fresh with updated code.
        # This is critical for Ring0's MCP subprocess which loads ring0_mcp.py
        # at startup — without this, code changes never take effect.
        await launcher.kill_all()

        # Shutdown Android registry and mDNS discovery
        if android_registry:
            await android_registry.shutdown()
        mdns = app.get("mdns_discovery")
        if mdns:
            await mdns.stop()

        if _restart["requested"]:
            # Fast path: skip graceful WebSocket close (10s timeout per socket).
            # Connections break naturally when the process exits; clients reconnect.
            logger.info("[server] Restart — skipping graceful close for speed")
        else:
            await terminal_manager.close_all()
            if webrtc_manager:
                await webrtc_manager.close_all()
            await ws_bridge.close_all()
        logger.info("[server] Shutdown complete")

    app.on_shutdown.append(on_shutdown)

    return app


# ── Restart support ───────────────────────────────────────────────────────────

_RESTART_EXIT_CODE = 75

# Mutable container so the flag is shared even when this module is imported
# under two names (__main__ vs server.main — a Python -m gotcha).
_restart = {"requested": False}


def request_restart() -> None:
    """Request a server restart (used by the admin/restart endpoint).

    Sends SIGTERM to trigger aiohttp's graceful shutdown. The Makefile restart
    loop detects exit code 75 and relaunches the process.
    """
    import signal
    _restart["requested"] = True
    os.kill(os.getpid(), signal.SIGTERM)


# ── Entry point ───────────────────────────────────────────────────────────────

def _get_ssl_context():
    """Load SSL context from certs/ directory if available."""
    import ssl
    if os.environ.get("VIBR8_DISABLE_TLS") == "1":
        return None
    cert_dir = Path(__file__).parent.parent / "certs"
    cert_file = cert_dir / "cert.pem"
    key_file = cert_dir / "key.pem"
    if cert_file.exists() and key_file.exists():
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert_file), str(key_file))
        logger.info("[server] SSL enabled (cert: %s)", cert_file)
        return ctx
    return None


def main() -> None:
    try:
        app = create_app()
        bind_host = app[BIND_HOST_KEY]
        ssl_ctx = _get_ssl_context()
        scheme = "https" if ssl_ctx else "http"
        ws_scheme = "wss" if ssl_ctx else "ws"
        logger.info("Server running on %s://%s:%d", scheme, bind_host, PORT)
        logger.info("  CLI WebSocket:     %s://%s:%d/ws/cli/:sessionId", ws_scheme, bind_host, PORT)
        logger.info("  Browser WebSocket: %s://%s:%d/ws/browser/:sessionId", ws_scheme, bind_host, PORT)
        if os.environ.get("NODE_ENV") != "production":
            logger.info("Dev mode: frontend at http://localhost:5174")
        web.run_app(app, host=bind_host, port=PORT, print=None, shutdown_timeout=2.0, reuse_address=True, ssl_context=ssl_ctx)
    except Exception:
        logger.exception("[server] Fatal error during startup/run")
        sys.exit(1)
    if _restart["requested"]:
        logger.info("[server] Exiting with code %d for restart", _RESTART_EXIT_CODE)
        sys.exit(_RESTART_EXIT_CODE)


if __name__ == "__main__":
    main()
