"""The Isthmian integration."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import voluptuous as vol

from homeassistant.components.http import StaticPathConfig
from homeassistant.const import CONF_API_TOKEN, CONF_URL, Platform
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import UispClient
from .const import (
    CARD_URL,
    CONF_FINGERPRINT,
    DOMAIN,
    SERVICE_CABLE_TEST,
    SERVICE_GET_PORT_CLIENTS,
    SERVICE_POWER_CYCLE_PORT,
)
from .coordinator import UispConfigEntry, UispCoordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

CARD_PATH = Path(__file__).parent / "frontend" / "uisp-nms-card.js"

POWER_CYCLE_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Optional("off_seconds", default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=120)),
    }
)

PORT_SCHEMA = vol.Schema({vol.Required("entity_id"): cv.entity_id})


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


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve the bundled dashboard card and load it on every dashboard.

    The file is served with long cache headers, so the URL carries a hash of
    its contents. Adapted from marosmars/edgeswitch-homeassistant (MIT).
    """
    if hass.http is None or "frontend" not in hass.config.components:
        return
    from homeassistant.components.frontend import add_extra_js_url  # noqa: PLC0415

    digest = await hass.async_add_executor_job(
        lambda: hashlib.sha256(CARD_PATH.read_bytes()).hexdigest()[:10]
    )
    await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, str(CARD_PATH), True)])
    add_extra_js_url(hass, f"{CARD_URL}?v={digest}")


def _resolve_port(hass: HomeAssistant, entity_id: str) -> tuple[UispCoordinator, str, str]:
    """Return the coordinator, device id and interface of a UISP port entity.

    Port entities have unique ids "{device_id}_{interface}_{key}".
    """
    entry = er.async_get(hass).async_get(entity_id)
    parts = entry.unique_id.split("_") if entry and entry.platform == DOMAIN else []
    config_entry = (
        hass.config_entries.async_get_entry(entry.config_entry_id)
        if entry and entry.config_entry_id
        else None
    )
    if len(parts) < 3 or config_entry is None or config_entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_port",
            translation_placeholders={"entity_id": entity_id},
        )
    coordinator: UispCoordinator = config_entry.runtime_data
    device_id, interface = parts[0], parts[1]
    data = coordinator.data.get(device_id)
    if data is None or interface not in data.interfaces:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_port",
            translation_placeholders={"entity_id": entity_id},
        )
    if not data.connected:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="device_disconnected",
            translation_placeholders={"entity_id": entity_id},
        )
    return coordinator, device_id, interface


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the dashboard card and register the services."""
    await _async_register_card(hass)

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

    async def async_get_port_clients(call: ServiceCall) -> ServiceResponse:
        """Return the devices seen on a port, from a freshly fetched MAC table."""
        coordinator, device_id, interface = _resolve_port(hass, call.data["entity_id"])
        return await coordinator.async_port_clients(device_id, interface)

    async def async_cable_test(call: ServiceCall) -> ServiceResponse:
        """Run a cable test on a port and return the result. The port loses link meanwhile."""
        entity_id = call.data["entity_id"]
        coordinator, device_id, interface = _resolve_port(hass, entity_id)
        iface = coordinator.data[device_id].interfaces[interface]
        if not (iface.get("capabilities") or {}).get("supportCableTest"):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="cable_test_unsupported",
                translation_placeholders={"entity_id": entity_id},
            )
        return await coordinator.async_cable_test(device_id, interface)

    hass.services.async_register(
        DOMAIN, SERVICE_POWER_CYCLE_PORT, async_power_cycle_port, schema=POWER_CYCLE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_PORT_CLIENTS,
        async_get_port_clients,
        schema=PORT_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CABLE_TEST,
        async_cable_test,
        schema=PORT_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: UispConfigEntry) -> bool:
    """Set up Isthmian from a config entry."""
    coordinator = UispCoordinator(hass, entry, create_client(hass, dict(entry.data)))
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: UispConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
