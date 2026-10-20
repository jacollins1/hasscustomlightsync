"""Base entity for Music Light Sync."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, VERSION
from .controller import LightSyncController


class LightSyncEntity(Entity):
    """Entity bound to one controller (one config entry)."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, controller: LightSyncController, key: str) -> None:
        self._controller = controller
        self._attr_translation_key = key
        self._attr_unique_id = f"{controller.entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, controller.entry.entry_id)},
            name=controller.name,
            manufacturer="Music Light Sync",
            model="Sendspin visualizer",
            sw_version=VERSION,
        )

    async def async_added_to_hass(self) -> None:
        """Follow controller updates."""
        await super().async_added_to_hass()
        self.async_on_remove(self._controller.add_update_listener(self.async_write_ha_state))
