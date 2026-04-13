"""Computer-use agent protocol and shared types.

Defines the interface that any computer-use agent must implement
(UITarsAgent, future ClaudeComputerUseAgent, HybridAgent, etc.).
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable


class AgentMode(str, Enum):
    """Top-level mode for the computer-use agent."""
    WATCH = "watch"  # Observe and describe — no actions
    ACT = "act"      # Take actions toward a goal


class ExecutionMode(str, Enum):
    """How actions are gated in Act mode."""
    AUTO = "auto"        # Always execute immediately
    CONFIRM = "confirm"  # Always ask user before executing
    GATED = "gated"      # Auto if action parsed cleanly, else confirm


@runtime_checkable
class ComputerUseAgent(Protocol):
    """Interface for computer-use agents.

    Any class that implements these methods can be registered with
    WsBridge as a computer-use agent.  The ws_bridge routes browser
    messages to the appropriate methods.
    """

    session_id: str

    # ── Metadata ─────────────────────────────────────────────────────────

    @property
    def model_name(self) -> str:
        """Human-readable model identifier for display."""
        ...

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize the agent (connect to desktop, load model, etc.)."""
        ...

    async def stop(self) -> None:
        """Release all resources."""
        ...

    def on_message(self, cb: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        """Register callback for outgoing messages to browsers."""
        ...

    # ── Act mode ─────────────────────────────────────────────────────────

    def submit_task(self, task: str, mode: ExecutionMode = ExecutionMode.AUTO) -> None:
        """Submit a task. Interrupts any running task/watch first."""
        ...

    def interrupt(self) -> None:
        """Stop the current task loop."""
        ...

    def approve(self) -> None:
        """Approve a pending action (confirm/gated mode)."""
        ...

    def reject(self) -> None:
        """Reject a pending action (confirm/gated mode)."""
        ...

    # ── Watch mode ───────────────────────────────────────────────────────

    def watch_start(self, prompt: str | None = None, interval: float = 5.0) -> None:
        """Start watch mode — periodic observation with no actions."""
        ...

    def watch_stop(self) -> None:
        """Stop watch mode."""
        ...
