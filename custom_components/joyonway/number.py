"""Number platform for the independent PV heating target."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import JoyonwayConfigEntry
from .pv_heating import PVHeatingSetpoint


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JoyonwayConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Expose the PV heating setpoint."""
    async_add_entities([PVHeatingSetpoint(entry.runtime_data, entry)])
