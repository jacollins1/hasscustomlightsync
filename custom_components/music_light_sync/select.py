"""Effect mode select."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity
from homeassistant.helpers.restore_state import RestoreEntity

from .engine import Mode
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
    """Add the select."""
    async_add_entities([EffectSelect(entry.runtime_data, "effect")])


class EffectSelect(LightSyncEntity, SelectEntity, RestoreEntity):
    """Which effect the bulbs run."""

    _attr_options = [mode.value for mode in Mode]

    async def async_added_to_hass(self) -> None:
        """Restore the last effect."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in self._attr_options:
            self._controller.set_mode(Mode(last.state))

    @property
    def current_option(self) -> str:
        """Active effect."""
        return self._controller.mode.value

    async def async_select_option(self, option: str) -> None:
        """Change effect (takes effect on the next beat)."""
        self._controller.set_mode(Mode(option))
