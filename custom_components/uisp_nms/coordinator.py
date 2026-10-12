"""Data update coordinator for Isthmian."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import time
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
    DEVICE_TYPE_SWITCH,
    DOMAIN,
    MAC_TABLE_MAX_AGE,
    MAX_PARALLEL_REQUESTS,
    POE_OFF,
    REFRESH_AFTER_WRITE,
    VLAN_REFRESH_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class UispVlans:
    """VLAN membership of a device's ports, keyed by interface name."""

    native: dict[str, int] = field(default_factory=dict)
    tagged: dict[str, list[int]] = field(default_factory=dict)
    names: dict[int, str] = field(default_factory=dict)

    @classmethod
    def from_switch_vlans(cls, config: dict[str, Any]) -> UispVlans:
        """Parse a switch's VLAN configuration (GET /devices/{id}/vlans).

        Each VLAN lists its member ports as tagged or untagged. VLAN ranges
        are skipped.
        """
        native: dict[str, int] = {}
        tagged: dict[str, list[int]] = {}
        names: dict[int, str] = {}
        for vlan in config.get("vlans") or []:
            if not isinstance(vlan, dict) or not str(vlan.get("id")).isdigit():
                continue
            vlan_id = int(vlan["id"])
            if vlan.get("name"):
                names[vlan_id] = vlan["name"]
            for member in vlan.get("participation") or []:
                name = (member.get("interface") or {}).get("id")
                if not name:
                    continue
                if member.get("mode") == "untagged":
                    native[name] = vlan_id
                elif member.get("mode") == "tagged":
                    tagged.setdefault(name, []).append(vlan_id)
        return cls(native, {name: sorted(ids) for name, ids in tagged.items()}, names)

    @classmethod
    def from_router_interfaces(cls, interfaces: dict[str, dict[str, Any]]) -> UispVlans:
        """Tagged VLANs of a router port are its VLAN sub-interfaces (eth1.10)."""
        tagged: dict[str, list[int]] = {}
        for iface in interfaces.values():
            vlan = iface.get("vlan") or {}
            if isinstance(vlan.get("id"), int) and vlan.get("parent"):
                tagged.setdefault(vlan["parent"], []).append(vlan["id"])
        return cls({}, {name: sorted(ids) for name, ids in tagged.items()})


