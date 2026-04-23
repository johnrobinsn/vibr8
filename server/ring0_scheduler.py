"""Ring0 Scheduled Tasks — background task execution with review queue.

Tasks run on a schedule (hourly/daily/weekly/once) in sandboxed Claude CLI
sessions. Results accumulate in a review queue for the user to inspect via
Ring0.

Persistence: task definitions in ~/.vibr8/ring0/tasks/*.json,
             queue results in ~/.vibr8/ring0/queue/*.json.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from server.cli_launcher import CliLauncher
    from server.ws_bridge import WsBridge

logger = logging.getLogger(__name__)

VIBR8_DIR = Path.home() / ".vibr8"
TASKS_DIR = VIBR8_DIR / "ring0" / "tasks"
QUEUE_DIR = VIBR8_DIR / "ring0" / "queue"

EXECUTION_TIMEOUT = 600  # 10 minutes max per task execution
MISSED_RUN_TIMEOUT = 120  # 2 minutes for catch-up tasks on startup
STARTUP_GRACE_SECONDS = 30  # delay before firing missed runs
STALENESS_THRESHOLD = 86400  # 24h — skip runs older than this
MAX_CONCURRENT_TASKS = 1  # only 1 task CLI at a time
WATCHDOG_INTERVALS = (60, 120, 300)  # seconds at which to log warnings


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


# ── Data Models ──────────────────────────────────────────────────────────────


@dataclass
class TaskDefinition:
    id: str = ""
    name: str = ""
    prompt: str = ""
    schedule: str = "daily"  # hourly | daily | weekly | once
    priority: str = "normal"  # normal | high | urgent
    enabled: bool = True
    created_at: float = 0.0
    last_run_at: float = 0.0
    next_run_at: float = 0.0
    run_if_missed: bool = True
    project_dir: str = ""
    model: str = ""
    schedule_hour: int = 9
    schedule_minute: int = 0
    schedule_day: int = 0  # 0=Mon..6=Sun
    schedule_at: float = 0.0  # for "once"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskDefinition:
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class TaskResult:
    id: str = ""
    task_id: str = ""
    task_name: str = ""
    output: str = ""
    status: str = "completed"  # completed | failed | timeout
    priority: str = "normal"
    created_at: float = 0.0
    reviewed: bool = False
    review_action: str = ""  # done | defer | delegate | followup
    reviewed_at: float = 0.0
    execution_cost_usd: float = 0.0
    is_rollup: bool = False
    run_count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskResult:
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})


# ── Schedule Computation ─────────────────────────────────────────────────────


def compute_next_run(task: TaskDefinition, after: Optional[float] = None) -> float:
    """Compute the next run timestamp for a task, after the given time."""
    if after is None:
        after = time.time()

    if task.schedule == "once":
        return task.schedule_at

    now_dt = datetime.fromtimestamp(after)

    if task.schedule == "hourly":
        # Next occurrence of :MM after `after`
        candidate = now_dt.replace(minute=task.schedule_minute, second=0, microsecond=0)
        if candidate.timestamp() <= after:
            candidate += timedelta(hours=1)
        return candidate.timestamp()

    if task.schedule == "daily":
        # Next occurrence of HH:MM after `after`
        candidate = now_dt.replace(
            hour=task.schedule_hour, minute=task.schedule_minute,
            second=0, microsecond=0,
        )
        if candidate.timestamp() <= after:
            candidate += timedelta(days=1)
        return candidate.timestamp()

    if task.schedule == "weekly":
        # Next occurrence of day-of-week at HH:MM after `after`
        candidate = now_dt.replace(
            hour=task.schedule_hour, minute=task.schedule_minute,
            second=0, microsecond=0,
        )
        days_ahead = task.schedule_day - candidate.weekday()
        if days_ahead < 0 or (days_ahead == 0 and candidate.timestamp() <= after):
            days_ahead += 7
        candidate += timedelta(days=days_ahead)
        return candidate.timestamp()

    # Unknown schedule type — far future
    logger.warning("[scheduler] Unknown schedule type %r for task %s", task.schedule, task.id)
    return after + 86400 * 365


# ── Task Queue ───────────────────────────────────────────────────────────────


class TaskQueue:
    """Manages the review queue — results from task executions."""

    def __init__(self) -> None:
        self._results: dict[str, TaskResult] = {}
        self._load()

    def _load(self) -> None:
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        for path in QUEUE_DIR.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                result = TaskResult.from_dict(data)
                self._results[result.id] = result
            except Exception:
                logger.exception("[scheduler] Failed to load queue item %s", path)

    def _save_result(self, result: TaskResult) -> None:
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        path = QUEUE_DIR / f"{result.id}.json"
        path.write_text(json.dumps(result.to_dict(), indent=2))

    def _delete_file(self, result_id: str) -> None:
        path = QUEUE_DIR / f"{result_id}.json"
        path.unlink(missing_ok=True)

    def add(self, result: TaskResult) -> None:
        """Add a result to the queue. Handles rollup for existing unreviewed results."""
        # Check for existing unreviewed result from same task (rollup)
        for existing in self._results.values():
            if existing.task_id == result.task_id and not existing.reviewed:
                existing.run_count += 1
                existing.is_rollup = True
                existing.output += f"\n\n--- Run {existing.run_count} ({datetime.fromtimestamp(result.created_at).strftime('%Y-%m-%d %H:%M')}) ---\n\n{result.output}"
                existing.created_at = result.created_at  # update to latest
                if result.status == "failed":
                    existing.status = "failed"  # escalate
                existing.execution_cost_usd += result.execution_cost_usd
                self._save_result(existing)
                logger.info("[scheduler] Rolled up result for task %s (run %d)", result.task_id, existing.run_count)
                return

        self._results[result.id] = result
        self._save_result(result)

    def list_pending(self) -> list[TaskResult]:
        """Return unreviewed results, sorted by priority then created_at."""
        priority_order = {"urgent": 0, "high": 1, "normal": 2}
        pending = [r for r in self._results.values() if not r.reviewed]
        # Deferred items (review_action == "defer") go to the bottom
        pending.sort(key=lambda r: (
            1 if r.review_action == "defer" else 0,
            priority_order.get(r.priority, 2),
            r.created_at,
        ))
        return pending

    def list_reviewed(self) -> list[TaskResult]:
        """Return reviewed results, most recent first."""
        reviewed = [r for r in self._results.values() if r.reviewed]
        reviewed.sort(key=lambda r: r.reviewed_at, reverse=True)
        return reviewed

    def list_all(self) -> list[TaskResult]:
        """Return all results, pending first then reviewed."""
        return self.list_pending() + self.list_reviewed()

    def get(self, result_id: str) -> Optional[TaskResult]:
        return self._results.get(result_id)

    def count_pending(self) -> int:
        return sum(1 for r in self._results.values() if not r.reviewed)

    def mark_reviewed(self, result_id: str, action: str) -> bool:
        result = self._results.get(result_id)
        if not result:
            return False
        result.reviewed = True
        result.review_action = action
        result.reviewed_at = time.time()
        self._save_result(result)
        return True

    def highest_pending_priority(self) -> str:
        """Return the highest priority among pending results."""
        priorities = [r.priority for r in self._results.values() if not r.reviewed]
        if not priorities:
            return "normal"
        for p in ("urgent", "high", "normal"):
            if p in priorities:
                return p
        return "normal"


# ── Task Scheduler ───────────────────────────────────────────────────────────


class TaskScheduler:
    """Manages scheduled task definitions and orchestrates execution."""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskDefinition] = {}
        self._queue = TaskQueue()
        self._launcher: Optional[CliLauncher] = None
        self._ws_bridge: Optional[WsBridge] = None
        self._loop_task: Optional[asyncio.Task] = None
        self._executing: set[str] = set()  # task IDs currently running
        self._execution_tasks: dict[str, asyncio.Task] = {}
        self._exec_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
        self._wake_event = asyncio.Event()
        self._stopped = False
        self._load_tasks()

    @property
    def queue(self) -> TaskQueue:
        return self._queue

    def set_dependencies(self, launcher: CliLauncher, ws_bridge: WsBridge) -> None:
        self._launcher = launcher
        self._ws_bridge = ws_bridge

    def _load_tasks(self) -> None:
        TASKS_DIR.mkdir(parents=True, exist_ok=True)
        for path in TASKS_DIR.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                task = TaskDefinition.from_dict(data)
                self._tasks[task.id] = task
            except Exception:
                logger.exception("[scheduler] Failed to load task %s", path)
        if self._tasks:
            logger.info("[scheduler] Loaded %d task(s)", len(self._tasks))

    def _save_task(self, task: TaskDefinition) -> None:
        TASKS_DIR.mkdir(parents=True, exist_ok=True)
        path = TASKS_DIR / f"{task.id}.json"
        path.write_text(json.dumps(task.to_dict(), indent=2))

    def _delete_task_file(self, task_id: str) -> None:
        path = TASKS_DIR / f"{task_id}.json"
        path.unlink(missing_ok=True)

    # ── CRUD ─────────────────────────────────────────────────────────────

    def create_task(
        self,
        name: str,
        prompt: str,
        schedule: str = "daily",
        priority: str = "normal",
        schedule_hour: int = 9,
        schedule_minute: int = 0,
        schedule_day: int = 0,
        project_dir: str = "",
        model: str = "",
        run_if_missed: bool = True,
    ) -> TaskDefinition:
        task = TaskDefinition(
            id=_gen_id("task"),
            name=name,
            prompt=prompt,
            schedule=schedule,
            priority=priority,
            enabled=True,
            created_at=time.time(),
            schedule_hour=schedule_hour,
            schedule_minute=schedule_minute,
            schedule_day=schedule_day,
            project_dir=project_dir,
            model=model,
            run_if_missed=run_if_missed,
        )

        if schedule == "once":
            # For one-shot tasks, schedule_at must be set separately or defaults to now + 1min
            task.schedule_at = time.time() + 60

        task.next_run_at = compute_next_run(task)
        self._tasks[task.id] = task
        self._save_task(task)
        self._wake_event.set()  # wake the scheduler loop
        logger.info("[scheduler] Created task %s (%s), next run at %s",
                     task.id, task.name, datetime.fromtimestamp(task.next_run_at).strftime("%Y-%m-%d %H:%M"))
        return task

    def update_task(self, task_id: str, **updates: Any) -> Optional[TaskDefinition]:
        task = self._tasks.get(task_id)
        if not task:
            return None
        schedule_changed = False
        for key, value in updates.items():
            if hasattr(task, key) and key not in ("id", "created_at"):
                setattr(task, key, value)
                if key in ("schedule", "schedule_hour", "schedule_minute", "schedule_day", "schedule_at"):
                    schedule_changed = True
        if schedule_changed:
            task.next_run_at = compute_next_run(task)
        self._save_task(task)
        self._wake_event.set()
        return task

    def delete_task(self, task_id: str) -> bool:
        if task_id not in self._tasks:
            return False
        del self._tasks[task_id]
        self._delete_task_file(task_id)
        self._wake_event.set()
        logger.info("[scheduler] Deleted task %s", task_id)
        return True

    def get_task(self, task_id: str) -> Optional[TaskDefinition]:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[TaskDefinition]:
        tasks = list(self._tasks.values())
        tasks.sort(key=lambda t: t.next_run_at)
        return tasks

    # ── Scheduler Loop ───────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the scheduler background loop."""
        self._stopped = False
        self._loop_task = asyncio.create_task(self._run_loop())
        logger.info("[scheduler] Started")
        asyncio.create_task(self._delayed_check_missed_runs())

    async def _delayed_check_missed_runs(self) -> None:
        """Wait for startup grace period, then check for missed runs."""
        logger.info("[scheduler] Waiting %ds before checking missed runs", STARTUP_GRACE_SECONDS)
        await asyncio.sleep(STARTUP_GRACE_SECONDS)
        if not self._stopped:
            await self._check_missed_runs()

    async def stop(self) -> None:
        """Stop the scheduler background loop."""
        self._stopped = True
        self._wake_event.set()
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        for task_id, atask in list(self._execution_tasks.items()):
            logger.info("[scheduler] Cancelling executing task %s on shutdown", task_id)
            atask.cancel()
        if self._execution_tasks:
            await asyncio.gather(*self._execution_tasks.values(), return_exceptions=True)
            self._execution_tasks.clear()
        logger.info("[scheduler] Stopped")

    async def _check_missed_runs(self) -> None:
        """On startup, execute any tasks that missed their scheduled run."""
        now = time.time()
        for task in self._tasks.values():
            if not task.enabled or task.schedule == "once":
                continue
            if task.run_if_missed and task.next_run_at > 0 and task.next_run_at < now:
                age = now - task.next_run_at
                if age > STALENESS_THRESHOLD:
                    logger.info(
                        "[scheduler] Skipping stale missed run for task %s (%s), "
                        "%.1fh overdue — rescheduling",
                        task.id, task.name, age / 3600,
                    )
                    task.next_run_at = compute_next_run(task, after=now)
                    self._save_task(task)
                    continue
                logger.info("[scheduler] Missed run for task %s (%s), executing now (%.1fh overdue)",
                             task.id, task.name, age / 3600)
                self._schedule_task_execution(task, is_missed_run=True)

    async def _run_loop(self) -> None:
        """Main scheduler loop — sleep until next task, then execute."""
        while not self._stopped:
            try:
                self._wake_event.clear()

                # Find next task to run
                now = time.time()
                next_task = None
                next_time = float("inf")

                for task in self._tasks.values():
                    if not task.enabled:
                        continue
                    if task.id in self._executing:
                        continue
                    if task.schedule == "once" and task.last_run_at > 0:
                        continue  # already ran
                    if 0 < task.next_run_at < next_time:
                        next_time = task.next_run_at
                        next_task = task

                if next_task is None:
                    # No tasks to run — wait for wake
                    logger.debug("[scheduler] No tasks scheduled, waiting for wake")
                    await self._wake_event.wait()
                    continue

                delay = next_time - now
                if delay > 0:
                    logger.debug("[scheduler] Next task %s (%s) in %.0fs",
                                  next_task.id, next_task.name, delay)
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=delay)
                        # Woke early — recalculate (task may have been added/removed)
                        continue
                    except asyncio.TimeoutError:
                        pass  # Time to execute

                # Re-check the task is still valid and due
                task = self._tasks.get(next_task.id)
                if not task or not task.enabled or task.id in self._executing:
                    continue
                if task.next_run_at > time.time():
                    continue  # not due yet (schedule changed)

                self._schedule_task_execution(task)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[scheduler] Error in scheduler loop")
                await asyncio.sleep(10)

    # ── Task Execution ───────────────────────────────────────────────────

    async def execute_task_now(self, task_id: str) -> Optional[str]:
        """Manually trigger a task execution. Returns error string or None on success."""
        task = self._tasks.get(task_id)
        if not task:
            return "Task not found"
        if task.id in self._executing:
            return "Task is already executing"
        self._schedule_task_execution(task)
        return None

    def _schedule_task_execution(self, task: TaskDefinition, *, is_missed_run: bool = False) -> None:
        """Create a tracked, semaphore-gated asyncio.Task for executing a task."""
        timeout = MISSED_RUN_TIMEOUT if is_missed_run else EXECUTION_TIMEOUT

        async def _guarded_execute() -> None:
            async with self._exec_semaphore:
                await self._execute_task(task, timeout=timeout)

        atask = asyncio.create_task(_guarded_execute())
        self._execution_tasks[task.id] = atask

        def _on_done(t: asyncio.Task) -> None:
            self._execution_tasks.pop(task.id, None)
            if t.cancelled():
                logger.info("[scheduler] Task %s was cancelled", task.id)
            elif t.exception():
                logger.error("[scheduler] Task %s raised unhandled exception: %s",
                             task.id, t.exception())

        atask.add_done_callback(_on_done)

    async def _execute_task(self, task: TaskDefinition, *, timeout: float = EXECUTION_TIMEOUT) -> None:
        """Execute a task: spawn session, wait for completion, capture results."""
        if not self._launcher or not self._ws_bridge:
            logger.error("[scheduler] Cannot execute task %s — dependencies not set", task.id)
            return

        if task.id in self._executing:
            logger.warning("[scheduler] Task %s already executing, skipping", task.id)
            return

        self._executing.add(task.id)
        session_id = f"task-{task.id.split('_')[1][:8]}-{secrets.token_hex(4)}"
        start_time = time.time()

        logger.info("[scheduler] Executing task %s (%s) in session %s (timeout=%ds)",
                     task.id, task.name, session_id, int(timeout))

        try:
            # Build prompt
            prompt = (
                f"You are executing a scheduled background task. "
                f"Your output will be reviewed by the user later.\n\n"
                f"Task: {task.name}\n\n"
                f"{task.prompt}\n\n"
                f"Be thorough but concise. Summarize your findings clearly."
            )

            # Determine working directory
            cwd = task.project_dir or str(VIBR8_DIR / "ring0")

            # Spawn a sandboxed Claude CLI session (no MCP tools)
            from server.cli_launcher import LaunchOptions
            options = LaunchOptions(
                sessionId=session_id,
                permissionMode="bypassPermissions",
                cwd=cwd,
                model=task.model or None,
                isBackgroundTask=True,
            )
            self._launcher.launch(options)

            # Wait for session to become connected
            connected = await self._wait_for_session_state(session_id, "connected", timeout=30)
            if not connected:
                raise RuntimeError("Session did not connect within 30s")

            # Submit the prompt
            await asyncio.sleep(1)  # brief delay for CLI to be ready
            err = await self._ws_bridge.submit_user_message(session_id, prompt)
            if err:
                raise RuntimeError(f"Failed to submit prompt: {err}")

            # Wait for session to go idle (task completed)
            watchdog = asyncio.create_task(
                self._task_watchdog(task.id, task.name, start_time)
            )
            try:
                completed = await self._wait_for_idle(session_id, timeout=timeout)
            finally:
                watchdog.cancel()

            # Capture the result
            output = self._extract_last_assistant_message(session_id)
            cost = self._extract_cost(session_id)

            if completed:
                status = "completed"
            else:
                status = "timeout"
                output = output or f"(Task timed out after {int(timeout)}s)"

            result = TaskResult(
                id=_gen_id("res"),
                task_id=task.id,
                task_name=task.name,
                output=output,
                status=status,
                priority=task.priority,
                created_at=time.time(),
                execution_cost_usd=cost,
            )
            self._queue.add(result)
            logger.info("[scheduler] Task %s completed: status=%s, cost=$%.4f",
                         task.id, status, cost)

            # Emit event
            await self._emit_completion_event(task, result)

        except Exception as e:
            logger.exception("[scheduler] Task %s execution failed: %s", task.id, e)
            # Store failure in queue
            result = TaskResult(
                id=_gen_id("res"),
                task_id=task.id,
                task_name=task.name,
                output=f"Execution error: {e}",
                status="failed",
                priority=task.priority,
                created_at=time.time(),
            )
            self._queue.add(result)
            await self._emit_completion_event(task, result)

        finally:
            elapsed = time.time() - start_time
            logger.info("[scheduler] Task %s (%s) finished in %.1fs", task.id, task.name, elapsed)
            self._executing.discard(task.id)

            # Kill the disposable execution session
            try:
                await self._launcher.kill(session_id)
            except Exception:
                pass

            # Update task schedule
            task.last_run_at = time.time()
            if task.schedule != "once":
                task.next_run_at = compute_next_run(task, after=time.time())
                logger.info("[scheduler] Task %s next run at %s",
                             task.id, datetime.fromtimestamp(task.next_run_at).strftime("%Y-%m-%d %H:%M"))
            self._save_task(task)

    async def _task_watchdog(self, task_id: str, task_name: str, start_time: float) -> None:
        """Log warnings at intervals while a task is running."""
        try:
            for threshold in WATCHDOG_INTERVALS:
                remaining = threshold - (time.time() - start_time)
                if remaining > 0:
                    await asyncio.sleep(remaining)
                logger.warning(
                    "[scheduler] Task %s (%s) still running after %.0fs",
                    task_id, task_name, time.time() - start_time,
                )
            await asyncio.sleep(float("inf"))
        except asyncio.CancelledError:
            pass

    async def _wait_for_session_state(self, session_id: str, target_state: str, timeout: float) -> bool:
        """Poll until session reaches target state or timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            info = self._launcher.get_session(session_id) if self._launcher else None
            if info and info.state == target_state:
                return True
            if info and info.state == "exited":
                return False
            await asyncio.sleep(0.5)
        return False

    async def _wait_for_idle(self, session_id: str, timeout: float) -> bool:
        """Wait for session to go idle (finished processing) or timeout."""
        deadline = time.time() + timeout
        # First wait for it to start running
        running_seen = False
        while time.time() < deadline:
            session = self._ws_bridge._sessions.get(session_id) if self._ws_bridge else None
            if session:
                is_running = session.state.get("is_running", False)
                if is_running:
                    running_seen = True
                elif running_seen:
                    # Was running, now idle — done
                    return True
            # Also check if CLI exited
            info = self._launcher.get_session(session_id) if self._launcher else None
            if info and info.state == "exited":
                return True  # exited = done (may have succeeded or failed)
            await asyncio.sleep(1)
        return False

    def _extract_last_assistant_message(self, session_id: str) -> str:
        """Extract the last assistant message from session history."""
        if not self._ws_bridge:
            return ""
        session = self._ws_bridge._sessions.get(session_id)
        if not session:
            return ""
        # Walk backward through message history for the last assistant message
        for msg in reversed(session.message_history):
            if msg.get("type") == "assistant_message":
                return msg.get("content", "")
        return ""

    def _extract_cost(self, session_id: str) -> float:
        """Extract total cost from session state."""
        if not self._ws_bridge:
            return 0.0
        session = self._ws_bridge._sessions.get(session_id)
        if not session:
            return 0.0
        return session.state.get("total_cost_usd", 0.0)

    async def _emit_completion_event(self, task: TaskDefinition, result: TaskResult) -> None:
        """Emit a task_completed event via the Ring0 event system."""
        if not self._ws_bridge:
            return
        try:
            from server.ring0_events import Ring0Event
            await self._ws_bridge.emit_ring0_event(Ring0Event(fields={
                "type": "task_completed",
                "task": task.name,
                "taskId": task.id,
                "status": result.status,
                "priority": result.priority,
                "pendingCount": str(self._queue.count_pending()),
            }))
        except Exception:
            logger.exception("[scheduler] Failed to emit completion event")
