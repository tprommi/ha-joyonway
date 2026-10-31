"""Optional PV heating control with a separate user thermostat state."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError

from .entity import JoyonwayCoordinatorEntity, device_info

if TYPE_CHECKING:
    from .coordinator import JoyonwayConfigEntry, JoyonwayCoordinator


OPT_PV_ENABLED = "pv_heating_enabled"
OPT_PV_SETPOINT = "pv_heating_setpoint"
OPT_PV_USER_SETPOINT = "pv_user_setpoint"
OPT_PV_USER_ENABLED = "pv_user_heater_enabled"


class PVHeatingController:
    """Arbitrate heater and setpoint writes between the user and PV control."""

    def __init__(self, coordinator: JoyonwayCoordinator) -> None:
        self.coordinator = coordinator
        options = coordinator.entry.options
        self.enabled = bool(options.get(OPT_PV_ENABLED, False))
        self.released = False
        self.active = False
        self.restoring_user_target = False
        self.setpoint = options.get(OPT_PV_SETPOINT)
        self.user_setpoint = options.get(OPT_PV_USER_SETPOINT)
        self.user_enabled = options.get(OPT_PV_USER_ENABLED)

    def _notify(self) -> None:
        self.coordinator.async_update_listeners()

    def _save(self, **changes: Any) -> None:
        options = {**self.coordinator.entry.options, **changes}
        self.coordinator.hass.config_entries.async_update_entry(
            self.coordinator.entry, options=options
        )

    def _heater_mode_allows_control(self) -> bool:
        mode = self.coordinator.heater_mode
        return mode is None or mode == "manual"

    def _can_take_over(self) -> bool:
        data = self.coordinator.data
        if not self.enabled or not self.released or not self.coordinator.is_connected:
            return False
        if data is None or not self._heater_mode_allows_control():
            return False
        if (
            not self.active
            and self.coordinator.adapter.is_heater_enabled(data) is not False
        ):
            return False
        if self.user_enabled is None:
            self.user_enabled = self.coordinator.adapter.is_heater_enabled(data)
        return self.user_enabled is False

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self._save(**{OPT_PV_ENABLED: enabled})
        self._reconcile()
        self._notify()

    def set_release(self, released: bool) -> None:
        self.released = released
        self._reconcile()
        self._notify()

    def set_setpoint(self, value: float) -> None:
        target = round(value)
        adapter = self.coordinator.adapter
        if not adapter.temp_min_c <= target <= adapter.temp_max_c:
            raise HomeAssistantError("PV heating setpoint is outside the spa range")
        self.setpoint = target
        self._save(**{OPT_PV_SETPOINT: target})
        if self.active:
            self._submit_setpoint(target, pv=True)
        self._notify()

    def set_user_setpoint(self, value: int) -> None:
        self.user_setpoint = value
        self._save(**{OPT_PV_USER_SETPOINT: value})
        self._notify()

    def user_turn_on(self) -> None:
        """Give a user HEAT request priority over PV ownership."""
        self.user_enabled = True
        self._save(**{OPT_PV_USER_ENABLED: True})
        if self.active:
            self.active = False
            self._restore_user_setpoint()
        self._notify()

    def user_turn_off(self) -> bool:
        """Return true if PV immediately takes over the heater command."""
        self.user_enabled = False
        self._save(**{OPT_PV_USER_ENABLED: False})
        self._reconcile()
        self._notify()
        return self.active

    def on_broadcast(self) -> None:
        """Recheck eligibility after a controller state change or reconnect."""
        if self.active or (self.enabled and self.released):
            self._reconcile()
        if (
            self.restoring_user_target
            and self.coordinator.data is not None
            and self.coordinator.data.get("setpoint") == self.user_setpoint
        ):
            self.restoring_user_target = False
            self._notify()

    def _reconcile(self) -> None:
        was_active = self.active
        should_control = self._can_take_over()
        if should_control and not self.active:
            data = self.coordinator.data
            if data is None or data.get("setpoint") is None:
                return
            if self.user_setpoint is None:
                self.user_setpoint = int(data["setpoint"])
                self._save(**{OPT_PV_USER_SETPOINT: self.user_setpoint})
            self.active = True
            self.restoring_user_target = False
            target = self.setpoint
            if target is None:
                target = int(data["setpoint"])
                self.setpoint = target
                self._save(**{OPT_PV_SETPOINT: target})
            self._submit_setpoint(target, pv=True)
            self._submit_heater(True)
        elif not should_control and self.active:
            self.active = False
            self._submit_heater(False)
            self._restore_user_setpoint()
        if self.active != was_active:
            self._notify()

    def _restore_user_setpoint(self) -> None:
        if self.user_setpoint is not None:
            self.restoring_user_target = True
            self._submit_setpoint(self.user_setpoint, pv=False)

    def _on_pv_failure(self) -> None:
        """Revoke permission after a failed command and restore user settings."""
        if not self.active:
            return
        self.released = False
        self.active = False
        self._submit_heater(False)
        self._restore_user_setpoint()
        self._notify()

    def _submit_setpoint(self, target: int, *, pv: bool) -> None:
        coordinator = self.coordinator

        def build(overrides: dict, data: dict | None) -> bytes | None:
            if self.active != pv or not coordinator.is_connected:
                return None
            if data is not None and data.get("setpoint") == overrides["setpoint"]:
                return None
            return coordinator.adapter.build_temp_command(overrides["setpoint"])

        coordinator.intent_queue.submit(
            group="setpoint",
            overrides={"setpoint": target},
            build_fn=build,
            on_failure=self._on_pv_failure if pv else None,
        )

    def _submit_heater(self, on: bool) -> None:
        coordinator = self.coordinator

        def build(overrides: dict, data: dict | None) -> bytes | None:
            if self.active != on or not coordinator.is_connected:
                return None
            if on and not self._can_take_over():
                return None
            if coordinator.adapter.is_heater_enabled(data) == on:
                return None
            return coordinator.adapter.build_heater_command(on=on)

        coordinator.intent_queue.submit(
            group="heater",
            overrides={"heater_enabled": on},
            build_fn=build,
            on_failure=self._on_pv_failure if on else None,
        )


class PVHeatingEnabledSwitch(JoyonwayCoordinatorEntity, SwitchEntity):
    """Master enable for PV heating."""

    _attr_has_entity_name = True
    _attr_translation_key = "pv_heating_enabled"
    _attr_icon = "mdi:solar-power"

    def __init__(
        self, coordinator: JoyonwayCoordinator, entry: JoyonwayConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_pv_heating_enabled"
        self._attr_device_info = device_info(entry)

    @property
    def is_on(self) -> bool:
        return self.coordinator.pv_heating.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.pv_heating.set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.pv_heating.set_enabled(False)


class PVHeatingReleaseSwitch(JoyonwayCoordinatorEntity, SwitchEntity):
    """External process grants or revokes PV surplus permission."""

    _attr_has_entity_name = True
    _attr_translation_key = "pv_heating_release"
    _attr_icon = "mdi:transmission-tower"

    def __init__(
        self, coordinator: JoyonwayCoordinator, entry: JoyonwayConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_pv_heating_release"
        self._attr_device_info = device_info(entry)

    @property
    def is_on(self) -> bool:
        return self.coordinator.pv_heating.released

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.pv_heating.set_release(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.pv_heating.set_release(False)


class PVHeatingActiveSensor(JoyonwayCoordinatorEntity, BinarySensorEntity):
    """Show whether PV control currently owns the spa heater."""

    _attr_has_entity_name = True
    _attr_translation_key = "pv_heating_active"
    _attr_icon = "mdi:solar-power-variant"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(
        self, coordinator: JoyonwayCoordinator, entry: JoyonwayConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_pv_heating_active"
        self._attr_device_info = device_info(entry)

    @property
    def is_on(self) -> bool:
        return self.coordinator.pv_heating.active


class PVHeatingSetpoint(JoyonwayCoordinatorEntity, NumberEntity):
    """Independent PV target temperature."""

    _attr_has_entity_name = True
    _attr_translation_key = "pv_heating_setpoint"
    _attr_icon = "mdi:thermometer-chevron-up"
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_step = 1

    def __init__(
        self, coordinator: JoyonwayCoordinator, entry: JoyonwayConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_pv_heating_setpoint"
        self._attr_device_info = device_info(entry)
        self._attr_native_min_value = coordinator.adapter.temp_min_c
        self._attr_native_max_value = coordinator.adapter.temp_max_c

    @property
    def native_value(self) -> float | None:
        value = self.coordinator.pv_heating.setpoint
        if value is not None:
            return value
        data = self.coordinator.data
        return data.get("setpoint") if data else None

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.pv_heating.set_setpoint(value)
