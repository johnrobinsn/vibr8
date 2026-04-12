"""ADB command helpers — thin async wrappers around the `adb` binary.

All commands are run via asyncio.create_subprocess_exec with timeouts.
Each function accepts an optional device_id for multi-device support.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5.0


@dataclass
class AdbDevice:
    """A device returned by `adb devices`."""
    serial: str          # e.g., "XXXXXXXX" or "192.168.1.50:5555"
    status: str          # "device", "offline", "unauthorized", "no permissions"
    model: str = ""      # From device properties
    transport_id: str = ""


async def _run_adb(
    *args: str,
    device_id: str = "",
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[bytes, bytes, int]:
    """Run an ADB command and return (stdout, stderr, returncode)."""
    cmd: list[str] = ["adb"]
    if device_id:
        cmd.extend(["-s", device_id])
    cmd.extend(args)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"ADB command timed out ({timeout}s): {' '.join(cmd)}")
    return stdout, stderr, proc.returncode or 0


async def adb_shell(
    *args: str,
    device_id: str = "",
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Run `adb shell <args>` and return stdout as a string.

    Raises RuntimeError on non-zero exit.
    """
    stdout, stderr, rc = await _run_adb("shell", *args, device_id=device_id, timeout=timeout)
    if rc != 0:
        raise RuntimeError(f"adb shell failed (rc={rc}): {stderr.decode(errors='replace')}")
    return stdout.decode(errors="replace").strip()


async def adb_exec_out(
    *args: str,
    device_id: str = "",
    timeout: float = DEFAULT_TIMEOUT,
) -> bytes:
    """Run `adb exec-out <args>` and return raw stdout bytes."""
    stdout, stderr, rc = await _run_adb("exec-out", *args, device_id=device_id, timeout=timeout)
    if rc != 0:
        raise RuntimeError(f"adb exec-out failed (rc={rc}): {stderr.decode(errors='replace')}")
    return stdout


async def list_devices() -> list[AdbDevice]:
    """List connected ADB devices via `adb devices -l`."""
    try:
        stdout, _, rc = await _run_adb("devices", "-l", timeout=DEFAULT_TIMEOUT)
    except (FileNotFoundError, TimeoutError):
        return []

    if rc != 0:
        return []

    devices: list[AdbDevice] = []
    for line in stdout.decode(errors="replace").splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial = parts[0]
        status = parts[1]
        # Parse key:value pairs (e.g., model:Pixel_9_Pro transport_id:3)
        model = ""
        transport_id = ""
        for part in parts[2:]:
            if part.startswith("model:"):
                model = part.split(":", 1)[1].replace("_", " ")
            elif part.startswith("transport_id:"):
                transport_id = part.split(":", 1)[1]
        devices.append(AdbDevice(
            serial=serial,
            status=status,
            model=model,
            transport_id=transport_id,
        ))
    return devices


async def connect(host: str, port: int = 5555) -> str:
    """Connect to a device via wireless ADB. Returns status message."""
    target = f"{host}:{port}"
    stdout, stderr, rc = await _run_adb("connect", target, timeout=10.0)
    result = stdout.decode(errors="replace").strip()
    if "connected" in result.lower():
        return result
    err = stderr.decode(errors="replace").strip()
    raise RuntimeError(f"ADB connect failed: {result or err}")


async def disconnect(device_id: str) -> str:
    """Disconnect a wireless ADB device."""
    stdout, _, _ = await _run_adb("disconnect", device_id, timeout=DEFAULT_TIMEOUT)
    return stdout.decode(errors="replace").strip()


async def device_info(device_id: str) -> dict[str, str]:
    """Get device properties (model, Android version, screen size, etc.)."""
    info: dict[str, str] = {}
    try:
        info["model"] = await adb_shell("getprop", "ro.product.model", device_id=device_id)
        info["androidVersion"] = await adb_shell("getprop", "ro.build.version.release", device_id=device_id)
        info["sdk"] = await adb_shell("getprop", "ro.build.version.sdk", device_id=device_id)
        info["manufacturer"] = await adb_shell("getprop", "ro.product.manufacturer", device_id=device_id)

        # Screen size: "Physical size: 1080x2340"
        wm_out = await adb_shell("wm", "size", device_id=device_id)
        match = re.search(r"(\d+)x(\d+)", wm_out)
        if match:
            info["screenWidth"] = match.group(1)
            info["screenHeight"] = match.group(2)
    except Exception as e:
        logger.warning("[adb] Failed to get device info for %s: %s", device_id, e)
    return info


async def is_device_online(device_id: str) -> bool:
    """Check if a device is reachable."""
    try:
        stdout, _, rc = await _run_adb("shell", "echo", "ok", device_id=device_id, timeout=3.0)
        return rc == 0 and b"ok" in stdout
    except (TimeoutError, RuntimeError):
        return False


async def push_file(local_path: str, remote_path: str, device_id: str = "") -> None:
    """Push a file to the device via `adb push`."""
    _, stderr, rc = await _run_adb("push", local_path, remote_path, device_id=device_id, timeout=30.0)
    if rc != 0:
        raise RuntimeError(f"adb push failed: {stderr.decode(errors='replace')}")


async def forward_port(local_port: int, remote: str, device_id: str = "") -> None:
    """Set up ADB port forwarding: `adb forward tcp:{local_port} {remote}`."""
    _, stderr, rc = await _run_adb("forward", f"tcp:{local_port}", remote, device_id=device_id)
    if rc != 0:
        raise RuntimeError(f"adb forward failed: {stderr.decode(errors='replace')}")


async def remove_forward(local_port: int, device_id: str = "") -> None:
    """Remove ADB port forwarding."""
    await _run_adb("forward", "--remove", f"tcp:{local_port}", device_id=device_id)


async def screencap(device_id: str, timeout: float = 5.0) -> bytes:
    """Capture a screenshot as PNG bytes (fallback, slower than scrcpy)."""
    return await adb_exec_out("screencap", "-p", device_id=device_id, timeout=timeout)
