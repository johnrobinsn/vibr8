"""Minimal scrcpy client — manages scrcpy-server lifecycle, decodes H.264
video frames via PyAV, and sends input events over the binary control protocol.

Protocol reference: scrcpy v2.7 wire format.
"""

from __future__ import annotations

import asyncio
import logging
import random
import socket
import struct
import time
from pathlib import Path
from typing import Any, Optional

import av

from server import adb_utils

logger = logging.getLogger(__name__)

# scrcpy-server binary bundled in server/vendor/
SCRCPY_SERVER_JAR = Path(__file__).parent / "vendor" / "scrcpy-server-v2.7"
SCRCPY_VERSION = "2.7"
REMOTE_SERVER_PATH = "/data/local/tmp/scrcpy-server.jar"

# Control message types (from ControlMessage.java)
TYPE_INJECT_KEYCODE = 0
TYPE_INJECT_TEXT = 1
TYPE_INJECT_TOUCH_EVENT = 2
TYPE_INJECT_SCROLL_EVENT = 3
TYPE_BACK_OR_SCREEN_ON = 4
TYPE_EXPAND_NOTIFICATION_PANEL = 5
TYPE_COLLAPSE_PANELS = 7
TYPE_SET_CLIPBOARD = 9
TYPE_SET_DISPLAY_POWER = 10
TYPE_ROTATE_DEVICE = 11

# Touch actions
ACTION_DOWN = 0
ACTION_UP = 1
ACTION_MOVE = 2

# Key actions
AKEY_ACTION_DOWN = 0
AKEY_ACTION_UP = 1

# AMOTION_EVENT_BUTTON_PRIMARY
BUTTON_PRIMARY = 1 << 0


