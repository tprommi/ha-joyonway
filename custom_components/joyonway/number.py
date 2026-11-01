"""Number platform for PV heating and configurable power estimates."""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .calculated_power import POWER_SETTINGS
from .coordinator import JoyonwayConfigEntry, JoyonwayCoordinator
from .entity import JoyonwayCoordinatorEntity, device_info
from .pv_heating import PVHeatingDelay, PVHeatingSetpoint


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JoyonwayConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Expose PV settings and the configurable component power estimates."""
    async_add_entities(
        [
            PVHeatingSetpoint(entry.runtime_data, entry),
            PVHeatingDelay(entry.runtime_data, entry, on=True),
            PVHeatingDelay(entry.runtime_data, entry, on=False),
            *(
                PowerSettingNumber(entry.runtime_data, entry, option_key, name_key, default)
                for option_key, name_key, default in POWER_SETTINGS
            ),
        ]
    )


class PowerSettingNumber(JoyonwayCoordinatorEntity, NumberEntity):
    """Persist one estimated component power in the config entry options."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_native_min_value = 0
    _attr_native_max_value = 20000
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: JoyonwayCoordinator,
        entry: JoyonwayConfigEntry,
        option_key: str,
        name_key: str,
        default: int,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._option_key = option_key
        self._default = default
        self._attr_translation_key = f"power_{name_key}"
        self._attr_unique_id = f"{entry.entry_id}_{self._attr_translation_key}"
        self._attr_device_info = device_info(entry)

    @property
    def native_value(self) -> float:
        return self._entry.options.get(self._option_key, self._default)

    async def async_set_native_value(self, value: float) -> None:
        options: dict[str, Any] = {
            **self._entry.options,
            self._option_key: round(value),
        }
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        self.coordinator.async_update_listeners()
