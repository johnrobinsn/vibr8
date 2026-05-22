"""NodeClient — uniform interface for hub-side code to invoke node operations.

Both the hub's in-process node (NodeOperations bound to in-process managers)
and any remote node (RemoteNodeClient = tunnel transport) expose the same
surface. Hub-side code (routes, voice routing, computer-use, etc.) never
branches on local vs remote — it gets a NodeClient and calls methods on it.

In Phase 4c-6 (Option A) the hub spawns a self-node subprocess at startup
and retargets local_node_ops at it via SwappableNodeClient.swap(). After
that swap, every NodeClient operation on the hub flows through the loopback
tunnel.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from vibr8_core.node_operations import NodeOperations, snake_to_camel

logger = logging.getLogger(__name__)


@runtime_checkable
class NodeClient(Protocol):
    """Structural protocol — anything that exposes NodeOperations' methods."""

    async def list_sessions(self) -> dict: ...
    async def create_session(self, options: dict | None = None) -> dict: ...
    async def submit_message(
        self, session_id: str = "", content: str = "", source_client_id: str = "",
    ) -> dict: ...
    async def kill_session(self, session_id: str = "") -> dict: ...
    async def relaunch_session(self, session_id: str = "") -> dict: ...
    async def delete_session(self, session_id: str = "") -> dict: ...
    async def archive_session(self, session_id: str = "") -> dict: ...
    async def unarchive_session(self, session_id: str = "") -> dict: ...
    async def rename_session(self, session_id: str = "", name: str = "") -> dict: ...


class SwappableNodeClient:
    """NodeClient wrapper whose backing target can be replaced at runtime.

    Used by the hub so `local_node_ops` can start as the in-process
    `NodeOperations` and then atomically swap to a `RemoteNodeClient`
    pointing at the self-node tunnel once it has registered. Routes
    capture the wrapper in closure and pick up the new target via
    `__getattr__` after the swap.
    """

    def __init__(self, initial_target: Any) -> None:
        self._target = initial_target

    def swap(self, new_target: Any) -> None:
        self._target = new_target

    @property
    def target(self) -> Any:
        return self._target

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._target, name)


class QualifyingNodeClient:
    """Wraps a NodeClient and qualifies session IDs at the hub boundary.

    The self-node has its own raw session UUIDs (e.g. ``abc-123-...``).
    The hub-side routing machinery (browser WS forwarding, remote-session
    proxy in WsBridge, etc.) keys off **qualified** session IDs of the
    form ``{node_id}:{raw_id}`` to decide whether to dispatch locally
    or via tunnel. Without that prefix the hub would treat self-node
    sessions as "local" and fail to forward.

    This wrapper sits between ``SwappableNodeClient`` and the underlying
    ``RemoteNodeClient`` (or ``NodeOperations``). It:
      - **Strips** the ``{node_id}:`` prefix from any ``session_id`` kwarg
        before forwarding, so the inner client always sees raw IDs.
      - **Rewrites** ``sessionId`` fields in response dicts to be
        ``{node_id}:{raw_id}``, so the frontend (and the rest of the hub)
        sees qualified IDs.

    Designed for the self-node specifically, but works for any node-id
    prefix.
    """

    # Method names whose response dict is a single session (top-level
    # sessionId field).
    _SINGLE_SESSION_METHODS = {
        "create_session", "launch_with_options", "get_session",
        "kill_session", "relaunch_session", "delete_session",
        "archive_session", "unarchive_session", "rename_session",
        "set_pen", "set_permission_mode",
    }

    def __init__(self, inner: Any, node_id: str) -> None:
        self._inner = inner
        self._node_id = node_id
        self._prefix = f"{node_id}:"

    @property
    def node_id(self) -> str:
        return self._node_id

    def _strip(self, sid: str) -> str:
        if sid and sid.startswith(self._prefix):
            return sid[len(self._prefix):]
        return sid

    def _qualify(self, sid: str) -> str:
        if sid and ":" not in sid:
            return f"{self._prefix}{sid}"
        return sid

    def _qualify_in_dict(self, d: dict) -> None:
        sid = d.get("sessionId")
        if isinstance(sid, str):
            d["sessionId"] = self._qualify(sid)

    def _qualify_response(self, name: str, result: Any) -> Any:
        if not isinstance(result, dict):
            return result
        # list_sessions: {sessions: [{sessionId, ...}, ...]}
        sessions = result.get("sessions")
        if isinstance(sessions, list):
            for s in sessions:
                if isinstance(s, dict):
                    self._qualify_in_dict(s)
        # single-session responses (top-level sessionId)
        if name in self._SINGLE_SESSION_METHODS or "sessionId" in result:
            self._qualify_in_dict(result)
        return result

    def __getattr__(self, name: str) -> Callable[..., Awaitable[Any]]:
        if name.startswith("_"):
            raise AttributeError(name)
        method = getattr(self._inner, name)

        async def _call(**kwargs: Any) -> Any:
            # Strip our prefix from any session_id kwarg so the inner
            # client (and the wire format) sees raw IDs.
            sid = kwargs.get("session_id")
            if isinstance(sid, str):
                kwargs["session_id"] = self._strip(sid)
            result = await method(**kwargs)
            return self._qualify_response(name, result)

        _call.__name__ = name
        return _call


