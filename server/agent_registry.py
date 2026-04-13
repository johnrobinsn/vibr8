"""Pluggable computer-use agent registry.

Agents register themselves at import time via ``register_agent_type()``.
The session creation flow looks up the factory by ``type_id`` and calls it
to produce a ``ComputerUseAgent`` instance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

import av


@runtime_checkable
class AgentTarget(Protocol):
    """Interface for any target the agent can control (desktop or Android)."""

    async def start(self) -> Any: ...
    async def stop(self) -> None: ...
    async def get_frame(self) -> av.VideoFrame | None: ...
    async def inject(self, event: dict[str, Any]) -> None: ...


# Callback the factory can use to send status messages to browsers
# (e.g. "Loading vision model...") without depending on WsBridge.
StatusCallback = Callable[[dict[str, Any]], Awaitable[None]]

# Factory signature:  (session_id, target, config, status_cb) -> agent
AgentFactory = Callable[
    [str, AgentTarget, dict[str, Any], StatusCallback | None],
    Awaitable[Any],  # Returns ComputerUseAgent (Any to avoid circular import)
]


@dataclass
class AgentTypeInfo:
    """Metadata for a registered agent type."""

    type_id: str                                # e.g. "ui-tars"
    display_name: str                           # e.g. "UI-TARS 7B (local GPU)"
    factory: AgentFactory
    resource_type: str = "local-gpu"            # "local-gpu" | "api" | "hybrid"
    config_schema: dict[str, Any] = field(default_factory=dict)
    default_config: dict[str, Any] = field(default_factory=dict)


_registry: dict[str, AgentTypeInfo] = {}


def register_agent_type(info: AgentTypeInfo) -> None:
    """Register an agent type. Called at module import time."""
    _registry[info.type_id] = info


def get_agent_type(type_id: str) -> AgentTypeInfo | None:
    """Look up an agent type by ID."""
    return _registry.get(type_id)


def list_agent_types() -> list[AgentTypeInfo]:
    """Return all registered agent types."""
    return list(_registry.values())
