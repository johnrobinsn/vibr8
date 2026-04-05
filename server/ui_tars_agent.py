"""UI-TARS agent — vision-language model that controls desktop GUIs.

Takes screenshots via WebRTC (DesktopTarget), sends to vLLM (OpenAI-
compatible API), parses model output (Thought/Action), and executes
actions back through the same WebRTC data channel.

Implements the ComputerUseAgent protocol with Watch and Act modes.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import time
from typing import Any, Callable, Awaitable

import av
import httpx
from PIL import Image

from server.computer_use_agent import ExecutionMode
from server.desktop_target import DesktopTarget
from server.ui_tars_actions import parse_action, execute_action

logger = logging.getLogger(__name__)

# Default vLLM endpoint
DEFAULT_API_URL = "http://localhost:8000/v1/chat/completions"
DEFAULT_MODEL = "ByteDance-Seed/UI-TARS-1.5-7B"
DEFAULT_MAX_ITERATIONS = 50
DEFAULT_WAIT_AFTER_ACTION = 1.5  # seconds

# UI-TARS system prompt (from official SDK)
SYSTEM_PROMPT = """You are a GUI agent. You are given a task and your action history, with screenshots. You need to perform the next action to complete the task.

## Output Format
```
Thought: ...
Action: ...
```

## Action Space
click(start_box='[x1, y1, x2, y2]')
left_double(start_box='[x1, y1, x2, y2]')
right_single(start_box='[x1, y1, x2, y2]')
drag(start_box='[x1, y1, x2, y2]', end_box='[x3, y3, x4, y4]')
hotkey(key='')
type(content='') #If you want to submit your input, use "\\n" at the end of `content`.
scroll(start_box='[x1, y1, x2, y2]', direction='down or up or right or left')
wait() #Sleep for 5s and take a screenshot to check for any changes.
finished()
call_user() # Submit the task and call the user when the task is unsolvable, or when you need the user's help.

## Note
- Write a small plan and finally summarize your next action (with its target element) in one sentence in `Thought` part.

