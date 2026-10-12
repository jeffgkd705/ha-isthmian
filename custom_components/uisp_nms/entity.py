"""Base entities for UISP NMS."""
from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, PORT_INTERFACE_TYPES
from .coordinator import UispCoordinator, UispDeviceData


def is_port(iface: dict[str, Any]) -> bool:
    """Return True for physical ports (switch ports, router ethernet)."""
    return iface.get("identification", {}).get("type") in PORT_INTERFACE_TYPES


def port_label(iface: dict[str, Any]) -> str:
    """Stable, human-readable port name: 'Port 8' on switches, 'eth0' on routers."""
    ident = iface.get("identification", {})
    if ident.get("type") == "port" and ident.get("position") is not None:
        return f"Port {ident['position']}"
    return ident.get("name", "")


class UispDeviceEntity(CoordinatorEntity[UispCoordinator]):
    """An entity belonging to one UISP device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: UispCoordinator, device_id: str, key: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_{key}"
        ident = self.device_data.device.get("identification", {})
        connections = {(CONNECTION_NETWORK_MAC, ident["mac"])} if ident.get("mac") else set()
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            connections=connections,
            name=ident.get("displayName") or ident.get("name") or ident.get("model"),
            manufacturer=ident.get("vendor") or "Ubiquiti",
            model=ident.get("modelName") or ident.get("model"),
            model_id=ident.get("model"),
            sw_version=ident.get("firmwareVersion"),
            configuration_url=f"{coordinator.client.base_url}/nms/devices",
        )

    @property
    def device_data(self) -> UispDeviceData:
        """Return the latest data for this device."""
        return self.coordinator.data[self._device_id]

    @property
    def overview(self) -> dict[str, Any]:
        """Return the device overview (status, cpu, ram, ...)."""
        return self.device_data.device.get("overview") or {}

    @property
    def available(self) -> bool:
        """Available while UISP has fresh data for the device."""
        return (
            super().available
            and self._device_id in self.coordinator.data
            and self.device_data.connected
        )


class UispPortEntity(UispDeviceEntity):
    """An entity belonging to one interface of a UISP device."""

    def __init__(
        self, coordinator: UispCoordinator, device_id: str, interface: str, key: str
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, device_id, f"{interface}_{key}")
        self._interface = interface
        self._attr_translation_placeholders = {"port": port_label(self.iface)}

    @property
    def iface(self) -> dict[str, Any]:
        """Return the latest data for this interface."""
        return self.device_data.interfaces.get(self._interface, {})

    @property
    def available(self) -> bool:
        """Available while the device is connected and reports the interface."""
        return super().available and self._interface in self.device_data.interfaces

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Port description, so users can tell ports apart, and its UISP name and position."""
        ident = self.iface.get("identification", {})
        return {
            "description": ident.get("description"),
            "interface": self._interface,
            "position": ident.get("position"),
        }
