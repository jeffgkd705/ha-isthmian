"""Tests for setup, entities, writes and the power-cycle service."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache,
)

from custom_components.uisp_nms.api import UispAuthError
from custom_components.uisp_nms.const import DOMAIN, SERVICE_POWER_CYCLE_PORT
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .conftest import ROUTER, SWITCH_8, SWITCH_10X, FakeUisp

AP_POE = "switch.family_room_switch_2_port_8_poe"
ES8_PORT3_POE = "switch.edgeswitch_8_port_3_poe"
ES8_PORT8_POE = "switch.edgeswitch_8_port_8_poe"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_entities(hass: HomeAssistant, config_entry, fake_uisp: FakeUisp) -> None:
    """Devices, ports and PoE controls become entities with sensible names."""
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    assert hass.states.get("binary_sensor.family_room_switch_2_connected_to_uisp").state == "on"
    assert hass.states.get("sensor.family_room_switch_2_cpu_usage").state == "18"
    assert hass.states.get("sensor.family_room_switch_2_last_boot").state.startswith("2026-08-14")
    # Temperature only where the device reports one.
    assert hass.states.get("sensor.family_room_switch_2_temperature") is None
    assert hass.states.get("sensor.edgeswitch_8_temperature").state == "41.0"

    link = hass.states.get("binary_sensor.family_room_switch_2_port_8_link")
    assert link.state == "on"
    assert link.attributes["description"] == "Family Room AP"
    assert link.attributes["speed"] == "1000-full"
    rx = hass.states.get("sensor.family_room_switch_2_port_8_rx_rate")
    assert float(rx.state) == pytest.approx(4.195512)
    assert rx.attributes["unit_of_measurement"] == "Mbit/s"

    # Router ethernet ports, but not VLAN interfaces or LAGs.
    assert hass.states.get("binary_sensor.gw_eth0_link").state == "on"
    assert not [s for s in hass.states.async_entity_ids() if "eth1_10" in s or "lag" in s]

    # PoE switch on PoE ports only; a select only where there is a choice.
    assert hass.states.get(AP_POE).state == "on"
    assert hass.states.get("switch.family_room_switch_2_port_1_poe") is None
    assert hass.states.get("select.family_room_switch_2_port_8_poe_output") is None
    select = hass.states.get("select.edgeswitch_8_port_3_poe_output")
    assert select.state == "24v"
    assert select.attributes["options"] == ["off", "active", "24v"]

    # Port enable switches exist but are disabled by default.
    reg = er.async_get(hass)
    enabled = reg.async_get("switch.family_room_switch_2_port_1_enabled")
    assert enabled.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_poe_off_and_on_restores_output(
    hass: HomeAssistant, config_entry, fake_uisp: FakeUisp
) -> None:
    """Turning PoE back on uses the output that was on before."""
    await _setup(hass, config_entry)

    await hass.services.async_call("switch", "turn_off", {"entity_id": ES8_PORT3_POE}, blocking=True)
    assert hass.states.get(ES8_PORT3_POE).state == "off"
    device_id, name, body = fake_uisp.puts[-1]
    assert (device_id, name, body["poe"]["output"]) == (SWITCH_8, "port3", "off")
    # The full interface object is sent back, as the UISP UI does.
    assert body["statistics"]["rxrate"] == 4195512

    await hass.services.async_call("switch", "turn_on", {"entity_id": ES8_PORT3_POE}, blocking=True)
    assert fake_uisp.puts[-1][2]["poe"]["output"] == "24v"
    assert hass.states.get(ES8_PORT3_POE).state == "on"


async def test_poe_on_never_guesses_voltage(
    hass: HomeAssistant, config_entry, fake_uisp: FakeUisp
) -> None:
    """A port with several outputs and no known last output refuses to turn on."""
    await _setup(hass, config_entry)
    with pytest.raises(HomeAssistantError, match="more than one PoE output"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": ES8_PORT8_POE}, blocking=True
        )
    assert fake_uisp.puts == []

    # Choosing the output with the select works and is remembered.
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.edgeswitch_8_port_8_poe_output", "option": "active"},
        blocking=True,
    )
    await hass.services.async_call("switch", "turn_off", {"entity_id": ES8_PORT8_POE}, blocking=True)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=10))
    await hass.async_block_till_done()
    await hass.services.async_call("switch", "turn_on", {"entity_id": ES8_PORT8_POE}, blocking=True)
    assert fake_uisp.puts[-1][2]["poe"]["output"] == "active"


async def test_poe_single_output_turns_on(
    hass: HomeAssistant, config_entry, fake_uisp: FakeUisp
) -> None:
    """With only one output besides off (ES-10X: 24v), turning on needs no history."""
    for iface in fake_uisp.interfaces[SWITCH_10X]:
        if iface["identification"]["name"] == "port8":
            iface["poe"]["output"] = "off"
    await _setup(hass, config_entry)
    await hass.services.async_call("switch", "turn_on", {"entity_id": AP_POE}, blocking=True)
    assert fake_uisp.puts[-1][2]["poe"]["output"] == "24v"


async def test_last_output_restored_after_restart(
    hass: HomeAssistant, config_entry, fake_uisp: FakeUisp
) -> None:
    """The last output survives a Home Assistant restart."""
    mock_restore_cache(hass, [State(ES8_PORT8_POE, "off", {"last_on_output": "active"})])
    await _setup(hass, config_entry)
    await hass.services.async_call("switch", "turn_on", {"entity_id": ES8_PORT8_POE}, blocking=True)
    assert fake_uisp.puts[-1][2]["poe"]["output"] == "active"


async def test_power_cycle(hass: HomeAssistant, config_entry, fake_uisp: FakeUisp) -> None:
    """power_cycle_port turns PoE off and back on with the same output."""
    await _setup(hass, config_entry)
    with patch("custom_components.uisp_nms.asyncio.sleep") as sleep:
        await hass.services.async_call(
            DOMAIN, SERVICE_POWER_CYCLE_PORT, {"entity_id": AP_POE, "off_seconds": 3}, blocking=True
        )
    sleep.assert_awaited_once_with(3)
    assert [p[2]["poe"]["output"] for p in fake_uisp.puts] == ["off", "24v"]

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_POWER_CYCLE_PORT,
            {"entity_id": "sensor.family_room_switch_2_cpu_usage"},
            blocking=True,
        )


async def test_power_cycle_refuses_port_that_is_off(
    hass: HomeAssistant, config_entry, fake_uisp: FakeUisp
) -> None:
    """Power-cycling a port that is off would turn it on; refuse instead."""
    await _setup(hass, config_entry)
    with pytest.raises(ServiceValidationError, match="is off"):
        await hass.services.async_call(
            DOMAIN, SERVICE_POWER_CYCLE_PORT, {"entity_id": ES8_PORT8_POE}, blocking=True
        )


async def test_disconnected_device(hass: HomeAssistant, config_entry, fake_uisp: FakeUisp) -> None:
    """A device UISP lost contact with: connected sensor off, the rest unavailable."""
    await _setup(hass, config_entry)
    fake_uisp.devices[0]["overview"]["status"] = "disconnected"
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get("binary_sensor.family_room_switch_2_connected_to_uisp").state == "off"
    assert hass.states.get(AP_POE).state == "unavailable"
    assert hass.states.get("binary_sensor.gw_eth0_link").state == "on"


async def test_rejected_write(hass: HomeAssistant, config_entry, fake_uisp: FakeUisp) -> None:
    """A write UISP rejects (e.g. read-only token) is reported, state unchanged."""
    await _setup(hass, config_entry)
    with (
        patch(
            "custom_components.uisp_nms.api.UispClient.update_interface",
            side_effect=UispAuthError("PUT rejected (HTTP 403)"),
        ),
        pytest.raises(HomeAssistantError, match="rejected"),
    ):
        await hass.services.async_call("switch", "turn_off", {"entity_id": AP_POE}, blocking=True)
    assert hass.states.get(AP_POE).state == "on"


async def test_auth_failure_starts_reauth(hass: HomeAssistant, config_entry) -> None:
    """A rejected token at setup asks the user for a new one."""
    with patch(
        "custom_components.uisp_nms.api.UispClient.get_devices",
        side_effect=UispAuthError("rejected"),
    ):
        config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == ["reauth"]


async def test_unload(hass: HomeAssistant, config_entry, fake_uisp: FakeUisp) -> None:
    """The entry unloads cleanly."""
    await _setup(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED
    # Router is unaffected by the switch-specific fixtures.
    assert ROUTER