## User Instruction
"""

OBSERVE_PROMPT = "Describe what you see on the screen in 2-3 sentences."


class UITarsAgent:
    """Autonomous desktop agent powered by UI-TARS vision-language model.

    Implements the ComputerUseAgent protocol — supports Watch mode
    (periodic observation) and Act mode (goal-directed actions with
    auto/confirm/gated execution).
    """

    def __init__(
        self,
        session_id: str,
        desktop_target: DesktopTarget,
        api_url: str = DEFAULT_API_URL,
        model: str = DEFAULT_MODEL,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        wait_after_action: float = DEFAULT_WAIT_AFTER_ACTION,
    ) -> None:
        self.session_id = session_id
        self._target = desktop_target
        self._api_url = api_url
        self._model = model
        self._max_iterations = max_iterations
        self._wait_after_action = wait_after_action

        self._running = False
        self._loop_task: asyncio.Task[None] | None = None
        self._on_message: Callable[[dict[str, Any]], Awaitable[None]] | None = None

        # Chat history for multi-turn context
        self._history: list[dict[str, Any]] = []

        # Confirm/reject gate (Act mode: confirm and gated)
        self._confirm_event: asyncio.Event | None = None
        self._confirm_approved: bool = False

        # Watch mode state
        self._watch_task: asyncio.Task[None] | None = None

    # ── Lifecycle ──────────────────��─────────────────────────────────────────

    async def start(self) -> None:
        """Initialize the desktop target (WebRTC peer connection)."""
        await self._target.start()
        self._running = True
        logger.info(
            "[ui-tars] Agent started for session %s (%dx%d)",
            self.session_id,
            self._target.native_width,
            self._target.native_height,
        )

    async def stop(self) -> None:
        """Stop agent and release resources."""
        self._running = False
        self._cancel_loop()
        self._cancel_watch()
        await self._target.stop()
        logger.info("[ui-tars] Agent stopped for session %s", self.session_id)

    def on_message(self, cb: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        """Register callback for outgoing messages to browsers."""
        self._on_message = cb

    # ── Act mode: task submission ───────────────��────────────────────────────

    def submit_task(self, task: str, mode: ExecutionMode = ExecutionMode.AUTO) -> None:
        """Submit a new task. Interrupts any running task/watch first."""
        self._cancel_loop()
        self._cancel_watch()
        self._history = []
        self._loop_task = asyncio.create_task(self._run_loop(task, mode))

    def interrupt(self) -> None:
        """Interrupt the running loop."""
        self._cancel_loop()
        asyncio.create_task(self._emit_status("idle"))

    def approve(self) -> None:
        """Approve a pending action (confirm/gated mode)."""
        self._confirm_approved = True
        if self._confirm_event:
            self._confirm_event.set()

    def reject(self) -> None:
        """Reject a pending action (confirm/gated mode)."""
        self._confirm_approved = False
        if self._confirm_event:
            self._confirm_event.set()

    # ── Watch mode ────────────────────────���──────────────────���───────────────

    def watch_start(self, prompt: str | None = None, interval: float = 5.0) -> None:
        """Start watch mode — periodic observation with no actions."""
        self._cancel_loop()
        self._cancel_watch()
        self._watch_task = asyncio.create_task(
            self._watch_loop(prompt or OBSERVE_PROMPT, interval)
        )

    def watch_stop(self) -> None:
        """Stop watch mode."""
        self._cancel_watch()
        asyncio.create_task(self._emit_status("idle"))

    # ── Internal helpers ���───────────────────────────────���────────────────────

    def _cancel_loop(self) -> None:
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
        # Unblock any pending confirmation
        if self._confirm_event:
            self._confirm_event.set()

    def _cancel_watch(self) -> None:
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()

    # ── Act mode: agent loop ─────────��─────────────────────────���─────────────

    async def _run_loop(self, task: str, mode: ExecutionMode) -> None:
        """Main agent loop: screenshot → model → act ��� repeat."""
        await self._emit_status("running")

        try:
            for iteration in range(1, self._max_iterations + 1):
                if not self._running:
                    break

                # 1. Capture screenshot
                frame = await self._target.get_frame()
                if frame is None:
                    logger.warning("[ui-tars] No frame captured, retrying...")
                    await asyncio.sleep(1)
                    continue

                # 2. Convert to 1000×1000 PNG base64
                screenshot_b64 = self._frame_to_png_b64(frame)

                # 3. Build messages
                messages = self._build_messages(task, screenshot_b64)

                # 4. Call API
                response_text = await self._call_api(messages)
                if not response_text:
                    await self._emit_assistant(f"*[Iteration {iteration}]* API call failed, retrying...")
                    await asyncio.sleep(2)
                    continue

                # 5. Parse action
                action = parse_action(response_text)

                # 6. Emit thought/action to browsers
                display_text = self._format_action_display(action, response_text)
                await self._emit_assistant(display_text, iteration=iteration)

                # 7. Record in history for multi-turn
                self._history.append({
                    "role": "assistant",
                    "content": response_text,
                })

                # 8. Execute (gated by execution mode)
                if action.action_type:
                    should_execute = await self._gate_execution(action, mode, iteration)
                    if should_execute:
                        termination = await execute_action(action, self._target)
                        if termination:
                            reason = "Task completed" if termination == "finished" else "Agent needs user input"
                            await self._emit_result(reason, iteration)
                            return
                    elif should_execute is None:
                        # Rejected — stop the loop
                        await self._emit_result("Action rejected by user", iteration)
                        return

                # 9. Wait for UI to update
                await asyncio.sleep(self._wait_after_action)

            # Max iterations reached
            await self._emit_result(f"Reached max iterations ({self._max_iterations})", self._max_iterations)

        except asyncio.CancelledError:
            logger.info("[ui-tars] Loop cancelled for session %s", self.session_id)
        except Exception:
            logger.exception("[ui-tars] Loop error for session %s", self.session_id)
            await self._emit_assistant("*Agent encountered an error.*")
        finally:
            await self._emit_status("idle")

    async def _gate_execution(
        self, action, mode: ExecutionMode, iteration: int,
    ) -> bool | None:
        """Decide whether to execute an action based on execution mode.

        Returns True to execute, False to skip (but continue loop),
        or None to stop the loop (rejected).
        """
        if mode == ExecutionMode.AUTO:
            return True

        if mode == ExecutionMode.GATED:
            # Auto-execute if action parsed cleanly (has a type)
            if action.action_type and action.action_type not in ("finished", "call_user"):
                return True
            # Fall through to confirm for unparseable actions

        # CONFIRM (or GATED fallthrough): ask user
        await self._emit_status("confirming")
        await self._emit({
            "type": "confirm",
            "step": iteration,
            "action_type": action.action_type,
            "action_summary": self._action_summary(action),
            "thought": action.thought,
        })

        self._confirm_event = asyncio.Event()
        self._confirm_approved = False

        try:
            await asyncio.wait_for(self._confirm_event.wait(), timeout=120)
        except asyncio.TimeoutError:
            self._confirm_approved = False

        self._confirm_event = None
        await self._emit_status("running")

        if self._confirm_approved:
            return True
        return None  # Rejected → stop loop

    @staticmethod
    def _action_summary(action) -> str:
        """Human-readable one-line action summary."""
        if not action.action_type:
            return "unknown action"
        parts = [action.action_type]
        if action.params:
            param_str = ", ".join(f"{k}={v}" for k, v in action.params.items())
            parts.append(f"({param_str})")
        return "".join(parts)

    @staticmethod
    def _format_action_display(action, response_text: str) -> str:
        """Format action for display in chat."""
        display_text = ""
        if action.thought:
            display_text += f"**Thought:** {action.thought}\n\n"
        if action.action_type:
            display_text += f"**Action:** `{action.action_type}"
            if action.params:
                param_str = ", ".join(f"{k}={v}" for k, v in action.params.items())
                display_text += f"({param_str})"
            display_text += "`"
        elif not action.thought:
            display_text = f"```\n{response_text[:500]}\n```"
        return display_text

    # ── Watch mode: observation loop ──────────────��────────────────────���─────

    async def _watch_loop(self, prompt: str, interval: float) -> None:
        """Watch loop: screenshot → observe prompt → emit description → sleep."""
        await self._emit_status("watching")

        try:
            while self._running:
                frame = await self._target.get_frame()
                if frame is None:
                    await asyncio.sleep(1)
                    continue

                screenshot_b64 = self._frame_to_png_b64(frame)

                messages: list[dict[str, Any]] = [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}},
                    ],
                }]

                response_text = await self._call_api(messages)
                if response_text:
                    await self._emit({
                        "type": "observation",
                        "text": response_text,
                        "timestamp": int(time.time() * 1000),
                    })

                await asyncio.sleep(interval)

        except asyncio.CancelledError:
            logger.info("[ui-tars] Watch cancelled for session %s", self.session_id)
        except Exception:
            logger.exception("[ui-tars] Watch error for session %s", self.session_id)
        finally:
            await self._emit_status("idle")

    # ── API ��─────────────────────────────────────────────────────────────────

    def _build_messages(self, task: str, screenshot_b64: str) -> list[dict[str, Any]]:
        """Build the chat messages for the API call."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT + task},
        ]

        # Add history (previous assistant responses as context)
        for entry in self._history:
            messages.append(entry)

        # Current screenshot
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"},
                },
            ],
        })

        return messages

    async def _call_api(self, messages: list[dict[str, Any]]) -> str:
        """POST to vLLM OpenAI-compatible API. Returns response text or empty string."""
        body = {
            "model": self._model,
            "messages": messages,
            "max_tokens": 512,
            "temperature": 0.0,
        }
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    self._api_url,
                    json=body,
                    timeout=180,
                )
                r.raise_for_status()
                data = r.json()
                return data["choices"][0]["message"]["content"]
        except Exception as exc:
            logger.error("[ui-tars] API call failed: %s", exc)
            return ""

    # ── Screenshot conversion ──────────────────────────────────────────���─────

    @staticmethod
    def _frame_to_png_b64(frame: av.VideoFrame) -> str:
        """Convert av.VideoFrame to 1000×1000 PNG base64 string."""
        img = frame.to_image()
        img = img.resize((1000, 1000), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")

    # ���─ Message emission ──────────────────────��──────────────────────────────

    async def _emit(self, msg: dict[str, Any]) -> None:
        if self._on_message:
            await self._on_message(msg)

    async def _emit_status(self, status: str) -> None:
        await self._emit({"type": "status_change", "status": status})

    async def _emit_assistant(self, content: str, iteration: int = 0) -> None:
        msg: dict[str, Any] = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": content,
            },
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
