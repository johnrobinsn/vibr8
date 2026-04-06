"""UI-TARS action parsing and execution.

Parses model output and translates actions into input injection events
for desktop control via the WebRTC data channel.

Ported from /mntc/code/v1/src/v1/model/parser.py.

UI-TARS coordinate space: 1000×1000 normalized.  Point format: (x, y).
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

# UI-TARS normalizes coords to a 1000×1000 grid
_COORD_SCALE = 1000.0

# ── Parsed action ────────────────────────────────────────────────────────────


@dataclass
class ParsedAction:
    thought: str = ""
    action_type: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    raw: str = ""


# ── Parse (matching v1 parser format) ────────────────────────────────────────


def parse_action(text: str) -> ParsedAction:
    """Parse UI-TARS model output into a structured action.

    Handles both bare actions (click(start_box='(x,y)')) and
    Thought:/Action: prefixed output.
    """
    result = ParsedAction(raw=text)

    # Extract optional Thought: prefix
    m = re.search(r"Thought:\s*(.+?)(?=\nAction:|\Z)", text, re.DOTALL)
    if m:
        result.thought = m.group(1).strip()

    # Use text after "Action:" if present, otherwise full text
    m = re.search(r"Action:\s*(.+)", text, re.DOTALL)
    action_text = m.group(1).strip() if m else text.strip()

    # Try each parser — matches v1/model/parser.py patterns
    for parser in [
        _parse_finished,
        _parse_call_user,
        _parse_wait,
        _parse_double_click,
        _parse_click,
        _parse_right_click,
        _parse_drag,
        _parse_type,
        _parse_hotkey,
        _parse_scroll,
        _parse_bare_coords,
    ]:
        parsed = parser(action_text, result)
        if parsed:
            return parsed

    return result


def _parse_click(text: str, r: ParsedAction) -> ParsedAction | None:
    m = re.search(r"click\(start_box='?\((\d+),\s*(\d+)\)'?\)", text)
    if m:
        r.action_type = "click"
        r.params = {"x": int(m.group(1)), "y": int(m.group(2))}
        return r
    return None


def _parse_double_click(text: str, r: ParsedAction) -> ParsedAction | None:
    # Check "double_click" and "left_double"
    m = re.search(r"(?:double_click|left_double)\(start_box='?\((\d+),\s*(\d+)\)'?\)", text)
    if m:
        r.action_type = "left_double"
        r.params = {"x": int(m.group(1)), "y": int(m.group(2))}
        return r
    return None


def _parse_right_click(text: str, r: ParsedAction) -> ParsedAction | None:
    m = re.search(r"right_single\(start_box='?\((\d+),\s*(\d+)\)'?\)", text)
    if m:
        r.action_type = "right_single"
        r.params = {"x": int(m.group(1)), "y": int(m.group(2))}
        return r
    return None


def _parse_drag(text: str, r: ParsedAction) -> ParsedAction | None:
    m = re.search(
        r"drag\(start_box='?\((\d+),\s*(\d+)\)'?,\s*end_box='?\((\d+),\s*(\d+)\)'?\)",
        text,
    )
    if m:
        r.action_type = "drag"
        r.params = {
            "sx": int(m.group(1)), "sy": int(m.group(2)),
            "ex": int(m.group(3)), "ey": int(m.group(4)),
        }
        return r
    return None


def _parse_type(text: str, r: ParsedAction) -> ParsedAction | None:
    m = re.search(r"type\((?:content|text)=['\"](.+?)['\"]\)", text)
    if m:
        r.action_type = "type"
        r.params = {"content": m.group(1)}
        return r
    return None


def _parse_hotkey(text: str, r: ParsedAction) -> ParsedAction | None:
    # hotkey(key='...') or press(key='...')
    m = re.search(r"(?:hotkey|press)\(key=['\"](.+?)['\"]\)", text)
    if m:
        r.action_type = "hotkey"
        r.params = {"key": m.group(1)}
        return r
    return None


def _parse_scroll(text: str, r: ParsedAction) -> ParsedAction | None:
    # start_box first: scroll(start_box='(x,y)', direction='dir')
    m = re.search(
        r"scroll\(start_box='?\((\d+),\s*(\d+)\)'?,\s*direction='(\w+)'\)", text,
    )
    if m:
        r.action_type = "scroll"
        r.params = {"x": int(m.group(1)), "y": int(m.group(2)), "direction": m.group(3)}
        return r
    # direction first: scroll(direction='dir', start_box='(x,y)')
    m = re.search(
        r"scroll\(direction='(\w+)',\s*start_box='?\((\d+),\s*(\d+)\)'?\)", text,
    )
    if m:
        r.action_type = "scroll"
        r.params = {"x": int(m.group(2)), "y": int(m.group(3)), "direction": m.group(1)}
        return r
    # direction only: scroll(direction='dir')
    m = re.search(r"scroll\(direction='(\w+)'\)", text)
    if m:
        r.action_type = "scroll"
        r.params = {"x": 500, "y": 500, "direction": m.group(1)}
        return r
    return None


def _parse_wait(text: str, r: ParsedAction) -> ParsedAction | None:
    if re.search(r"wait\(\)", text):
        r.action_type = "wait"
        return r
    return None


def _parse_finished(text: str, r: ParsedAction) -> ParsedAction | None:
    if re.search(r"finished\(\)", text):
        r.action_type = "finished"
        return r
    return None


def _parse_call_user(text: str, r: ParsedAction) -> ParsedAction | None:
    if re.search(r"call_user\(\)", text):
        r.action_type = "call_user"
        return r
    return None


def _parse_bare_coords(text: str, r: ParsedAction) -> ParsedAction | None:
    """Parse bare coordinate output like '(357,919)'."""
    m = re.match(r"^\((\d+),\s*(\d+)\)$", text.strip())
    if m:
        r.action_type = "click"
        r.params = {"x": int(m.group(1)), "y": int(m.group(2))}
        return r
    return None


# ── Execute ──────────────────────────────────────────────────────────────────


def _to_frac(x: int, y: int) -> tuple[float, float]:
    """Convert 1000×1000 normalized coords to 0.0-1.0 fractions."""
    return x / _COORD_SCALE, y / _COORD_SCALE


async def execute_action(
    action: ParsedAction,
    injector: Injector,
) -> str | None:
    """Execute a parsed action via the injector (data channel).

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
        fx, fy = _to_frac(action.params["x"], action.params["y"])
        await injector.inject({"type": "mousemove", "x": fx, "y": fy})
        await injector.inject({"type": "mousedown", "button": 0, "x": fx, "y": fy})
        await injector.inject({"type": "mouseup", "button": 0, "x": fx, "y": fy})
        return None

    if atype == "left_double":
        fx, fy = _to_frac(action.params["x"], action.params["y"])
        for _ in range(2):
            await injector.inject({"type": "mousemove", "x": fx, "y": fy})
            await injector.inject({"type": "mousedown", "button": 0, "x": fx, "y": fy})
            await injector.inject({"type": "mouseup", "button": 0, "x": fx, "y": fy})
            await asyncio.sleep(0.05)
        return None

    if atype == "right_single":
        fx, fy = _to_frac(action.params["x"], action.params["y"])
        await injector.inject({"type": "mousemove", "x": fx, "y": fy})
        await injector.inject({"type": "mousedown", "button": 2, "x": fx, "y": fy})
        await injector.inject({"type": "mouseup", "button": 2, "x": fx, "y": fy})
        return None

    if atype == "drag":
        sfx, sfy = _to_frac(action.params["sx"], action.params["sy"])
        efx, efy = _to_frac(action.params["ex"], action.params["ey"])
        await injector.inject({"type": "mousemove", "x": sfx, "y": sfy})
        await injector.inject({"type": "mousedown", "button": 0, "x": sfx, "y": sfy})
        await injector.inject({"type": "mousemove", "x": efx, "y": efy})
        await injector.inject({"type": "mouseup", "button": 0, "x": efx, "y": efy})
        return None

    if atype == "type":
        content = action.params.get("content", "")
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
        fx, fy = _to_frac(action.params["x"], action.params["y"])
        direction = action.params.get("direction", "down")
        await injector.inject({"type": "mousemove", "x": fx, "y": fy})
        dy = 300 if direction in ("down", "right") else -300
        await injector.inject({"type": "wheel", "dy": dy})
        return None

    if not atype:
        logger.warning("[ui-tars] No action parsed from: %s", action.raw[:200])

    return None