class RemoteNodeUnavailable(Exception):
    """Raised when a remote node has no live tunnel to handle a request."""

    def __init__(self, node_id: str):
        super().__init__(f"Remote node {node_id} unavailable")
        self.node_id = node_id


class RemoteNodeClient:
    """NodeClient implementation that forwards every call over the tunnel.

    Each attribute access returns a coroutine factory that serializes the
    call as a tunnel command. Method names map 1:1 to command types
    (e.g. `kill_session` → `{"type": "kill_session", ...}`).
    """

    def __init__(self, node_id: str, tunnel: Any):
        self._node_id = node_id
        self._tunnel = tunnel

    @property
    def node_id(self) -> str:
        return self._node_id

    def __getattr__(self, name: str) -> Callable[..., Awaitable[dict]]:
        if name.startswith("_"):
            raise AttributeError(name)

        async def _call(**kwargs: Any) -> dict:
            import dataclasses
            payload: dict[str, Any] = {"type": name}
            for k, v in kwargs.items():
                # Convert dataclass kwargs (e.g. LaunchOptions) to plain
                # dicts so they survive JSON serialization on the wire.
                if dataclasses.is_dataclass(v) and not isinstance(v, type):
                    v = {kk: vv for kk, vv in dataclasses.asdict(v).items() if vv is not None}
                payload[snake_to_camel(k)] = v
            return await self._tunnel.send_command(payload)

        _call.__name__ = name
        return _call


def resolve_node_client(
    session_or_node_id: str,
    *,
    local_ops: NodeOperations,
    node_registry: Any,
    ws_bridge: Any,
) -> tuple[NodeClient, str, bool]:
    """Resolve a (possibly prefixed) session id to (client, raw_sid, is_remote).

    - For a hub-local id, returns (local_ops, sid, False).
    - For a remote-prefixed id, returns (RemoteNodeClient, raw_sid, True).
    - Raises RemoteNodeUnavailable if the targeted node has no live tunnel.
    """
    node_id = ws_bridge.get_session_node_id(session_or_node_id) if session_or_node_id else ""
    is_remote = bool(node_id) and ws_bridge._is_remote_session(session_or_node_id)
    if not is_remote:
        return local_ops, session_or_node_id, False
    if not node_registry:
        raise RemoteNodeUnavailable(node_id)
    node = node_registry.get_node(node_id)
    if not node or not node.tunnel or not node.tunnel.connected:
        raise RemoteNodeUnavailable(node_id)
    raw_id = ws_bridge._raw_session_id(session_or_node_id)
    return RemoteNodeClient(node_id, node.tunnel), raw_id, True
