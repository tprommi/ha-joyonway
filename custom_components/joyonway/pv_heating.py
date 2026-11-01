"""Optional PV heating control with a separate user thermostat state."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import UnitOfTemperature, UnitOfTime
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory

from .entity import JoyonwayCoordinatorEntity, device_info

if TYPE_CHECKING:
    from .coordinator import JoyonwayConfigEntry, JoyonwayCoordinator


OPT_PV_ENABLED = "pv_heating_enabled"
OPT_PV_SETPOINT = "pv_heating_setpoint"
OPT_PV_USER_SETPOINT = "pv_user_setpoint"
OPT_PV_USER_ENABLED = "pv_user_heater_enabled"
OPT_PV_OWNER = "pv_heating_owned"
OPT_PV_ON_DELAY = "pv_heating_on_delay"
OPT_PV_OFF_DELAY = "pv_heating_off_delay"
DEFAULT_PV_DELAY = 60


class PVHeatingController:
    """Arbitrate heater and setpoint writes between the user and PV control."""

    def __init__(self, coordinator: JoyonwayCoordinator) -> None:
        self.coordinator = coordinator
        options = coordinator.entry.options
        self.enabled = bool(options.get(OPT_PV_ENABLED, False))
        self.released = False
        self.release_requested = False
        self._release_timer: asyncio.TimerHandle | None = None
        self.on_delay = int(options.get(OPT_PV_ON_DELAY, DEFAULT_PV_DELAY))
        self.off_delay = int(options.get(OPT_PV_OFF_DELAY, DEFAULT_PV_DELAY))
        self.active = False
        self.restoring_user_target = False
        self.setpoint = options.get(OPT_PV_SETPOINT)
        self.user_setpoint = options.get(OPT_PV_USER_SETPOINT)
        self.user_enabled = options.get(OPT_PV_USER_ENABLED)
        self._pv_owned_before_restart = options.get(OPT_PV_OWNER)
        self._startup_heater_check_pending = True
        self._last_heater_enabled: bool | None = None
        self._last_setpoint: int | None = None
        self._pv_heater_on_sent = False

    def _notify(self) -> None:
        self.coordinator.async_update_listeners()

    def shutdown(self) -> None:
        """Discard a pending release change when the entry unloads."""
        if self._release_timer is not None:
            self._release_timer.cancel()
            self._release_timer = None

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
        was_enabled = self.enabled
        was_active = self.active
        self.enabled = enabled
        self._save(**{OPT_PV_ENABLED: enabled})
        self._reconcile()
        if (
            was_enabled
            and not enabled
            and not was_active
            and self.user_enabled is False
            and self.coordinator.adapter.is_heater_enabled(self.coordinator.data)
            is True
        ):
            # After a restart PV ownership is lost, but the spa may still be armed.
            self._submit_heater(False)
        self._notify()

    def request_release(self, released: bool) -> None:
        if released == self.release_requested:
            return
        self.release_requested = released
        if self._release_timer is not None:
            self._release_timer.cancel()
            self._release_timer = None
        delay = self.on_delay if released else self.off_delay
        if delay:
            self._release_timer = asyncio.get_running_loop().call_later(
                delay, self.set_release, released
            )
        else:
            self.set_release(released)
        self._notify()

    def set_release(self, released: bool) -> None:
        self._release_timer = None
        self.released = released
        self._reconcile()
        self._notify()

    def set_delay(self, *, on: bool, value: float) -> None:
        seconds = round(value)
        if not 0 <= seconds <= 600:
            raise HomeAssistantError("PV heating delay must be between 0 and 600 seconds")
        if on:
            self.on_delay = seconds
            self._save(**{OPT_PV_ON_DELAY: seconds})
        else:
            self.off_delay = seconds
            self._save(**{OPT_PV_OFF_DELAY: seconds})
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
        self._save(**{OPT_PV_USER_ENABLED: True, OPT_PV_OWNER: False})
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
        data = self.coordinator.data
        if data is None:
            return
        heater_enabled = self.coordinator.adapter.is_heater_enabled(data)
        setpoint = data.get("setpoint")
        previous_heater = self._last_heater_enabled
        previous_setpoint = self._last_setpoint

        if (
            setpoint is not None
            and previous_setpoint is not None
            and setpoint != previous_setpoint
        ):
            if self.active and setpoint != self.setpoint:
                self.active = False
                self.restoring_user_target = False
                self.user_setpoint = setpoint
                self.user_enabled = True
                self._save(
                    **{
                        OPT_PV_USER_SETPOINT: setpoint,
                        OPT_PV_USER_ENABLED: True,
                        OPT_PV_OWNER: False,
                    }
                )
                self._notify()
            elif not self.active and not self.restoring_user_target:
                self.user_setpoint = setpoint
                self._save(**{OPT_PV_USER_SETPOINT: setpoint})

        if self._startup_heater_check_pending and heater_enabled is not None:
            self._startup_heater_check_pending = False
            if (
                self.user_enabled is False
                and heater_enabled
                and self._pv_owned_before_restart is not False
            ):
                self._submit_heater(False)
            elif heater_enabled and self._pv_owned_before_restart is False:
                self.user_enabled = True
                self._save(**{OPT_PV_USER_ENABLED: True})
                self._notify()
            elif not heater_enabled and self.user_enabled is True:
                self.user_enabled = False
                self._save(**{OPT_PV_USER_ENABLED: False})
                self._notify()
            if not heater_enabled and self._pv_owned_before_restart is not False:
                self._pv_owned_before_restart = False
                self._save(**{OPT_PV_OWNER: False})
        elif previous_heater is not None and heater_enabled != previous_heater:
            if heater_enabled:
                if not self._pv_heater_on_sent:
                    self.user_enabled = True
                    self._save(**{OPT_PV_USER_ENABLED: True, OPT_PV_OWNER: False})
                    if self.active:
                        self.active = False
                        self._restore_user_setpoint()
                    self._notify()
                self._pv_heater_on_sent = False
            else:
                self._pv_heater_on_sent = False
                if self._pv_owned_before_restart is not False:
                    self._pv_owned_before_restart = False
                    self._save(**{OPT_PV_OWNER: False})
                if self.user_enabled is True:
                    self.user_enabled = False
                    self._save(**{OPT_PV_USER_ENABLED: False})
                    self._notify()
                if self.active:
                    self.active = False
                    self._save(**{OPT_PV_OWNER: False})
                    self._notify()

        self._last_heater_enabled = heater_enabled
        self._last_setpoint = setpoint
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
            self._save(**{OPT_PV_OWNER: True})
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
            self._save(**{OPT_PV_OWNER: False})
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
        self.release_requested = False
        self.active = False
        self._save(**{OPT_PV_OWNER: False})
        self._pv_heater_on_sent = False
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
            if on:
                self._pv_heater_on_sent = True
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
    _attr_entity_category = EntityCategory.CONFIG
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
        return self.coordinator.pv_heating.release_requested

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.pv_heating.request_release(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.pv_heating.request_release(False)


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
    _attr_entity_category = EntityCategory.CONFIG
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


class PVHeatingDelay(JoyonwayCoordinatorEntity, NumberEntity):
    """Configurable delay before accepting or revoking PV release."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_native_min_value = 0
    _attr_native_max_value = 600
    _attr_native_step = 10
    _attr_icon = "mdi:timer-outline"

    def __init__(
        self, coordinator: JoyonwayCoordinator, entry: JoyonwayConfigEntry, *, on: bool
    ) -> None:
        super().__init__(coordinator)
        self._on = on
        key = "pv_heating_on_delay" if on else "pv_heating_off_delay"
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = device_info(entry)

    @property
    def native_value(self) -> float:
        pv = self.coordinator.pv_heating
        return pv.on_delay if self._on else pv.off_delay

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.pv_heating.set_delay(on=self._on, value=value)
