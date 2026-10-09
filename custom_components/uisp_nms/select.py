"""PoE output selection for ports offering more than one PoE output."""
from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import UispConfigEntry, UispCoordinator
from .entity import UispPortEntity, is_port
from .switch import poe_on_outputs


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UispConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up PoE output selects where there is a choice of voltage/mode."""
    coordinator = entry.runtime_data
    async_add_entities(
        UispPoeOutputSelect(coordinator, device_id, name)
        for device_id, data in coordinator.data.items()
        for name, iface in data.interfaces.items()
        if is_port(iface) and len(poe_on_outputs(iface)) > 1
    )


class UispPoeOutputSelect(UispPortEntity, SelectEntity):
    """Choose a port's PoE output, e.g. off / active (48V) / 24v passive."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "poe_output"

    def __init__(self, coordinator: UispCoordinator, device_id: str, interface: str) -> None:
        """Initialize the select."""
        super().__init__(coordinator, device_id, interface, "poe_output")

    @property
    def options(self) -> list[str]:
        """Return the outputs the port supports."""
        return list((self.iface.get("poe") or {}).get("capacities") or [])

    @property
    def current_option(self) -> str | None:
        """Return the current output."""
        return (self.iface.get("poe") or {}).get("output")

    async def async_select_option(self, option: str) -> None:
        """Set the PoE output."""

        def change(iface: dict[str, Any]) -> None:
            iface["poe"]["output"] = option

        await self.coordinator.async_write(
            self._device_id,
            self._interface,
            self.coordinator.client.set_poe_output(self._device_id, self._interface, option),
            change,
        )