class ScrcpyClient:
    """Manages a scrcpy session: server lifecycle, video decoding, and input.

    Usage:
        client = ScrcpyClient(device_id="XXXXXXXX")
        info = await client.start()  # push server, connect, start decoding
        frame = await client.get_frame()  # latest av.VideoFrame
        await client.inject_touch(ACTION_DOWN, 540, 1200, 1080, 2340)
        await client.stop()
    """

    def __init__(
        self,
        device_id: str,
        max_size: int = 1080,
        max_fps: int = 30,
        video_codec: str = "h264",
    ) -> None:
        self._device_id = device_id
        self._max_size = max_size
        self._max_fps = max_fps
        self._video_codec = video_codec

        # Connection state
        self._scid: int = 0
        self._local_port: int = 0
        self._video_sock: socket.socket | None = None
        self._control_sock: socket.socket | None = None
        self._server_proc: asyncio.subprocess.Process | None = None

        # Video decode state
        self._codec_ctx: av.CodecContext | None = None
        self._latest_frame: av.VideoFrame | None = None
        self._screen_width: int = 0
        self._screen_height: int = 0
        self._device_name: str = ""
        self._decode_task: asyncio.Task[None] | None = None
        self._running: bool = False

        # Async wrappers for socket I/O
        self._video_reader: asyncio.StreamReader | None = None
        self._control_writer: asyncio.StreamWriter | None = None

        # Frame subscribers — synchronous callbacks fired on every decoded frame
        self._frame_callbacks: list[Any] = []

    @property
    def screen_width(self) -> int:
        return self._screen_width

    @property
    def screen_height(self) -> int:
        return self._screen_height

    @property
    def device_name(self) -> str:
        return self._device_name

    @property
    def running(self) -> bool:
        return self._running

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> dict[str, Any]:
        """Push server, start it, connect sockets, begin decode loop.

        Returns dict with device info: name, width, height.
        """
        if self._running:
            raise RuntimeError("ScrcpyClient already running")

        # 1. Push server JAR to device
        await self._push_server()

        # 2. Generate unique SCID and find a free port
        self._scid = random.randint(0, 0x7FFFFFFF)
        self._local_port = self._find_free_port()

        # 3. Set up ADB forward
        abstract_name = f"localabstract:scrcpy_{self._scid:08x}"
        await adb_utils.forward_port(self._local_port, abstract_name, device_id=self._device_id)

        # 4. Start server process
        await self._start_server()

        # 5. Connect video socket
        await self._connect_video_socket()

        # 6. Connect control socket
        await self._connect_control_socket()

        # 7. Start decode loop
        self._running = True
        self._decode_task = asyncio.create_task(self._decode_loop())

        info = {
            "deviceName": self._device_name,
            "width": self._screen_width,
            "height": self._screen_height,
        }
        logger.info("[scrcpy] Started: device=%s, %dx%d, port=%d",
                     self._device_name, self._screen_width, self._screen_height, self._local_port)
        return info

    async def stop(self) -> None:
        """Disconnect sockets, kill server, clean up."""
        self._running = False

        if self._decode_task and not self._decode_task.done():
            self._decode_task.cancel()
            try:
                await self._decode_task
            except asyncio.CancelledError:
                pass

        if self._control_writer:
            try:
                self._control_writer.close()
                await self._control_writer.wait_closed()
            except Exception:
                pass
            self._control_writer = None

        if self._video_sock:
            try:
                self._video_sock.close()
            except Exception:
                pass
            self._video_sock = None
        self._video_reader = None

        if self._server_proc and self._server_proc.returncode is None:
            try:
                self._server_proc.kill()
                await self._server_proc.wait()
            except Exception:
                pass
            self._server_proc = None

        # Clean up ADB forward
        if self._local_port:
            try:
                await adb_utils.remove_forward(self._local_port, device_id=self._device_id)
            except Exception:
                pass

        self._latest_frame = None
        self._codec_ctx = None
        logger.info("[scrcpy] Stopped for device %s", self._device_id)

    # ── Frame access ──────────────────────────────────────────────────────

    async def get_frame(self) -> av.VideoFrame | None:
        """Return the most recent decoded video frame (non-blocking)."""
        return self._latest_frame

    def on_frame(self, callback: Any) -> Any:
        """Subscribe to raw decoded frames. Returns unsubscribe function."""
        self._frame_callbacks.append(callback)
        return lambda: self._frame_callbacks.remove(callback) if callback in self._frame_callbacks else None

    # ── Input injection ───────────────────────────────────────────────────

    async def inject_touch(
        self,
        action: int,
        x: int,
        y: int,
        width: int,
        height: int,
        touch_id: int = -1,
        pressure: int = 0xFFFF,
    ) -> None:
        """Send a touch event via the control socket.

        action: ACTION_DOWN (0), ACTION_UP (1), ACTION_MOVE (2)
        x, y: absolute pixel coordinates
        width, height: screen dimensions (for normalization)
        """
        if action == ACTION_DOWN:
            pressure = 0xFFFF
        elif action == ACTION_UP:
            pressure = 0

        # struct ">BqiiHHHii"
        data = struct.pack(">B", TYPE_INJECT_TOUCH_EVENT)
        data += struct.pack(">BqiiHHHii",
                            action,
                            touch_id,
                            x, y,
                            width, height,
                            pressure,
                            BUTTON_PRIMARY if action != ACTION_UP else 0,
                            BUTTON_PRIMARY if action == ACTION_DOWN else 0)
        await self._send_control(data)

    async def inject_key(self, action: int, keycode: int, repeat: int = 0, meta_state: int = 0) -> None:
        """Send a keycode event via the control socket."""
        data = struct.pack(">B", TYPE_INJECT_KEYCODE)
        data += struct.pack(">Biii", action, keycode, repeat, meta_state)
        await self._send_control(data)

    async def inject_text(self, text: str) -> None:
        """Send text input via the control socket (handles Unicode)."""
        text_bytes = text.encode("utf-8")
        data = struct.pack(">B", TYPE_INJECT_TEXT)
        data += struct.pack(">I", len(text_bytes))
        data += text_bytes
        await self._send_control(data)

    async def inject_scroll(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        h_scroll: int,
        v_scroll: int,
    ) -> None:
        """Send a scroll event via the control socket."""
        data = struct.pack(">B", TYPE_INJECT_SCROLL_EVENT)
        data += struct.pack(">iiHHii", x, y, width, height, h_scroll, v_scroll)
        # buttons field (added in v2.x)
        data += struct.pack(">i", 0)
        await self._send_control(data)

    async def set_display_power(self, on: bool) -> None:
        """Turn the device display on or off."""
        data = struct.pack(">BB", TYPE_SET_DISPLAY_POWER, 1 if on else 0)
        await self._send_control(data)

    async def back_or_screen_on(self, action: int = AKEY_ACTION_DOWN) -> None:
        """Send back-or-turn-screen-on event."""
        data = struct.pack(">BB", TYPE_BACK_OR_SCREEN_ON, action)
        await self._send_control(data)

    # ── Private: server lifecycle ─────────────────────────────────────────

    async def _push_server(self) -> None:
        """Push scrcpy-server JAR to the device."""
        if not SCRCPY_SERVER_JAR.exists():
            raise FileNotFoundError(f"scrcpy-server not found at {SCRCPY_SERVER_JAR}")
        await adb_utils.push_file(
            str(SCRCPY_SERVER_JAR),
            REMOTE_SERVER_PATH,
            device_id=self._device_id,
        )

    async def _start_server(self) -> None:
        """Start scrcpy-server on the device via `adb shell`."""
        cmd = [
            "adb", "-s", self._device_id, "shell",
            f"CLASSPATH={REMOTE_SERVER_PATH}",
            "app_process", "/", "com.genymobile.scrcpy.Server",
            SCRCPY_VERSION,
            f"scid={self._scid:08x}",
            "log_level=info",
            f"max_size={self._max_size}",
            f"max_fps={self._max_fps}",
            f"video_codec={self._video_codec}",
            "audio=false",
            "control=true",
            "tunnel_forward=true",
            "send_device_meta=true",
            "send_frame_meta=true",
            "send_dummy_byte=true",
            "send_codec_meta=true",
        ]
        self._server_proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Give the server time to start and bind the socket
        await asyncio.sleep(0.5)
        if self._server_proc.returncode is not None:
            stderr = (await self._server_proc.stderr.read()).decode(errors="replace") if self._server_proc.stderr else ""
            raise RuntimeError(f"scrcpy-server exited immediately: {stderr[:500]}")

    # ── Private: socket connection ────────────────────────────────────────

    def _find_free_port(self) -> int:
        """Find a free TCP port on localhost."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    async def _connect_video_socket(self) -> None:
        """Connect the video socket and read initial metadata."""
        # Retry connection for up to 3 seconds
        sock = None
        for _ in range(30):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setblocking(False)
                loop = asyncio.get_running_loop()
                await asyncio.wait_for(
                    loop.sock_connect(sock, ("127.0.0.1", self._local_port)),
                    timeout=1.0,
                )
                break
            except (ConnectionRefusedError, asyncio.TimeoutError, OSError):
                if sock:
                    sock.close()
                    sock = None
                await asyncio.sleep(0.1)

        if sock is None:
            raise ConnectionError(f"Failed to connect video socket on port {self._local_port}")

        self._video_sock = sock

        loop = asyncio.get_running_loop()

        # Read dummy byte
        dummy = await self._recv_exact(sock, 1, loop)
        if dummy != b"\x00":
            logger.warning("[scrcpy] Unexpected dummy byte: %r", dummy)

        # Read device name (64 bytes, null-padded)
        name_bytes = await self._recv_exact(sock, 64, loop)
        self._device_name = name_bytes.split(b"\x00", 1)[0].decode(errors="replace")

        # Read codec metadata (12 bytes): codec_id(u32), width(u32), height(u32)
        codec_meta = await self._recv_exact(sock, 12, loop)
        codec_id, w, h = struct.unpack(">III", codec_meta)
        self._screen_width = w
        self._screen_height = h

        # Initialize H.264 decoder
        codec_name = {0x68323634: "h264", 0x68323635: "hevc", 0x00617631: "av1"}.get(codec_id, "h264")
        self._codec_ctx = av.CodecContext.create(codec_name, "r")

        # Create asyncio stream reader for the video socket
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.create_connection(lambda: protocol, sock=sock)
        self._video_reader = reader

    async def _connect_control_socket(self) -> None:
        """Connect the control socket."""
        loop = asyncio.get_running_loop()

        for _ in range(30):
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", self._local_port),
                    timeout=1.0,
                )
                # Read and discard dummy byte
                dummy = await asyncio.wait_for(reader.read(1), timeout=2.0)
                self._control_writer = writer
                return
            except (ConnectionRefusedError, asyncio.TimeoutError, OSError):
                await asyncio.sleep(0.1)

        raise ConnectionError(f"Failed to connect control socket on port {self._local_port}")

    async def _recv_exact(self, sock: socket.socket, n: int, loop: asyncio.AbstractEventLoop) -> bytes:
        """Read exactly n bytes from a non-blocking socket."""
        buf = b""
        while len(buf) < n:
            chunk = await loop.sock_recv(sock, n - len(buf))
            if not chunk:
                raise ConnectionError("Socket closed while reading")
            buf += chunk
        return buf

    async def _send_control(self, data: bytes) -> None:
        """Write data to the control socket."""
        if not self._control_writer:
            return
        try:
            self._control_writer.write(data)
            await self._control_writer.drain()
        except (ConnectionError, OSError) as e:
            logger.warning("[scrcpy] Control socket write failed: %s", e)

    # ── Private: video decode loop ────────────────────────────────────────

    async def _decode_loop(self) -> None:
        """Continuously read H.264 packets and decode to av.VideoFrame.

        Each packet has a 12-byte header:
          - Bits 63: config packet flag (SPS/PPS)
          - Bits 62: key frame flag
          - Bits 61-0: PTS (62 bits)
          - Bytes 8-11: packet size (u32)
        """
        reader = self._video_reader
        codec = self._codec_ctx
        if not reader or not codec:
            return

        while self._running:
            try:
                # Read 12-byte frame header
                header = await reader.readexactly(12)
                pts_flags = struct.unpack(">Q", header[:8])[0]
                pkt_size = struct.unpack(">I", header[8:12])[0]

                is_config = bool((pts_flags >> 63) & 1)
                # is_keyframe = bool((pts_flags >> 62) & 1)

                # Read packet data
                data = await reader.readexactly(pkt_size)

                # Config packets contain SPS/PPS — parse for resolution changes
                if is_config:
                    self._handle_config_packet(data)

                # Decode
                packets = codec.parse(data)
                for packet in packets:
                    try:
                        frames = codec.decode(packet)
                        for frame in frames:
                            self._latest_frame = frame
                            for cb in self._frame_callbacks:
                                try:
                                    cb(frame)
                                except Exception:
                                    pass
                    except av.error.InvalidDataError:
                        # Corrupt frame — skip
                        pass

            except asyncio.IncompleteReadError:
                logger.info("[scrcpy] Video socket closed")
                break
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[scrcpy] Decode loop error")
                break

        self._running = False

    def _handle_config_packet(self, data: bytes) -> None:
        """Parse SPS from a config packet to detect resolution changes.

        For H.264, SPS NAL units (type 7) contain the video dimensions.
        We look for NAL start codes and check the NAL type.
        """
        # Simple approach: re-create the codec context to pick up new SPS
        # This handles rotation transparently
        try:
            codec_name = "h264"
            if self._codec_ctx:
                codec_name = self._codec_ctx.name
            new_ctx = av.CodecContext.create(codec_name, "r")
            # Feed the config packet to the new context
            packets = new_ctx.parse(data)
            for pkt in packets:
                try:
                    frames = new_ctx.decode(pkt)
                    for frame in frames:
                        # Resolution changed
                        if frame.width != self._screen_width or frame.height != self._screen_height:
                            logger.info("[scrcpy] Resolution changed: %dx%d → %dx%d",
                                        self._screen_width, self._screen_height,
                                        frame.width, frame.height)
                            self._screen_width = frame.width
                            self._screen_height = frame.height
                        self._latest_frame = frame
                        for cb in self._frame_callbacks:
                            try:
                                cb(frame)
                            except Exception:
                                pass
                except av.error.InvalidDataError:
                    pass
            self._codec_ctx = new_ctx
        except Exception:
            logger.warning("[scrcpy] Failed to handle config packet")
