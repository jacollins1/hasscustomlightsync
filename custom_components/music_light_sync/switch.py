"""Sync switch: arms the light show."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF
from homeassistant.helpers.restore_state import RestoreEntity

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
    """Add the switch."""
    async_add_entities([SyncSwitch(entry.runtime_data, "sync")])


class SyncSwitch(LightSyncEntity, SwitchEntity, RestoreEntity):
    """When on, the lights follow the music whenever Music Assistant streams to them."""

    async def async_added_to_hass(self) -> None:
        """Restore the last armed state (on by default)."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        self._controller.armed = not (last is not None and last.state == STATE_OFF)

    @property
    def is_on(self) -> bool:
        """Armed."""
        return self._controller.armed

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Arm."""
        await self._controller.async_set_armed(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disarm (ends a running show and restores the lights)."""
        await self._controller.async_set_armed(False)
