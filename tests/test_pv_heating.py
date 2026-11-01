"""PV heating arbitration and independent thermostat state tests."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from homeassistant.components.climate import HVACMode
from homeassistant.const import CONF_HOST

from custom_components.joyonway.adapters.p25 import P25B85Adapter
from custom_components.joyonway.climate import SpaClimate
from custom_components.joyonway.pv_heating import PVHeatingController
from custom_components.joyonway.switch import SpaHeaterSwitch


class FakeIntentQueue:
    def __init__(self) -> None:
        self.commands: list[tuple[str, dict, object, object]] = []

    def submit(self, group, overrides, build_fn, on_failure=None) -> None:
        self.commands.append((group, overrides, build_fn, on_failure))


class FakeCoordinator:
    def __init__(self, *, heater_enabled: bool = False, mode: str = "manual") -> None:
        self.adapter = P25B85Adapter()
        self.data = {"heater_enabled": heater_enabled, "setpoint": 32}
        self.heater_mode = mode
        self.is_connected = True
        self.entry = SimpleNamespace(
            entry_id="entry_1", options={}, data={CONF_HOST: "spa.local"}
        )
        self.hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_update_entry=self._update_entry)
        )
        self.intent_queue = FakeIntentQueue()
        self.listener_updates = 0

    def _update_entry(self, entry, *, options) -> None:
        entry.options = options

    def async_update_listeners(self) -> None:
        self.listener_updates += 1


@pytest.mark.parametrize("heater_enabled", [False, True])
def test_pv_never_sends_on_startup(heater_enabled: bool) -> None:
    coordinator = FakeCoordinator(heater_enabled=heater_enabled)
    coordinator.entry.options = {"pv_heating_enabled": True}
    pv = PVHeatingController(coordinator)

    assert pv.enabled is True
    assert pv.released is False
    assert pv.active is False
    assert coordinator.intent_queue.commands == []


@pytest.mark.parametrize("user_enabled", [False, True, None])
def test_disabling_pv_after_restart_respects_user_thermostat(
    user_enabled: bool | None,
) -> None:
    coordinator = FakeCoordinator(heater_enabled=True)
    coordinator.entry.options = {
        "pv_heating_enabled": True,
        "pv_user_heater_enabled": user_enabled,
    }
    pv = PVHeatingController(coordinator)
    assert coordinator.intent_queue.commands == []

    pv.set_enabled(False)

    if user_enabled is False:
        assert [command[0] for command in coordinator.intent_queue.commands] == [
            "heater"
        ]
        assert coordinator.intent_queue.commands[0][2](
            {"heater_enabled": False}, coordinator.data
        ) == coordinator.adapter.build_heater_command(on=False)
    else:
        assert coordinator.intent_queue.commands == []


@pytest.mark.asyncio
async def test_pv_release_delays_and_cancels_pending_change() -> None:
    coordinator = FakeCoordinator()
    pv = PVHeatingController(coordinator)
    assert (pv.on_delay, pv.off_delay) == (60, 60)
    pv.set_enabled(True)
    pv.set_delay(on=True, value=1)
    pv.set_delay(on=False, value=1)

    pv.request_release(True)
    assert pv.release_requested is True
    assert pv.released is False
    assert not pv.active
    pv.request_release(False)
    await asyncio.sleep(0)
    assert pv.released is False
    assert coordinator.intent_queue.commands == []

    pv.request_release(True)
    await asyncio.sleep(1.05)
    assert pv.released is True
    assert pv.active
    pv.request_release(False)
    assert pv.released is True
    await asyncio.sleep(1.05)
    assert pv.released is False
    assert not pv.active


def test_pv_requires_all_conditions_and_keeps_user_target() -> None:
    coordinator = FakeCoordinator()
    pv = PVHeatingController(coordinator)
    pv.set_setpoint(38)
    pv.set_release(True)
    assert not pv.active
    assert coordinator.intent_queue.commands == []

    pv.set_enabled(True)
    assert pv.active
    assert pv.user_enabled is False
    assert pv.user_setpoint == 32
    assert [command[0] for command in coordinator.intent_queue.commands] == [
        "setpoint",
        "heater",
    ]
    assert coordinator.intent_queue.commands[0][2](
        {"setpoint": 38}, coordinator.data
    ) == coordinator.adapter.build_temp_command(38)
    assert coordinator.intent_queue.commands[1][2](
        {"heater_enabled": True}, coordinator.data
    ) == coordinator.adapter.build_heater_command(on=True)

    pv.set_user_setpoint(34)
    assert pv.user_setpoint == 34
    assert coordinator.entry.options["pv_user_setpoint"] == 34
    pv.set_release(False)
    assert not pv.active
    assert [command[0] for command in coordinator.intent_queue.commands[-2:]] == [
        "heater",
        "setpoint",
    ]
    assert coordinator.intent_queue.commands[-1][2](
        {"setpoint": 34}, {"setpoint": 38}
    ) == coordinator.adapter.build_temp_command(34)


@pytest.mark.parametrize(
    ("heater_enabled", "mode", "connected"),
    [(True, "manual", True), (False, "auto", True), (False, "manual", False)],
)
def test_pv_does_not_take_over_user_or_unavailable_spa(
    heater_enabled: bool, mode: str, connected: bool
) -> None:
    coordinator = FakeCoordinator(heater_enabled=heater_enabled, mode=mode)
    coordinator.is_connected = connected
    pv = PVHeatingController(coordinator)
    pv.set_setpoint(38)
    pv.set_enabled(True)
    pv.set_release(True)

    assert not pv.active
    assert coordinator.intent_queue.commands == []


def test_pv_waits_for_physical_heater_off_even_with_saved_user_off() -> None:
    coordinator = FakeCoordinator(heater_enabled=True)
    coordinator.entry.options = {"pv_user_heater_enabled": False}
    pv = PVHeatingController(coordinator)
    pv.set_enabled(True)
    pv.set_release(True)
    assert not pv.active

    coordinator.data["heater_enabled"] = False
    pv.on_broadcast()
    assert pv.active


def test_user_heat_request_cancels_pending_pv_write() -> None:
    coordinator = FakeCoordinator()
    pv = PVHeatingController(coordinator)
    pv.set_setpoint(38)
    pv.set_enabled(True)
    pv.set_release(True)
    pending_pv_heater = coordinator.intent_queue.commands[-1]

    pv.user_turn_on()
    assert pv.active is False
    assert pv.user_enabled is True
    assert pending_pv_heater[2]({"heater_enabled": True}, coordinator.data) is None
    assert coordinator.intent_queue.commands[-1][0] == "setpoint"


def test_pv_can_take_over_after_connection_returns() -> None:
    coordinator = FakeCoordinator()
    coordinator.is_connected = False
    pv = PVHeatingController(coordinator)
    pv.set_enabled(True)
    pv.set_release(True)
    assert not pv.active

    coordinator.is_connected = True
    pv.on_broadcast()
    assert pv.active


def test_pv_relinquishes_control_when_manual_mode_is_removed() -> None:
    coordinator = FakeCoordinator()
    pv = PVHeatingController(coordinator)
    pv.set_enabled(True)
    pv.set_release(True)
    assert pv.active

    coordinator.heater_mode = "auto"
    pv.on_broadcast()
    assert not pv.active
    assert coordinator.intent_queue.commands[-2][0] == "heater"
    assert coordinator.intent_queue.commands[-1][0] == "setpoint"


def test_failed_pv_command_releases_control() -> None:
    coordinator = FakeCoordinator()
    pv = PVHeatingController(coordinator)
    pv.set_setpoint(38)
    pv.set_enabled(True)
    pv.set_release(True)
    failure = coordinator.intent_queue.commands[-1][3]
    assert failure is not None

    failure()
    assert pv.active is False
    assert pv.released is False
    assert [command[0] for command in coordinator.intent_queue.commands[-2:]] == [
        "heater",
        "setpoint",
    ]


@pytest.mark.asyncio
async def test_user_thermostat_stays_off_and_keeps_its_target_during_pv() -> None:
    coordinator = FakeCoordinator()
    pv = PVHeatingController(coordinator)
    coordinator.pv_heating = pv
    climate = SpaClimate(coordinator, coordinator.entry)
    climate._arm_pending_hvac_timeout = lambda: None
    climate.async_write_ha_state = lambda: None

    pv.set_setpoint(38)
    pv.set_enabled(True)
    pv.set_release(True)
    coordinator.data = {"heater_enabled": True, "setpoint": 38, "status": "heating"}

    assert climate.hvac_mode == HVACMode.OFF
    assert climate.target_temperature == 32
    await climate.async_set_temperature(temperature=34)
    assert climate.target_temperature == 34
    assert pv.user_setpoint == 34
    assert coordinator.intent_queue.commands[-1][0] == "heater"

    await climate.async_set_hvac_mode(HVACMode.HEAT)
    assert pv.active is False
    assert pv.user_enabled is True
    assert climate.target_temperature == 34
    assert coordinator.intent_queue.commands[-2][0] == "setpoint"
    assert coordinator.intent_queue.commands[-1][0] == "heater"


@pytest.mark.asyncio
async def test_user_heater_switch_can_take_priority_over_pv() -> None:
    coordinator = FakeCoordinator()
    pv = PVHeatingController(coordinator)
    coordinator.pv_heating = pv
    pv.set_enabled(True)
    pv.set_release(True)
    coordinator.data = {"heater_enabled": True, "setpoint": 38}

    heater = SpaHeaterSwitch(coordinator, coordinator.entry)
    heater._arm_pending_timeout = lambda: None
    heater.async_write_ha_state = lambda: None
    assert heater.is_on is False

    await heater.async_turn_on()
    assert pv.active is False
    assert pv.user_enabled is True
    assert coordinator.intent_queue.commands[-1][0] == "heater"


@pytest.mark.asyncio
async def test_pv_waits_until_user_off_command_is_confirmed() -> None:
    coordinator = FakeCoordinator(heater_enabled=True)
    pv = PVHeatingController(coordinator)
    coordinator.pv_heating = pv
    climate = SpaClimate(coordinator, coordinator.entry)
    climate._arm_pending_hvac_timeout = lambda: None
    climate.async_write_ha_state = lambda: None

    pv.set_enabled(True)
    pv.set_release(True)
    assert not pv.active
    await climate.async_set_hvac_mode(HVACMode.OFF)
    assert not pv.active
    assert coordinator.intent_queue.commands[-1][0] == "heater"

    coordinator.data["heater_enabled"] = False
    pv.on_broadcast()
    assert pv.active
