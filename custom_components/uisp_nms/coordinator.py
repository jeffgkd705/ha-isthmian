"""Data update coordinator for UISP NMS."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import UispAuthError, UispCertificateError, UispClient, UispError
from .const import (
    DEFAULT_SCAN_INTERVAL,
    DEVICE_STATUS_ACTIVE,
    DOMAIN,
    MAX_PARALLEL_REQUESTS,
    POE_OFF,
    REFRESH_AFTER_WRITE,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class UispDeviceData:
    """One UISP device and its interfaces, keyed by interface name."""

    device: dict[str, Any]
    interfaces: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def connected(self) -> bool:
        """Return True if UISP currently has a connection to the device."""
        return self.device.get("overview", {}).get("status") == DEVICE_STATUS_ACTIVE


type UispConfigEntry = ConfigEntry[UispCoordinator]


class UispCoordinator(DataUpdateCoordinator[dict[str, UispDeviceData]]):
    """Poll UISP for all devices and their interfaces."""

    config_entry: UispConfigEntry

    def __init__(self, hass: HomeAssistant, entry: UispConfigEntry, client: UispClient) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.client = client
        # Last PoE output other than "off" seen per (device_id, interface),
        # so turning PoE back on restores the same voltage/mode.
        self.last_poe_output: dict[tuple[str, str], str] = {}
        self._cancel_refresh: CALLBACK_TYPE | None = None

    async def async_write(
        self,
        device_id: str,
        interface: str,
        write: Coroutine[Any, Any, None],
        change: Callable[[dict[str, Any]], None],
    ) -> None:
        """Run a write, show its result right away, and re-read shortly after.

        UISP answers before the device has applied the change, so the cached
        interface is updated with change() and a refresh is scheduled.
        """
        try:
            await write
        except UispError as err:
            raise HomeAssistantError(f"UISP rejected the change to {interface}: {err}") from err

        if self.data and device_id in self.data and interface in self.data[device_id].interfaces:
            iface = self.data[device_id].interfaces[interface]
            change(iface)
            self._remember_poe_output(device_id, interface, iface)
            self.async_update_listeners()

        if self._cancel_refresh:
            self._cancel_refresh()
        self._cancel_refresh = async_call_later(
            self.hass, REFRESH_AFTER_WRITE, HassJob(self._async_refresh_after_write)
        )

    async def _async_refresh_after_write(self, _now: datetime) -> None:
        self._cancel_refresh = None
        await self.async_request_refresh()

    async def async_shutdown(self) -> None:
        """Cancel a pending refresh on unload."""
        if self._cancel_refresh:
            self._cancel_refresh()
            self._cancel_refresh = None
        await super().async_shutdown()

    async def _async_update_data(self) -> dict[str, UispDeviceData]:
        """Fetch devices, then interfaces of each connected device."""
        try:
            devices = await self.client.get_devices()
            result: dict[str, UispDeviceData] = {}
            for device in devices:
                device_id = device.get("identification", {}).get("id")
                if device_id:
                    result[device_id] = UispDeviceData(device=device)

            semaphore = asyncio.Semaphore(MAX_PARALLEL_REQUESTS)

            async def fetch(data: UispDeviceData, device_id: str) -> None:
                async with semaphore:
                    interfaces = await self.client.get_interfaces(device_id)
                data.interfaces = {
                    i["identification"]["name"]: i
                    for i in interfaces
                    if i.get("identification", {}).get("name")
                }

            # Interfaces of a disconnected device would only be stale copies.
            await asyncio.gather(
                *(fetch(d, i) for i, d in result.items() if d.connected)
            )
        except UispAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except UispCertificateError as err:
            raise UpdateFailed(
                f"UISP certificate problem: {err}. If UISP's certificate was "
                "replaced, use Reconfigure on the integration to trust the new one"
            ) from err
        except UispError as err:
            raise UpdateFailed(f"Error communicating with UISP: {err}") from err

        # Keep the previous interfaces for devices that are now disconnected,
        # so their entities keep their attributes while reported unavailable.
        if self.data:
            for device_id, data in result.items():
                if not data.connected and device_id in self.data:
                    data.interfaces = self.data[device_id].interfaces

        for device_id, data in result.items():
            for name, iface in data.interfaces.items():
                self._remember_poe_output(device_id, name, iface)

        return result

    def _remember_poe_output(self, device_id: str, interface: str, iface: dict[str, Any]) -> None:
        output = (iface.get("poe") or {}).get("output")
        if output and output != POE_OFF:
            self.last_poe_output[(device_id, interface)] = output