@dataclass
class UispDeviceData:
    """One UISP device and its interfaces, keyed by interface name."""

    device: dict[str, Any]
    interfaces: dict[str, dict[str, Any]] = field(default_factory=dict)
    vlans: UispVlans = field(default_factory=UispVlans)

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
        # VLAN names by id, from the descriptions of VLAN interfaces (e.g. on the router).
        self.vlan_names: dict[int, str] = {}
        # Switch VLAN membership and when it was read (time.monotonic()).
        self._switch_vlans: dict[str, tuple[float, UispVlans]] = {}
        # Last MAC table fetched per device and when (time.monotonic()).
        self._mac_tables: dict[str, tuple[float, list[dict[str, Any]]]] = {}
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

        self._schedule_refresh_after_write()

    def _schedule_refresh_after_write(self) -> None:
        if self._cancel_refresh:
            self._cancel_refresh()
        self._cancel_refresh = async_call_later(
            self.hass, REFRESH_AFTER_WRITE, HassJob(self._async_refresh_after_write)
        )

    async def async_port_clients(self, device_id: str, interface: str) -> dict[str, Any]:
        """Return what the device's MAC table has learned on a port.

        Entries learned on a LAG the port belongs to, or on a VLAN interface
        of a router port (eth1.10), count for the port.
        """
        now = time.monotonic()
        cached = self._mac_tables.get(device_id)
        if cached is None or now - cached[0] >= MAC_TABLE_MAX_AGE:
            try:
                entries = await self.client.get_mac_table(device_id)
            except UispError as err:
                raise HomeAssistantError(f"Could not read the MAC table: {err}") from err
            cached = (now, entries)
            self._mac_tables[device_id] = cached
        interfaces = self.data[device_id].interfaces
        lag = next(
            (
                name
                for name, iface in interfaces.items()
                if interface in ((iface.get("lag") or {}).get("ports") or [])
            ),
            None,
        )
        names = {interface, lag} - {None}
        clients = [
            {
                "mac": entry.get("mac"),
                "hostname": entry.get("hostname") or None,
                "vendor": entry.get("vendor") or None,
                "ip_addresses": entry.get("addresses") or [],
                "vlan": entry.get("vlan"),
                "vlan_name": self.vlan_names.get(entry.get("vlan")),
            }
            for entry in cached[1]
            if entry.get("interfaceId") in names
            or str(entry.get("interfaceId", "")).startswith(f"{interface}.")
        ]
        clients.sort(key=lambda c: (c["hostname"] is None, (c["hostname"] or "").lower(), c["mac"] or ""))
        return {
            "lag": lag,
            "lag_ports": ((interfaces.get(lag) or {}).get("lag") or {}).get("ports") if lag else None,
            "clients": clients,
        }

    async def async_cable_test(self, device_id: str, interface: str) -> dict[str, Any]:
        """Run a cable test on a port; the port is re-read afterwards, as it lost link."""
        try:
            data = await self.client.cable_test(device_id, interface)
        except UispError as err:
            raise HomeAssistantError(f"UISP could not run the cable test on {interface}: {err}") from err
        finally:
            self._schedule_refresh_after_write()
        pairs = data.get("pairs") if isinstance(data.get("pairs"), dict) else {}
        result: dict[str, Any] = {
            "status": data.get("status"),
            "length_from": data.get("lengthFrom"),
            "length_to": data.get("lengthTo"),
            "pairs": {
                name.removeprefix("pair").lower(): {
                    "state": pair.get("state"),
                    "length_from": pair.get("lengthFrom"),
                    "length_to": pair.get("lengthTo"),
                }
                for name, pair in pairs.items()
                if isinstance(pair, dict)
            },
        }
        if "status" not in data:
            # Not the documented format; pass it on so it can be seen and supported.
            result["raw"] = data
        return result

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

        await self._async_update_vlans(result)
        return result

    async def _async_update_vlans(self, result: dict[str, UispDeviceData]) -> None:
        """Attach VLAN membership and collect VLAN names.

        Switch membership and VLAN names come from the switch's VLAN
        configuration, re-read at most every VLAN_REFRESH_INTERVAL seconds.
        Names from router VLAN interface descriptions take precedence. VLANs are informational, so a failure
        keeps the previous membership instead of failing the update.
        """
        names: dict[int, str] = {}
        now = time.monotonic()
        for device_id, data in result.items():
            for iface in data.interfaces.values():
                vlan = iface.get("vlan") or {}
                description = iface.get("identification", {}).get("description")
                if isinstance(vlan.get("id"), int) and description:
                    names.setdefault(vlan["id"], description)
            if data.device.get("identification", {}).get("type") != DEVICE_TYPE_SWITCH:
                data.vlans = UispVlans.from_router_interfaces(data.interfaces)
                continue
            fetched, vlans = self._switch_vlans.get(device_id, (None, None))
            if data.connected and (fetched is None or now - fetched >= VLAN_REFRESH_INTERVAL):
                try:
                    config = await self.client.get_device_vlans(device_id)
                except UispError as err:
                    _LOGGER.debug("Could not read VLANs of %s: %s", device_id, err)
                else:
                    vlans = UispVlans.from_switch_vlans(config)
                    self._switch_vlans[device_id] = (now, vlans)
            if vlans is not None:
                data.vlans = vlans
        for data in result.values():
            for vlan_id, name in data.vlans.names.items():
                names.setdefault(vlan_id, name)
        self.vlan_names = names

    def _remember_poe_output(self, device_id: str, interface: str, iface: dict[str, Any]) -> None:
        output = (iface.get("poe") or {}).get("output")
        if output and output != POE_OFF:
            self.last_poe_output[(device_id, interface)] = output
