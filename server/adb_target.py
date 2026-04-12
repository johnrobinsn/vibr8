"""ADB Target — scrcpy-based target for Android device control.

Drop-in replacement for DesktopTarget. Implements the same get_frame()/inject()
interface used by UITarsAgent. Uses ScrcpyClient for both screen capture and
input injection via the scrcpy binary control protocol.

The inject() method receives the same JSON event format as DesktopTarget
(mousemove, mousedown, mouseup, text, keydown, keyup, wheel) and coalesces
mouse events into Android touch gestures.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import av

from server.scrcpy_client import (
    ScrcpyClient,
    ACTION_DOWN,
    ACTION_MOVE,
    ACTION_UP,
    AKEY_ACTION_DOWN,
    AKEY_ACTION_UP,
)

logger = logging.getLogger(__name__)

# Map web key names to Android keycodes
_KEYCODE_MAP: dict[str, int] = {
    "Enter": 66,
    "Backspace": 67,        # KEYCODE_DEL (Android DEL = backspace)
    "Delete": 112,          # KEYCODE_FORWARD_DEL
    "Tab": 61,
    "Escape": 111,
    "Home": 3,              # KEYCODE_HOME
    "Back": 4,              # KEYCODE_BACK
    "ArrowUp": 19,          # KEYCODE_DPAD_UP
    "ArrowDown": 20,
    "ArrowLeft": 21,
    "ArrowRight": 22,
    "Space": 62,
    "AppSwitch": 187,       # KEYCODE_APP_SWITCH
    "Power": 26,
    "VolumeUp": 24,
    "VolumeDown": 25,
    "Menu": 82,
    "Search": 84,
}


class AdbTarget:
    """scrcpy-based target for Android device control.

    Implements the ComputerUseAgent target interface:
    - get_frame() → av.VideoFrame
    - inject(event: dict) → None

    Mouse events are coalesced into touch gestures:
    - mousemove + mousedown + mouseup at same pos → tap
    - mousedown + mousemove + mouseup at diff pos → swipe
    - mousemove without mousedown → no-op (no cursor on Android)
    """

    def __init__(self, scrcpy_client: ScrcpyClient) -> None:
        self._scrcpy = scrcpy_client

        # Event coalescing state
        self._mouse_down_pos: Optional[tuple[int, int]] = None  # (x, y) at mousedown
        self._mouse_current_pos: Optional[tuple[int, int]] = None  # Latest mousemove pos
        self._is_dragging: bool = False

    @property
    def screen_width(self) -> int:
        return self._scrcpy.screen_width

    @property
    def screen_height(self) -> int:
        return self._scrcpy.screen_height

    async def start(self) -> None:
        """Start the scrcpy client (if not already running)."""
        if not self._scrcpy.running:
            await self._scrcpy.start()

    async def stop(self) -> None:
        """Stop the scrcpy client."""
        await self._scrcpy.stop()

    async def get_frame(self) -> av.VideoFrame | None:
        """Return the latest decoded frame from scrcpy."""
        return await self._scrcpy.get_frame()

    async def inject(self, event: dict[str, Any]) -> None:
        """Translate JSON input events to scrcpy control commands.

        Events arrive in the same format as desktop WebRTC data channel:
        - mousemove: {"type": "mousemove", "x": 0.5, "y": 0.5}
        - mousedown: {"type": "mousedown", "button": 0}
        - mouseup: {"type": "mouseup", "button": 0}
        - text: {"type": "text", "text": "hello"}
        - keydown: {"type": "keydown", "key": "Enter"}
        - keyup: {"type": "keyup", "key": "Enter"}
        - wheel: {"type": "wheel", "x": 0.5, "y": 0.5, "dy": 300}

        Coordinates are fractional (0.0-1.0), converted to absolute pixels.
        """
        event_type = event.get("type", "")
        w = self._scrcpy.screen_width
        h = self._scrcpy.screen_height
        if not w or not h:
            return

        if event_type == "mousemove":
            px = int(event.get("x", 0) * w)
            py = int(event.get("y", 0) * h)
            self._mouse_current_pos = (px, py)

            # If mouse is down, send move event for swipe tracking
            if self._mouse_down_pos is not None:
                self._is_dragging = True
                await self._scrcpy.inject_touch(ACTION_MOVE, px, py, w, h)

        elif event_type == "mousedown":
            px, py = self._mouse_current_pos or (0, 0)
            self._mouse_down_pos = (px, py)
            self._is_dragging = False
            await self._scrcpy.inject_touch(ACTION_DOWN, px, py, w, h)

        elif event_type == "mouseup":
            px, py = self._mouse_current_pos or (0, 0)
            await self._scrcpy.inject_touch(ACTION_UP, px, py, w, h)
            self._mouse_down_pos = None
            self._is_dragging = False

        elif event_type == "text":
            text = event.get("text", "")
            if text:
                await self._scrcpy.inject_text(text)

        elif event_type == "keydown":
            key = event.get("key", "")
            keycode = _KEYCODE_MAP.get(key)
            if keycode is not None:
                await self._scrcpy.inject_key(AKEY_ACTION_DOWN, keycode)

        elif event_type == "keyup":
            key = event.get("key", "")
            keycode = _KEYCODE_MAP.get(key)
            if keycode is not None:
                await self._scrcpy.inject_key(AKEY_ACTION_UP, keycode)

        elif event_type == "wheel":
            px = int(event.get("x", 0) * w)
            py = int(event.get("y", 0) * h)
            dy = event.get("dy", 0)
            # Convert pixel delta to scroll units (negative dy = scroll down in web, but
            # scrcpy uses positive v_scroll = scroll up, so negate)
            v_scroll = -1 if dy > 0 else 1 if dy < 0 else 0
            if v_scroll:
                await self._scrcpy.inject_scroll(px, py, w, h, 0, v_scroll)
