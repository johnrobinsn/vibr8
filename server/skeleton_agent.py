"""Skeleton computer-use agent — starter template.

Copy this file and fill in the inference logic. Implements the full
ComputerUseAgent protocol with FrameStream support.

See docs/computer-use-agents.md for the full developer guide.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

import av
from PIL import Image

from server.agent_registry import AgentTarget, StatusCallback
from server.computer_use_agent import ExecutionMode
from server.frame_stream import FrameStream

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

AGENT_NAME = "skeleton"
DISPLAY_NAME = "Skeleton Agent (template)"

# Default config values — override via agentConfig at session creation
DEFAULT_FPS = 5
DEFAULT_MAX_ITERATIONS = 30
DEFAULT_WAIT_AFTER_ACTION = 1.5  # seconds between actions


class SkeletonAgent:
    """Minimal computer-use agent with video frame streaming.

    Implements the ComputerUseAgent protocol. Uses FrameStream for
    continuous frame access instead of single-frame polling.

    Replace the TODO sections with your model's inference and action
    parsing logic.
    """

    def __init__(
        self,
        session_id: str,
        target: AgentTarget,
        stream: FrameStream,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        wait_after_action: float = DEFAULT_WAIT_AFTER_ACTION,
    ) -> None:
        self.session_id = session_id
        self._target = target
        self._stream = stream
        self._max_iterations = max_iterations
        self._wait_after_action = wait_after_action

        self._running = False
        self._loop_task: asyncio.Task[None] | None = None
        self._on_message: Callable[[dict[str, Any]], Awaitable[None]] | None = None

        # Confirm/reject gate for CONFIRM and GATED modes
        self._confirm_event: asyncio.Event | None = None
        self._confirm_approved: bool = False

        # Watch mode
        self._watch_task: asyncio.Task[None] | None = None
        self._watching = True  # start in watch mode (matches frontend default)

    # ── Metadata ─────────────────────────────────────────────────────────

    @property
    def model_name(self) -> str:
        # TODO: Return your model's name
        return "skeleton-model"

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        await self._target.start()
        await self._stream.start()
        self._running = True
        logger.info("[%s] Agent started for session %s", AGENT_NAME, self.session_id)

    async def stop(self) -> None:
        self._running = False
        self._cancel_loop()
        self._cancel_watch()
        await self._stream.stop()
        await self._target.stop()
        logger.info("[%s] Agent stopped for session %s", AGENT_NAME, self.session_id)

    def on_message(self, cb: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._on_message = cb

    # ── Act mode ─────────────────────────────────────────────────────────

    def submit_task(self, task: str, mode: ExecutionMode = ExecutionMode.AUTO) -> None:
        if self._watching:
            # In watch mode, treat user input as an observation query
            self._cancel_watch()
            self._watch_task = asyncio.create_task(self._watch_loop(task, interval=5.0))
            return
        self._cancel_loop()
        self._loop_task = asyncio.create_task(self._run_loop(task, mode))

    def interrupt(self) -> None:
        self._cancel_loop()
        asyncio.create_task(self._emit_status("idle"))

    def approve(self) -> None:
        self._confirm_approved = True
        if self._confirm_event:
            self._confirm_event.set()

    def reject(self) -> None:
        self._confirm_approved = False
        if self._confirm_event:
            self._confirm_event.set()

    # ── Watch mode ───────────────────────────────────────────────────────

    def watch_start(self, prompt: str | None = None, interval: float = 5.0) -> None:
        self._cancel_loop()
        self._cancel_watch()
        self._watching = True
        self._watch_task = asyncio.create_task(
            self._watch_loop(prompt or "Describe what you see on the screen.", interval)
        )

    def watch_stop(self) -> None:
        self._watching = False
        self._cancel_watch()
        asyncio.create_task(self._emit_status("idle"))

    # ── Act mode: main loop ──────────────────────────────────────────────

    async def _run_loop(self, task: str, mode: ExecutionMode) -> None:
        """Main agent loop: observe → think → act → repeat."""
        await self._emit_status("running")

        try:
            for iteration in range(1, self._max_iterations + 1):
                if not self._running:
                    break

                # 1. Get frames — use FrameStream for video context
                #    Single-frame agents can use self._stream.latest() instead.
                recent_frames = self._stream.recent_seconds(3.0)
                latest = self._stream.latest()

                if latest is None:
                    logger.warning("[%s] No frame yet (iteration %d)", AGENT_NAME, iteration)
                    await asyncio.sleep(1)
                    continue

                # 2. Convert frame(s) to format your model expects
                image = self._frame_to_image(latest)

                # 3. Run inference
                # TODO: Replace with your model's inference
                # For video models, pass `recent_frames` instead of a single image.
                # Each TimestampedFrame has .frame (av.VideoFrame) and .timestamp (float).
                thought, action_type, action_params = await self._infer(image, task)

                # 4. Display result
                display = ""
                if thought:
                    display += f"**Thought:** {thought}\n\n"
                if action_type:
                    display += f"**Action:** `{action_type}({action_params})`"
                else:
                    display += "*No action determined*"
                await self._emit_assistant(display, iteration=iteration)

                # 5. Gate execution (CONFIRM/GATED modes)
                if action_type and action_type != "finished":
                    should_execute = await self._gate_execution(action_type, mode, iteration)
                    if should_execute:
                        await self._execute_action(action_type, action_params)
                    elif should_execute is None:
                        await self._emit_result("Action rejected by user", iteration)
                        return

                # 6. Check for completion
                if action_type == "finished":
                    await self._emit_result("Task completed", iteration)
                    return

                # 7. Wait for UI to update after action
                await asyncio.sleep(self._wait_after_action)

            await self._emit_result(f"Reached max iterations ({self._max_iterations})", self._max_iterations)

        except asyncio.CancelledError:
            logger.info("[%s] Loop cancelled for session %s", AGENT_NAME, self.session_id)
        except Exception:
            logger.exception("[%s] Loop error for session %s", AGENT_NAME, self.session_id)
            await self._emit_assistant("*Agent encountered an error.*")
        finally:
            await self._emit_status("idle")

    # ── Watch mode: observation loop ─────────────────────────────────────

    async def _watch_loop(self, prompt: str, interval: float) -> None:
        await self._emit_status("watching")

        try:
            while self._running:
                latest = self._stream.latest()
                if latest is None:
                    await asyncio.sleep(1)
                    continue

                image = self._frame_to_image(latest)

                # TODO: Replace with your model's observation inference
                description = await self._observe(image, prompt)

                if description:
                    await self._emit({
                        "type": "observation",
                        "text": description,
                        "timestamp": int(time.time() * 1000),
                    })

                await asyncio.sleep(interval)

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[%s] Watch error for session %s", AGENT_NAME, self.session_id)
        finally:
            await self._emit_status("idle")

    # ── Inference stubs ──────────────────────────────────────────────────
    # TODO: Replace these with your actual model inference.

    async def _infer(
        self, image: Image.Image, task: str,
    ) -> tuple[str, str, dict[str, Any]]:
        """Run inference to determine the next action.

        Args:
            image: Current screenshot as PIL Image.
            task: The user's goal / instruction.

        Returns:
            (thought, action_type, action_params) tuple.
            action_type examples: "click", "type", "scroll", "press", "finished"
            action_params examples: {"x": 0.5, "y": 0.3}, {"text": "hello"}
        """
        # Stub: always reports finished after one step
        return ("This is a skeleton agent — no model loaded.", "finished", {})

    async def _observe(self, image: Image.Image, prompt: str) -> str:
        """Run observation inference (watch mode).

        Args:
            image: Current screenshot as PIL Image.
            prompt: Observation prompt from the user.

        Returns:
            Natural-language description of what's on screen.
        """
        # Stub
        return "Skeleton agent: observation not implemented."

    # ── Action execution ─────────────────────────────────────────────────

    async def _execute_action(self, action_type: str, params: dict[str, Any]) -> None:
        """Execute an action on the target device.

        Translates high-level actions (click, type, scroll) into
        inject() calls with the correct event format.

        TODO: Extend with your model's action vocabulary.
        """
        if action_type == "click":
            x, y = params.get("x", 0.5), params.get("y", 0.5)
            await self._target.inject({"type": "mousedown", "x": x, "y": y, "button": 0})
            await asyncio.sleep(0.05)
            await self._target.inject({"type": "mouseup", "x": x, "y": y, "button": 0})

        elif action_type == "type":
            text = params.get("text", "")
            for char in text:
                await self._target.inject({"type": "keydown", "key": char})
                await self._target.inject({"type": "keyup", "key": char})
                await asyncio.sleep(0.02)

        elif action_type == "press":
            key = params.get("key", "")
            await self._target.inject({"type": "keydown", "key": key})
            await asyncio.sleep(0.05)
            await self._target.inject({"type": "keyup", "key": key})

        elif action_type == "scroll":
            x, y = params.get("x", 0.5), params.get("y", 0.5)
            direction = params.get("direction", "down")
            delta = -120 if direction == "up" else 120
            await self._target.inject({"type": "wheel", "x": x, "y": y, "deltaY": delta})

        else:
            logger.warning("[%s] Unknown action type: %s", AGENT_NAME, action_type)

    # ── Confirmation gate ────────────────────────────────────────────────

    async def _gate_execution(
        self, action_type: str, mode: ExecutionMode, iteration: int,
    ) -> bool | None:
        """Gate action execution based on mode.

        Returns True to execute, False to skip, None to stop (rejected).
        """
        if mode == ExecutionMode.AUTO:
            return True

        if mode == ExecutionMode.GATED and action_type:
            return True  # parsed cleanly → auto-execute

        # CONFIRM or GATED fallthrough: ask user
        await self._emit_status("confirming")
        await self._emit({
            "type": "confirm",
            "step": iteration,
            "action_type": action_type,
            "action_summary": action_type,
            "thought": "",
        })

        self._confirm_event = asyncio.Event()
        self._confirm_approved = False

        try:
            await asyncio.wait_for(self._confirm_event.wait(), timeout=120)
        except asyncio.TimeoutError:
            self._confirm_approved = False

        self._confirm_event = None
        await self._emit_status("running")
        return True if self._confirm_approved else None

    # ── Frame conversion ─────────────────────────────────────────────────

    @staticmethod
    def _frame_to_image(frame: av.VideoFrame) -> Image.Image:
        """Convert av.VideoFrame to PIL Image.

        Override if your model needs a specific resolution or format.
        """
        return frame.to_image()

    # ── Message helpers ──────────────────────────────────────────────────

    async def _emit(self, msg: dict[str, Any]) -> None:
        if self._on_message:
            await self._on_message(msg)

    async def _emit_status(self, status: str) -> None:
        await self._emit({"type": "status_change", "status": status})

    async def _emit_assistant(self, content: str, iteration: int = 0) -> None:
        msg: dict[str, Any] = {
            "type": "assistant",
            "message": {
                "id": f"agent_{uuid.uuid4().hex[:12]}",
                "role": "assistant",
                "model": self.model_name,
                "content": [{"type": "text", "text": content}],
                "stop_reason": "end_turn",
                "type": "message",
            },
            "parent_tool_use_id": None,
            "timestamp": int(time.time() * 1000),
        }
        if iteration:
            msg["iteration"] = iteration
        await self._emit(msg)

    async def _emit_result(self, summary: str, iterations: int) -> None:
        await self._emit({
            "type": "result",
            "content": summary,
            "iterations": iterations,
            "timestamp": int(time.time() * 1000),
        })

    # ── Internal ─────────────────────────────────────────────────────────

    def _cancel_loop(self) -> None:
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
        if self._confirm_event:
            self._confirm_event.set()

    def _cancel_watch(self) -> None:
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()


# ── Factory & Registration ───────────────────────────────────────────────────


async def create_skeleton_agent(
    session_id: str,
    target: AgentTarget,
    config: dict[str, Any],
    status_cb: StatusCallback | None = None,
) -> SkeletonAgent:
    """Factory function for SkeletonAgent.

    This is what the agent registry calls to create an instance.
    Load your model here and send status updates via status_cb.
    """
    # TODO: Load your model here. Example:
    # if status_cb:
    #     await status_cb({"type": "status_change", "status": "running"})
    #     await status_cb({
    #         "type": "assistant",
    #         "message": {"role": "assistant", "content": "Loading model..."},
    #         "parent_tool_use_id": None,
    #         "timestamp": int(time.time() * 1000),
    #     })
    # model = await load_my_model()

    fps = config.get("fps", DEFAULT_FPS)
    stream = FrameStream(target, max_buffer=300, target_fps=fps)

    agent = SkeletonAgent(
        session_id=session_id,
        target=target,
        stream=stream,
        max_iterations=config.get("max_iterations", DEFAULT_MAX_ITERATIONS),
        wait_after_action=config.get("wait_after_action", DEFAULT_WAIT_AFTER_ACTION),
    )
    return agent


# Register with the agent registry.
# This runs at import time — add `import server.skeleton_agent  # noqa: F401`
# to server/main.py to make it available.
#
# NOTE: Uncomment these lines when your agent is ready to use.
#
# from server.agent_registry import register_agent_type, AgentTypeInfo
#
# register_agent_type(AgentTypeInfo(
#     type_id=AGENT_NAME,
#     display_name=DISPLAY_NAME,
#     factory=create_skeleton_agent,
#     resource_type="api",  # or "local-gpu" or "hybrid"
#     config_schema={
#         "type": "object",
#         "properties": {
#             "fps": {"type": "number", "default": DEFAULT_FPS},
#             "max_iterations": {"type": "integer", "default": DEFAULT_MAX_ITERATIONS},
#             "wait_after_action": {"type": "number", "default": DEFAULT_WAIT_AFTER_ACTION},
#         },
#     },
#     default_config={
#         "fps": DEFAULT_FPS,
#         "max_iterations": DEFAULT_MAX_ITERATIONS,
#         "wait_after_action": DEFAULT_WAIT_AFTER_ACTION,
#     },
# ))
