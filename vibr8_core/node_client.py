"""NodeClient — uniform interface for hub-side code to invoke node operations.

Both the hub's in-process node (today: LocalNodeClient = NodeOperations bound
to hub managers) and any remote node (RemoteNodeClient = tunnel transport)
expose the same surface. Hub-side code (routes, voice routing, computer-use,
etc.) never branches on local vs remote — it gets a NodeClient and calls
methods on it.

In Phase 4 the hub will stop instantiating its own NodeOperations and instead
talk to its own self-node over a loopback tunnel. At that point every
NodeClient is a RemoteNodeClient.
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


# NodeOperations already implements the protocol structurally — it IS a
# NodeClient when bound to in-process managers. Exporting an alias for clarity.
LocalNodeClient = NodeOperations


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
            payload: dict[str, Any] = {"type": name}
            for k, v in kwargs.items():
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
