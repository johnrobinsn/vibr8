"""mDNS/Bonjour discovery for ADB devices on the local network.

Android 11+ devices with wireless debugging enabled advertise via mDNS
as `_adb-tls-connect._tcp.local.`. This module discovers them and returns
their IP/port for wireless ADB connection.

Requires the `zeroconf` package (optional dependency).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class DiscoveredDevice:
    """An Android device discovered via mDNS."""
    name: str          # Service name (usually device model)
    ip: str            # IP address
    port: int          # ADB port
    properties: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ip": self.ip,
            "port": self.port,
            "source": "mdns",
            "properties": self.properties,
        }


class MdnsDiscovery:
    """Discovers ADB devices via mDNS/Bonjour.

    Uses the zeroconf library to listen for `_adb-tls-connect._tcp.local.`
    service advertisements on the local network.
    """

    SERVICE_TYPE = "_adb-tls-connect._tcp.local."

    def __init__(self) -> None:
        self._devices: dict[str, DiscoveredDevice] = {}  # service_name → device
        self._zeroconf: Any = None
        self._browser: Any = None
        self._available = False

        try:
            from zeroconf import Zeroconf, ServiceBrowser  # noqa: F401
            self._available = True
        except ImportError:
            logger.info("[mdns] zeroconf not installed — mDNS discovery disabled")

    @property
    def available(self) -> bool:
        return self._available

    async def start(self) -> None:
        """Start mDNS discovery in background."""
        if not self._available:
            return

        from zeroconf import Zeroconf, ServiceBrowser, ServiceStateChange

        loop = asyncio.get_running_loop()

        def on_service_state_change(
            zeroconf: Zeroconf,
            service_type: str,
            name: str,
            state_change: ServiceStateChange,
        ) -> None:
            if state_change == ServiceStateChange.Added:
                asyncio.run_coroutine_threadsafe(
                    self._on_service_added(zeroconf, service_type, name), loop
                )
            elif state_change == ServiceStateChange.Removed:
                self._devices.pop(name, None)
                logger.debug("[mdns] Device removed: %s", name)

        self._zeroconf = Zeroconf()
        self._browser = ServiceBrowser(
            self._zeroconf,
            self.SERVICE_TYPE,
            handlers=[on_service_state_change],
        )
        logger.info("[mdns] Discovery started for %s", self.SERVICE_TYPE)

    async def _on_service_added(
        self, zeroconf: Any, service_type: str, name: str
    ) -> None:
        """Handle a newly discovered ADB service."""
        from zeroconf import Zeroconf

        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(
            None, lambda: zeroconf.get_service_info(service_type, name)
        )
        if not info:
            return

        # Extract IP address
        addresses = info.parsed_scoped_addresses()
        if not addresses:
            return
        ip = addresses[0]

        # Extract properties
        props = {}
        if info.properties:
            for k, v in info.properties.items():
                key = k.decode() if isinstance(k, bytes) else str(k)
                val = v.decode() if isinstance(v, bytes) else str(v)
                props[key] = val

        device = DiscoveredDevice(
            name=name.replace(f".{self.SERVICE_TYPE}", ""),
            ip=ip,
            port=info.port or 5555,
            properties=props,
        )
        self._devices[name] = device
        logger.info("[mdns] Discovered device: %s at %s:%d", device.name, ip, device.port)

    def get_discovered(self) -> list[DiscoveredDevice]:
        """Return currently discovered devices."""
        return list(self._devices.values())

    async def stop(self) -> None:
        """Stop mDNS discovery."""
        if self._browser:
            self._browser.cancel()
            self._browser = None
        if self._zeroconf:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._zeroconf.close)
            self._zeroconf = None
        self._devices.clear()
        logger.info("[mdns] Discovery stopped")
