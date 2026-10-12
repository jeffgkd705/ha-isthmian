"""Binary sensors for UISP NMS: device connected to UISP, port link."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import LINK_STATUS_ACTIVE
from .coordinator import UispConfigEntry, UispCoordinator
from .entity import UispDeviceEntity, UispPortEntity, is_port


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UispConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up UISP binary sensors."""
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = []
    for device_id, data in coordinator.data.items():
        entities.append(UispConnectedSensor(coordinator, device_id))
        entities.extend(
            UispLinkSensor(coordinator, device_id, name)
            for name, iface in data.interfaces.items()
            if is_port(iface)
        )
    async_add_entities(entities)


class UispConnectedSensor(UispDeviceEntity, BinarySensorEntity):
    """Whether UISP currently has a connection to the device."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "connected"

    def __init__(self, coordinator: UispCoordinator, device_id: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device_id, "connected")

    @property
    def available(self) -> bool:
        """Stay available when the device is disconnected; that is the state."""
        return self.coordinator.last_update_success and self._device_id in self.coordinator.data

    @property
    def is_on(self) -> bool:
        """Return True if connected."""
        return self.device_data.connected

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Raw UISP status (active, disconnected, ...)."""
        return {"status": self.overview.get("status")}


class UispLinkSensor(UispPortEntity, BinarySensorEntity):
    """Whether a port has link."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "link"

    def __init__(self, coordinator: UispCoordinator, device_id: str, interface: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device_id, interface, "link")

    @property
    def is_on(self) -> bool:
        """Return True if the port has link."""
        return (self.iface.get("status") or {}).get("status") == LINK_STATUS_ACTIVE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Negotiated speed and port settings."""
        status = self.iface.get("status") or {}
        poe = self.iface.get("poe") or {}
        stp = ((self.iface.get("port") or {}).get("stp") or {})
        return {
            **super().extra_state_attributes,
            "speed": status.get("currentSpeed"),
            "enabled": self.iface.get("enabled"),
            "poe_output": poe.get("output"),
            "poe_power": (self.iface.get("statistics") or {}).get("poePower"),
            "stp_state": stp.get("state") if stp.get("enabled") else None,
            "cable_test": (self.iface.get("capabilities") or {}).get("supportCableTest") is True,
            **self._vlan_attributes(),
        }

    def _vlan_attributes(self) -> dict[str, Any]:
        """Untagged and tagged VLANs of the port, with names where known."""
        vlans = self.device_data.vlans
        names = self.coordinator.vlan_names
        native = vlans.native.get(self._interface)
        return {
            "native_vlan": native,
            "native_vlan_name": names.get(native) if native is not None else None,
            "tagged_vlans": [
                {"id": vlan_id, "name": names.get(vlan_id)}
                for vlan_id in vlans.tagged.get(self._interface, [])
            ],
        }
