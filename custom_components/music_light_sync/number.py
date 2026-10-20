"""Latency offset: fine-tune when the lights fire relative to the speakers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.const import EntityCategory, UnitOfTime

from .const import USER_OFFSET_RANGE_MS
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
    """Add the number."""
    async_add_entities([LatencyOffset(entry.runtime_data, "latency_offset")])


class LatencyOffset(LightSyncEntity, RestoreNumber):
    """Positive values fire the lights earlier, negative later."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = -USER_OFFSET_RANGE_MS
    _attr_native_max_value = USER_OFFSET_RANGE_MS
    _attr_native_step = 10
    _attr_native_unit_of_measurement = UnitOfTime.MILLISECONDS

    async def async_added_to_hass(self) -> None:
        """Restore the last offset."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._controller.set_user_offset(float(last.native_value))

    @property
    def native_value(self) -> float:
        """Current offset."""
        return self._controller.user_offset_ms

    async def async_set_native_value(self, value: float) -> None:
        """Set the offset (applies immediately)."""
        self._controller.set_user_offset(value)
