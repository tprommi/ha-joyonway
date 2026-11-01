"""Estimated spa power from active components and configurable watt values."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfPower

from .const import (
    OPT_POWER_HEATING,
    OPT_POWER_LIGHT,
    OPT_POWER_OZONE,
    OPT_POWER_PUMP_HIGH,
    OPT_POWER_PUMP_LOW,
)
from .entity import JoyonwayCoordinatorEntity, device_info

if TYPE_CHECKING:
    from .coordinator import JoyonwayConfigEntry, JoyonwayCoordinator


POWER_SETTINGS: tuple[tuple[str, str, int], ...] = (
    (OPT_POWER_PUMP_LOW, "pump_low", 370),
    (OPT_POWER_PUMP_HIGH, "pump_high", 2100),
    (OPT_POWER_HEATING, "heating", 2780),
    (OPT_POWER_OZONE, "ozone", 20),
    (OPT_POWER_LIGHT, "light", 5),
)

POWER_DEFAULTS: dict[str, int] = {
    option_key: default for option_key, _, default in POWER_SETTINGS
}


class CalculatedPowerSensor(JoyonwayCoordinatorEntity, SensorEntity):
    """Estimated active power based on spa states and configurable values."""

    _attr_has_entity_name = True
    _attr_translation_key = "calculated_power"
    _attr_icon = "mdi:flash"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_suggested_display_precision = 0

    def __init__(
        self, coordinator: JoyonwayCoordinator, entry: JoyonwayConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_calculated_power"
        self._attr_device_info = device_info(entry)

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data
        if data is None:
            return None

        options = self._entry.options
        watts = {key: options.get(key, default) for key, _, default in POWER_SETTINGS}
        nozzle = data.get("jets")
        jets_active = data.get("jets_active") is True or nozzle in ("low", "high")
        jets_high = nozzle == "high"
        circulation = data.get("circulation_active") is True
        heating = data.get("heater_active") is True
        ozone = data.get("ozone_active") is True
        light = data.get("light_active") is True

        pump_power = (
            watts[OPT_POWER_PUMP_HIGH]
            if jets_high
            else watts[OPT_POWER_PUMP_LOW]
            if jets_active or circulation
            else 0
        )
        return int(
            pump_power
            + (watts[OPT_POWER_HEATING] if heating else 0)
            + (watts[OPT_POWER_OZONE] if ozone else 0)
            + (watts[OPT_POWER_LIGHT] if light else 0)
        )
