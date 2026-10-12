"""Switches for Isthmian: PoE output and port enable per port."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DEVICE_TYPE_ROUTER, DOMAIN, POE_OFF
from .coordinator import UispConfigEntry, UispCoordinator
from .entity import UispPortEntity, is_port

ATTR_LAST_ON_OUTPUT = "last_on_output"


def poe_on_outputs(iface: dict[str, Any]) -> list[str]:
    """Return the PoE outputs other than 'off' that a port supports."""
    return [c for c in (iface.get("poe") or {}).get("capacities") or [] if c != POE_OFF]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UispConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up UISP switches."""
    coordinator = entry.runtime_data
    entities: list[SwitchEntity] = []
    for device_id, data in coordinator.data.items():
        for name, iface in data.interfaces.items():
            if not is_port(iface):
                continue
            if poe_on_outputs(iface):
                entities.append(UispPoeSwitch(coordinator, device_id, name))
            entities.append(UispPortEnabledSwitch(coordinator, device_id, name))
    async_add_entities(entities)


class UispPoeSwitch(UispPortEntity, SwitchEntity, RestoreEntity):
    """PoE output of a port. Turning on restores the last output in use."""

    _attr_device_class = SwitchDeviceClass.OUTLET
    _attr_translation_key = "poe"

    def __init__(self, coordinator: UispCoordinator, device_id: str, interface: str) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, device_id, interface, "poe")
        self._key = (device_id, interface)

    async def async_added_to_hass(self) -> None:
        """Restore the last PoE output used, in case the port is off at startup."""
        await super().async_added_to_hass()
        if self._key in self.coordinator.last_poe_output:
            return
        if (last := await self.async_get_last_state()) and (
            output := last.attributes.get(ATTR_LAST_ON_OUTPUT)
        ):
            self.coordinator.last_poe_output[self._key] = output

    @property
    def is_on(self) -> bool:
        """Return True if the port supplies power."""
        return (self.iface.get("poe") or {}).get("output") not in (None, POE_OFF)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Current and last-used PoE output."""
        return {
            **super().extra_state_attributes,
            "output": (self.iface.get("poe") or {}).get("output"),
            ATTR_LAST_ON_OUTPUT: self.coordinator.last_poe_output.get(self._key),
        }

    def _on_output(self) -> str:
        """Pick the output for turning on, never guessing between voltages."""
        if output := self.coordinator.last_poe_output.get(self._key):
            return output
        outputs = poe_on_outputs(self.iface)
        if len(outputs) == 1:
            return outputs[0]
        # e.g. 'active' (48V 802.3af/at) vs '24v' passive: the wrong one can
        # damage the powered device, so the user must choose.
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="poe_output_unknown",
            translation_placeholders={"port": self._attr_translation_placeholders["port"]},
        )

    async def _set_output(self, output: str) -> None:
        def change(iface: dict[str, Any]) -> None:
            iface["poe"]["output"] = output

        await self.coordinator.async_write(
            self._device_id,
            self._interface,
            self.coordinator.client.set_poe_output(self._device_id, self._interface, output),
            change,
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn PoE on with the last output used."""
        await self._set_output(self._on_output())

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn PoE off."""
        await self._set_output(POE_OFF)


class UispPortEnabledSwitch(UispPortEntity, SwitchEntity):
    """Enable or disable a port.

    Disabled by default: turning off the port a device uses to reach UISP
    cuts it off, and it can then only be re-enabled locally on the device.
    """

    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False
    _attr_translation_key = "port_enabled"

    def __init__(self, coordinator: UispCoordinator, device_id: str, interface: str) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, device_id, interface, "enabled")

    @property
    def is_on(self) -> bool:
        """Return True if the port is enabled."""
        return bool(self.iface.get("enabled"))

    async def _set_enabled(self, enabled: bool) -> None:
        def change(iface: dict[str, Any]) -> None:
            iface["enabled"] = enabled

        client = self.coordinator.client
        if self.device_data.device.get("identification", {}).get("type") == DEVICE_TYPE_ROUTER:
            write = client.set_interface_blocked(self._device_id, self._interface, not enabled)
        else:
            write = client.set_interface_enabled(self._device_id, self._interface, enabled)
        await self.coordinator.async_write(self._device_id, self._interface, write, change)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the port."""
        await self._set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the port."""
        await self._set_enabled(False)
