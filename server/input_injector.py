"""Input injector — translates browser input events into native OS actions.

Receives normalized JSON events from the WebRTC data channel and injects
mouse/keyboard input.

Linux: xdotool subprocess calls against an X11 display.
macOS: pyautogui (Quartz event injection). Requires Accessibility permissions.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="input-injector")

# ── Linux (xdotool) mappings ──────────────────────────────────────────────

# Browser button index → xdotool button number
_XDOTOOL_BUTTON_MAP = {0: 1, 1: 2, 2: 3}

# Browser key name → xdotool key name
_XDOTOOL_KEY_MAP = {
    "Enter": "Return",
    "Space": "space",
    " ": "space",
    "Backspace": "BackSpace",
    "ArrowLeft": "Left",
    "ArrowRight": "Right",
    "ArrowUp": "Up",
    "ArrowDown": "Down",
    "Escape": "Escape",
    "Tab": "Tab",
    "Delete": "Delete",
    "Home": "Home",
    "End": "End",
    "PageUp": "Prior",
    "PageDown": "Next",
    "Shift": "shift",
    "Control": "ctrl",
    "Alt": "alt",
    "Meta": "super",
    "CapsLock": "Caps_Lock",
}
for _i in range(1, 13):
    _XDOTOOL_KEY_MAP[f"F{_i}"] = f"F{_i}"

# ── macOS (pyautogui) mappings ────────────────────────────────────────────

_PYAUTOGUI_BUTTON_MAP = {0: "left", 1: "middle", 2: "right"}

_PYAUTOGUI_KEY_MAP = {
    "Enter": "return",
    "Space": "space",
    " ": "space",
    "Backspace": "backspace",
    "ArrowLeft": "left",
    "ArrowRight": "right",
    "ArrowUp": "up",
    "ArrowDown": "down",
    "Escape": "escape",
    "Tab": "tab",
    "Delete": "delete",
    "Home": "home",
    "End": "end",
    "PageUp": "pageup",
    "PageDown": "pagedown",
    "Shift": "shift",
    "Control": "ctrl",
    "Alt": "option",
    "Meta": "command",
    "CapsLock": "capslock",
}
for _i in range(1, 13):
    _PYAUTOGUI_KEY_MAP[f"F{_i}"] = f"f{_i}"


class InputInjector:
    """Injects mouse/keyboard events into the desktop.

    On Linux, uses xdotool with the given DISPLAY.
    On macOS, uses pyautogui (Quartz).
    """

    def __init__(self, display: str, screen_width: int, screen_height: int,
                 screen_size_fn: "Callable[[], tuple[int, int]] | None" = None) -> None:
        self._display = display
        self._screen_width = screen_width
        self._screen_height = screen_height
        self._screen_size_fn = screen_size_fn
        self._platform = sys.platform
        self._warned_permission = False

        if self._platform == "darwin":
            try:
                import pyautogui
                pyautogui.FAILSAFE = False
                self._pyautogui = pyautogui
            except ImportError:
                logger.warning("[input] pyautogui not installed — macOS input injection disabled")
                self._pyautogui = None
        else:
            self._env = {**os.environ, "DISPLAY": display}
            self._pyautogui = None

    async def inject(self, event: dict) -> None:
        """Dispatch a browser input event to the appropriate handler."""
        etype = event.get("type")
        if not etype:
            return
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(_executor, self._inject_sync, event)

    def _inject_sync(self, event: dict) -> None:
        etype = event["type"]
        try:
            if etype == "mousemove":
                self._mouse_move(event)
            elif etype == "mousedown":
                self._mouse_button(event, press=True)
            elif etype == "mouseup":
                self._mouse_button(event, press=False)
            elif etype == "wheel":
                self._wheel(event)
            elif etype == "keydown":
                self._key(event, press=True)
            elif etype == "keyup":
                self._key(event, press=False)
            elif etype == "text":
                self._type_text(event)
        except PermissionError:
            if not self._warned_permission:
                self._warned_permission = True
                logger.warning(
                    "[input] Permission denied — on macOS, grant Accessibility "
                    "permissions to this process in System Settings → Privacy & Security → Accessibility"
                )
        except Exception as exc:
            if not self._warned_permission and self._platform == "darwin" and "not trusted" in str(exc).lower():
                self._warned_permission = True
                logger.warning(
                    "[input] macOS Accessibility permission required — grant access in "
                    "System Settings → Privacy & Security → Accessibility"
                )
            else:
                logger.debug("[input] inject error for %s: %s", etype, exc)

    # ── Coordinate helpers ────────────────────────────────────────────────

    def _to_pixels(self, event: dict) -> tuple[int, int]:
        if self._screen_size_fn is not None:
            w, h = self._screen_size_fn()
        else:
            w, h = self._screen_width, self._screen_height
        x = max(0.0, min(1.0, float(event.get("x", 0))))
        y = max(0.0, min(1.0, float(event.get("y", 0))))
        return int(x * w), int(y * h)

    # ── Linux (xdotool) ──────────────────────────────────────────────────

    def _xdotool(self, *args: str) -> None:
        subprocess.run(
            ["xdotool", *args],
            env=self._env,
            timeout=1,
            capture_output=True,
        )

    # ── Mouse ─────────────────────────────────────────────────────────────

    def _mouse_move(self, event: dict) -> None:
        px, py = self._to_pixels(event)
        if self._platform == "darwin":
            if not self._pyautogui:
                return
            self._pyautogui.moveTo(px, py, duration=0)
        else:
            self._xdotool("mousemove", str(px), str(py))

    def _mouse_button(self, event: dict, press: bool) -> None:
        px, py = self._to_pixels(event)
        if self._platform == "darwin":
            if not self._pyautogui:
                return
            self._pyautogui.moveTo(px, py, duration=0)
            btn = _PYAUTOGUI_BUTTON_MAP.get(event.get("button", 0), "left")
            if press:
                self._pyautogui.mouseDown(button=btn)
            else:
                self._pyautogui.mouseUp(button=btn)
        else:
            btn = _XDOTOOL_BUTTON_MAP.get(event.get("button", 0), 1)
            self._xdotool("mousemove", str(px), str(py))
            self._xdotool("mousedown" if press else "mouseup", str(btn))

    def _wheel(self, event: dict) -> None:
        dy = event.get("dy", 0)
        if dy == 0:
            return
        if self._platform == "darwin":
            if not self._pyautogui:
                return
            # pyautogui.scroll: positive = up, negative = down
            # Browser dy: positive = scroll down, negative = scroll up
            clicks = max(1, int(abs(dy)) // 100)
            self._pyautogui.scroll(-clicks if dy > 0 else clicks)
        else:
            button = "5" if dy > 0 else "4"
            count = max(1, int(abs(dy)) // 100)
            for _ in range(count):
                self._xdotool("click", button)

    # ── Text input (IME, autocorrect, paste) ────────────────────────────

    def _type_text(self, event: dict) -> None:
        text = event.get("text", "")
        if not text:
            return
        if self._platform == "darwin":
            if not self._pyautogui:
                return
            self._pyautogui.typewrite(text, interval=0)
        else:
            self._xdotool("type", "--clearmodifiers", "--", text)

    # ── Keyboard ──────────────────────────────────────────────────────────

    def _key(self, event: dict, press: bool) -> None:
        key = event.get("key", "")
        if not key:
            return
        if self._platform == "darwin":
            if not self._pyautogui:
                return
            pkey = _PYAUTOGUI_KEY_MAP.get(key)
            if pkey is None:
                if len(key) == 1:
                    pkey = key
                else:
                    return
            if press:
                self._pyautogui.keyDown(pkey)
            else:
                self._pyautogui.keyUp(pkey)
        else:
            xkey = _XDOTOOL_KEY_MAP.get(key)
            if xkey is None:
                if len(key) == 1:
                    xkey = key
                else:
                    return
            self._xdotool("keydown" if press else "keyup", xkey)
