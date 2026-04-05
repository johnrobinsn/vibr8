"""UI-TARS action parsing and execution.

Parses model output (Thought/Action format) and translates actions into
input injection events for desktop control.

UI-TARS coordinate space: 1000×1000 normalized.  Bounding boxes are
[x1, y1, x2, y2] within that space.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol


class Injector(Protocol):
    """Anything with an async inject(event) method — InputInjector or DesktopTarget."""
    async def inject(self, event: dict) -> None: ...

logger = logging.getLogger(__name__)

# ── Parsed action ────────────────────────────────────────────────────────────


@dataclass
class ParsedAction:
    thought: str = ""
    action_type: str = ""  # click, left_double, right_single, drag, type, hotkey, scroll, wait, finished, call_user
    params: dict[str, Any] = field(default_factory=dict)
    raw: str = ""


# ── Regex patterns ───────────────────────────────────────────────────────────

_THOUGHT_RE = re.compile(r"Thought:\s*(.+?)(?=\nAction:|\Z)", re.DOTALL)
_ACTION_RE = re.compile(r"Action:\s*(.+)", re.DOTALL)

# Action parsers — match function-call style
_BOX_RE = re.compile(r"\[(\d+),\s*(\d+),\s*(\d+),\s*(\d+)\]")
_CLICK_RE = re.compile(r"click\(start_box='(\[[\d,\s]+\])'\)")
_DOUBLE_RE = re.compile(r"left_double\(start_box='(\[[\d,\s]+\])'\)")
_RIGHT_RE = re.compile(r"right_single\(start_box='(\[[\d,\s]+\])'\)")
_DRAG_RE = re.compile(r"drag\(start_box='(\[[\d,\s]+\])',\s*end_box='(\[[\d,\s]+\])'\)")
_TYPE_RE = re.compile(r"type\(content='(.+?)'\)", re.DOTALL)
_HOTKEY_RE = re.compile(r"hotkey\(key='(.+?)'\)")
_SCROLL_RE = re.compile(r"scroll\(start_box='(\[[\d,\s]+\])',\s*direction='(\w+)'\)")
_WAIT_RE = re.compile(r"wait\(\)")
_FINISHED_RE = re.compile(r"finished\(\)")
_CALL_USER_RE = re.compile(r"call_user\(\)")


def _parse_box(box_str: str) -> tuple[int, int, int, int] | None:
    m = _BOX_RE.search(box_str)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))


def _box_center(x1: int, y1: int, x2: int, y2: int) -> tuple[int, int]:
    return (x1 + x2) // 2, (y1 + y2) // 2


# ── Parse ────────────────────────────────────────────────────────────────────


def parse_action(text: str) -> ParsedAction:
    """Parse UI-TARS model output into a structured action."""
    result = ParsedAction(raw=text)

    # Extract thought
    m = _THOUGHT_RE.search(text)
    if m:
        result.thought = m.group(1).strip()

    # Extract action line
    m = _ACTION_RE.search(text)
    if not m:
        return result
    action_str = m.group(1).strip()

    # Match each action type
    if _FINISHED_RE.search(action_str):
        result.action_type = "finished"
    elif _CALL_USER_RE.search(action_str):
        result.action_type = "call_user"
    elif _WAIT_RE.search(action_str):
        result.action_type = "wait"
    elif (dm := _DRAG_RE.search(action_str)):
        result.action_type = "drag"
        result.params["start_box"] = dm.group(1)
        result.params["end_box"] = dm.group(2)
    elif (cm := _CLICK_RE.search(action_str)):
        result.action_type = "click"
        result.params["start_box"] = cm.group(1)
    elif (dbm := _DOUBLE_RE.search(action_str)):
        result.action_type = "left_double"
        result.params["start_box"] = dbm.group(1)
    elif (rm := _RIGHT_RE.search(action_str)):
        result.action_type = "right_single"
        result.params["start_box"] = rm.group(1)
    elif (tm := _TYPE_RE.search(action_str)):
        result.action_type = "type"
        result.params["content"] = tm.group(1)
    elif (hm := _HOTKEY_RE.search(action_str)):
        result.action_type = "hotkey"
        result.params["key"] = hm.group(1)
    elif (sm := _SCROLL_RE.search(action_str)):
        result.action_type = "scroll"
        result.params["start_box"] = sm.group(1)
        result.params["direction"] = sm.group(2)

    return result


# ── Execute ──────────────────────────────────────────────────────────────────


def _norm_to_frac(norm_x: int, norm_y: int) -> tuple[float, float]:
    """Convert 1000×1000 normalized coords to 0.0-1.0 fractions for InputInjector."""
    return norm_x / 1000.0, norm_y / 1000.0


async def execute_action(
    action: ParsedAction,
    injector: Injector,
) -> str | None:
    """Execute a parsed action via InputInjector.

    Returns a termination reason string if the loop should stop,
    or None to continue.
    """
    atype = action.action_type

    if atype == "finished":
        return "finished"

    if atype == "call_user":
        return "call_user"

    if atype == "wait":
        await asyncio.sleep(5)
        return None

    if atype == "click":
        box = _parse_box(action.params.get("start_box", ""))
        if not box:
            logger.warning("[ui-tars] click: bad box %s", action.params)
            return None
        cx, cy = _box_center(*box)
        fx, fy = _norm_to_frac(cx, cy)
        await injector.inject({"type": "mousemove", "x": fx, "y": fy})
        await injector.inject({"type": "mousedown", "button": 0, "x": fx, "y": fy})
        await injector.inject({"type": "mouseup", "button": 0, "x": fx, "y": fy})
        return None

    if atype == "left_double":
        box = _parse_box(action.params.get("start_box", ""))
        if not box:
            return None
        cx, cy = _box_center(*box)
        fx, fy = _norm_to_frac(cx, cy)
        for _ in range(2):
            await injector.inject({"type": "mousemove", "x": fx, "y": fy})
            await injector.inject({"type": "mousedown", "button": 0, "x": fx, "y": fy})
            await injector.inject({"type": "mouseup", "button": 0, "x": fx, "y": fy})
            await asyncio.sleep(0.05)
        return None

    if atype == "right_single":
        box = _parse_box(action.params.get("start_box", ""))
        if not box:
            return None
        cx, cy = _box_center(*box)
        fx, fy = _norm_to_frac(cx, cy)
        await injector.inject({"type": "mousemove", "x": fx, "y": fy})
        await injector.inject({"type": "mousedown", "button": 2, "x": fx, "y": fy})
        await injector.inject({"type": "mouseup", "button": 2, "x": fx, "y": fy})
        return None

    if atype == "drag":
        start = _parse_box(action.params.get("start_box", ""))
        end = _parse_box(action.params.get("end_box", ""))
        if not start or not end:
            return None
        sx, sy = _box_center(*start)
        ex, ey = _box_center(*end)
        sfx, sfy = _norm_to_frac(sx, sy)
        efx, efy = _norm_to_frac(ex, ey)
        await injector.inject({"type": "mousemove", "x": sfx, "y": sfy})
        await injector.inject({"type": "mousedown", "button": 0, "x": sfx, "y": sfy})
        await injector.inject({"type": "mousemove", "x": efx, "y": efy})
        await injector.inject({"type": "mouseup", "button": 0, "x": efx, "y": efy})
        return None

    if atype == "type":
        content = action.params.get("content", "")
        # Handle \n as Enter key
        if content.endswith("\\n"):
            content = content[:-2]
            await injector.inject({"type": "text", "text": content})
            await injector.inject({"type": "keydown", "key": "Enter"})
            await injector.inject({"type": "keyup", "key": "Enter"})
        else:
            await injector.inject({"type": "text", "text": content})
        return None

    if atype == "hotkey":
        keys = action.params.get("key", "").split("+")
        # Press all keys down, then release in reverse
        for key in keys:
            k = key.strip()
            if k:
                await injector.inject({"type": "keydown", "key": k})
        for key in reversed(keys):
            k = key.strip()
            if k:
                await injector.inject({"type": "keyup", "key": k})
        return None

    if atype == "scroll":
        box = _parse_box(action.params.get("start_box", ""))
        direction = action.params.get("direction", "down")
        if box:
            cx, cy = _box_center(*box)
            fx, fy = _norm_to_frac(cx, cy)
            await injector.inject({"type": "mousemove", "x": fx, "y": fy})
        dy = 300 if direction in ("down", "right") else -300
        await injector.inject({"type": "wheel", "dy": dy})
        return None

    if not atype:
        logger.warning("[ui-tars] No action parsed from: %s", action.raw[:200])

    return None
