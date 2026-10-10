"""The UISP NMS integration."""
from __future__ import annotations

import asyncio

import voluptuous as vol

from homeassistant.const import CONF_API_TOKEN, CONF_URL, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import UispClient
from .const import CONF_FINGERPRINT, DOMAIN, SERVICE_POWER_CYCLE_PORT
from .coordinator import UispConfigEntry, UispCoordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

POWER_CYCLE_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Optional("off_seconds", default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=120)),
    }
)


def create_client(hass: HomeAssistant, data: dict) -> UispClient:
    """Create a client for config entry data."""
    fingerprint = data.get(CONF_FINGERPRINT)
    return UispClient(
        url=data[CONF_URL],
        token=data[CONF_API_TOKEN],
        # A pinned certificate replaces normal verification.
        session=async_get_clientsession(hass, verify_ssl=fingerprint is None),
        fingerprint=fingerprint,
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the power_cycle_port service."""

    async def async_power_cycle_port(call: ServiceCall) -> None:
        """Turn a PoE switch off, wait, and turn it back on with the same output.

        Runs inside Home Assistant, so the port is powered again even if the
        caller (e.g. a dashboard) disconnects meanwhile.

        Adapted from the cycle_port service of marosmars/edgeswitch-homeassistant
        (MIT); see THIRD_PARTY_NOTICES.md.
        """
        entity_id = call.data["entity_id"]
        entry = er.async_get(hass).async_get(entity_id)
        if (
            entry is None
            or entry.platform != DOMAIN
            or entry.domain != "switch"
            or not entry.unique_id.endswith("_poe")
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_poe_switch",
                translation_placeholders={"entity_id": entity_id},
            )
        state = hass.states.get(entity_id)
        if state is None or state.state != "on":
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="poe_not_on",
                translation_placeholders={"entity_id": entity_id},
            )
        target = {"entity_id": entity_id}
        await hass.services.async_call("switch", "turn_off", target, blocking=True)
        await asyncio.sleep(call.data["off_seconds"])
        await hass.services.async_call("switch", "turn_on", target, blocking=True)

    hass.services.async_register(
        DOMAIN, SERVICE_POWER_CYCLE_PORT, async_power_cycle_port, schema=POWER_CYCLE_SCHEMA
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: UispConfigEntry) -> bool:
    """Set up UISP NMS from a config entry."""
    coordinator = UispCoordinator(hass, entry, create_client(hass, dict(entry.data)))
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: UispConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
