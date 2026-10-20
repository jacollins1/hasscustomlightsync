"""Status sensor with live diagnostics."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity

from .controller import Status
from .entity import LightSyncEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import LightSyncConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LightSyncConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensor."""
    async_add_entities([StatusSensor(entry.runtime_data, "status")])


class StatusSensor(LightSyncEntity, SensorEntity):
    """Connection/session state; attributes carry tempo and Zigbee stats."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [status.value for status in Status]
    # Counters change every few seconds during a show; keep them out of the database.
    _unrecorded_attributes = frozenset(
        {
            "bpm",
            "pulse_source",
            "frames_per_second",
            "rate_limit",
            "zigbee_rtt_ms",
            "lead_ms",
            "frames_sent",
            "frames_dropped_late",
            "frames_coalesced",
            "frame_errors",
            "palette",
            "time_synced",
            "last_error",
        }
    )

    @property
    def native_value(self) -> str:
        """Current status."""
        return self._controller.status.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Live diagnostics."""
        return self._controller.status_attributes()
