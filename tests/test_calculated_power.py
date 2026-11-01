"""Tests for configurable estimated power calculation."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from homeassistant.const import CONF_HOST

from custom_components.joyonway.calculated_power import CalculatedPowerSensor
from custom_components.joyonway.const import (
    OPT_POWER_HEATING,
    OPT_POWER_LIGHT,
    OPT_POWER_OZONE,
    OPT_POWER_PUMP_HIGH,
    OPT_POWER_PUMP_LOW,
)


class FakeCoordinator:
    def __init__(self, data: dict | None) -> None:
        self.data = data
        self.available = True
        self.listener_updates = 0

    def async_update_listeners(self) -> None:
        self.listener_updates += 1


@pytest.fixture
def entry() -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="power_test",
        data={CONF_HOST: "spa.local"},
        options={},
    )


def test_calculated_power_uses_template_default_values(entry: SimpleNamespace) -> None:
    coordinator = FakeCoordinator(
        {
            "jets": "high",
            "jets_active": True,
            "circulation_active": True,
            "heater_active": True,
            "ozone_active": True,
            "light_active": True,
        }
    )
    sensor = CalculatedPowerSensor(coordinator, entry)

    assert sensor.native_value == 4905


@pytest.mark.parametrize(
    ("jets", "jets_active", "circulation", "expected_pump"),
    [
        ("low", True, False, 370),
        ("off", False, True, 370),
        ("off", False, False, 0),
        ("high", True, True, 2100),
    ],
)
def test_calculated_power_pump_selection(
    entry: SimpleNamespace,
    jets: str,
    jets_active: bool,
    circulation: bool,
    expected_pump: int,
) -> None:
    sensor = CalculatedPowerSensor(
        FakeCoordinator(
            {
                "jets": jets,
                "jets_active": jets_active,
                "circulation_active": circulation,
                "heater_active": False,
                "ozone_active": False,
                "light_active": False,
            }
        ),
        entry,
    )

    assert sensor.native_value == expected_pump


def test_calculated_power_uses_configured_component_values(
    entry: SimpleNamespace,
) -> None:
    entry.options = {
        OPT_POWER_PUMP_LOW: 500,
        OPT_POWER_PUMP_HIGH: 2500,
        OPT_POWER_HEATING: 3000,
        OPT_POWER_OZONE: 25,
        OPT_POWER_LIGHT: 10,
    }
    sensor = CalculatedPowerSensor(
        FakeCoordinator(
            {
                "jets": "low",
                "jets_active": True,
                "circulation_active": False,
                "heater_active": True,
                "ozone_active": True,
                "light_active": True,
            }
        ),
        entry,
    )

    assert sensor.native_value == 3535


def test_calculated_power_is_unavailable_without_coordinator_data(
    entry: SimpleNamespace,
) -> None:
    sensor = CalculatedPowerSensor(FakeCoordinator(None), entry)

    assert sensor.native_value is None
