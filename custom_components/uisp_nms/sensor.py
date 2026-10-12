"""Sensors for Isthmian: device CPU, memory, boot time; port traffic rates."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfDataRate, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import UispConfigEntry, UispCoordinator
from .entity import UispDeviceEntity, UispPortEntity, is_port


@dataclass(frozen=True, kw_only=True)
class UispDeviceSensorDescription(SensorEntityDescription):
    """Describes a device-level sensor; value_fn gets the device record."""

    value_fn: Callable[[dict[str, Any]], Any]


def _started(device: dict[str, Any]) -> datetime | None:
    started = device.get("identification", {}).get("started")
    return dt_util.parse_datetime(started) if started else None


DEVICE_SENSORS: tuple[UispDeviceSensorDescription, ...] = (
    UispDeviceSensorDescription(
        key="cpu",
        translation_key="cpu",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("overview", {}).get("cpu"),
    ),
    UispDeviceSensorDescription(
        key="ram",
        translation_key="ram",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("overview", {}).get("ram"),
    ),
    UispDeviceSensorDescription(
        key="temperature",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("overview", {}).get("temperature"),
    ),
    UispDeviceSensorDescription(
        key="last_boot",
        translation_key="last_boot",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_started,
    ),
)


@dataclass(frozen=True, kw_only=True)
class UispPortSensorDescription(SensorEntityDescription):
    """Describes a port sensor; value_fn gets the interface record."""

    value_fn: Callable[[dict[str, Any]], Any]


PORT_SENSORS: tuple[UispPortSensorDescription, ...] = (
    UispPortSensorDescription(
        key="rx_rate",
        translation_key="rx_rate",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.BITS_PER_SECOND,
        suggested_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda i: (i.get("statistics") or {}).get("rxrate"),
    ),
    UispPortSensorDescription(
        key="tx_rate",
        translation_key="tx_rate",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.BITS_PER_SECOND,
        suggested_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda i: (i.get("statistics") or {}).get("txrate"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UispConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up UISP sensors."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for device_id, data in coordinator.data.items():
        entities.extend(
            UispDeviceSensor(coordinator, device_id, desc)
            for desc in DEVICE_SENSORS
            # Skip sensors the device never reports (e.g. no temperature probe).
            if desc.value_fn(data.device) is not None
        )
        for name, iface in data.interfaces.items():
            if is_port(iface):
                entities.extend(
                    UispPortSensor(coordinator, device_id, name, desc) for desc in PORT_SENSORS
                )
    async_add_entities(entities)


class UispDeviceSensor(UispDeviceEntity, SensorEntity):
    """A device-level sensor."""

    entity_description: UispDeviceSensorDescription

    def __init__(
        self, coordinator: UispCoordinator, device_id: str, description: UispDeviceSensorDescription
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.device_data.device)


class UispPortSensor(UispPortEntity, SensorEntity):
    """A per-port sensor."""

    entity_description: UispPortSensorDescription

    def __init__(
        self,
        coordinator: UispCoordinator,
        device_id: str,
        interface: str,
        description: UispPortSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device_id, interface, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.iface)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Add error and drop counters to the RX/TX sensors."""
        stats = self.iface.get("statistics") or {}
        return {
            **super().extra_state_attributes,
            "errors": stats.get("errors"),
            "dropped": stats.get("dropped"),
        }
