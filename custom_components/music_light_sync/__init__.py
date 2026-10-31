"""Music Light Sync: drive ZHA color bulbs from Music Assistant's music analysis.

One config entry per room. Rooms share the house's Zigbee budget, can share
one show when they play together, and each joins the "<speaker> - Light sync"
players in Music Assistant it opted in to.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import (
    CONF_AREA,
    CONF_FALLBACK_PLAYER,
    CONF_LAYOUT_SOURCE,
    CONF_LIGHTS,
    CONF_MATCH_ROOMS,
    CONF_PAUSE_ENTITIES,
    CONF_SHOW_MODE,
    DOMAIN,
    LAYOUT_FLOOR_PLAN,
    LAYOUT_MANUAL,
    SHOW_INDEPENDENT,
    SHOW_MIRRORED,
)
from .controller import LightSyncController
from .hub import async_get_hub
from .zigbee import BulbResolveError, BulbsNotReady, room_area_of

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.SWITCH, Platform.SELECT, Platform.NUMBER]

type LightSyncConfigEntry = ConfigEntry[LightSyncController]


async def async_setup_entry(hass: HomeAssistant, entry: LightSyncConfigEntry) -> bool:
    """Set up one room."""
    await async_get_hub(hass).async_ensure_pins()
    controller = LightSyncController(hass, entry)
    try:
        controller.prepare()
    except BulbsNotReady as err:
        raise ConfigEntryNotReady(str(err)) from err
    except BulbResolveError as err:
        raise ConfigEntryError(str(err)) from err
    if CONF_LAYOUT_SOURCE not in entry.options:
        # First setup after the upgrade: the floor plan when every bulb is pinned.
        controller.layout_source = LAYOUT_FLOOR_PLAN if controller.floor_positions is not None else LAYOUT_MANUAL
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_LAYOUT_SOURCE: controller.layout_source}
        )
        controller.pins_changed()
    entry.runtime_data = controller
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await controller.async_connect()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    entry.async_on_unload(hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _rename_follower(hass, entry)))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: LightSyncConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: LightSyncConfigEntry) -> bool:
    """Unload: stop the show and restore the lights."""
    await entry.runtime_data.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: LightSyncConfigEntry) -> None:
    """Delete the stored Sendspin pairing state and any hand-back record."""
    path = Path(hass.config.path(".storage", f"{DOMAIN}.{entry.entry_id}.sendspin.json"))
    with contextlib.suppress(FileNotFoundError):
        await hass.async_add_executor_job(path.unlink)
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.handback").async_remove()


# ---------------------------------------------------------------- renames


def rename_in_config(
    data: dict[str, Any], options: dict[str, Any], old: str, new: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Replace an entity id in the room's stored config; None for an unchanged part."""
    new_data = None
    if old in data.get(CONF_LIGHTS, []):
        new_data = {**data, CONF_LIGHTS: [new if e == old else e for e in data[CONF_LIGHTS]]}
    new_options = None
    if old in options.get(CONF_PAUSE_ENTITIES, []):
        new_options = dict(options)
        new_options[CONF_PAUSE_ENTITIES] = [new if e == old else e for e in options.get(CONF_PAUSE_ENTITIES, [])]
    return new_data, new_options


def _rename_follower(hass: HomeAssistant, entry: ConfigEntry) -> Any:
    """Keep the stored entity ids right when an entity is renamed."""

    @callback
    def _on_registry_update(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        data = event.data
        if data.get("action") != "update" or "old_entity_id" not in data:
            return
        old, new = data["old_entity_id"], data["entity_id"]
        new_data, new_options = rename_in_config(dict(entry.data), dict(entry.options), old, new)
        if new_data is None and new_options is None:
            return
        _LOGGER.info("%s: following rename %s -> %s", entry.title, old, new)
        hass.config_entries.async_update_entry(
            entry,
            data=new_data if new_data is not None else entry.data,
            options=new_options if new_options is not None else entry.options,
        )  # the update listener reloads the room

    return _on_registry_update


# ---------------------------------------------------------------- migration


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """1 -> 2: entries became rooms (record the area).
    2 -> 3: "match other rooms" became the show mode, the music player setting
    went (the room follows the speaker it plays with), and the layout source
    starts on the floor plan when every bulb is pinned there."""
    if entry.version > 3:
        return False
    if entry.version == 1:
        area = room_area_of(hass, list(entry.data.get(CONF_LIGHTS, [])))
        if area is not None and ar.async_get(hass).async_get_area(area) is None:
            area = None
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_AREA: area}, version=2)
        _LOGGER.info("Migrated %s to a room (area %s)", entry.title, area)
    if entry.version == 2:
        # The layout source is decided at the first setup, once the bulbs resolve.
        options = dict(entry.options)
        match = options.pop(CONF_MATCH_ROOMS, True)
        options.pop(CONF_FALLBACK_PLAYER, None)
        options.setdefault(CONF_SHOW_MODE, SHOW_MIRRORED if match else SHOW_INDEPENDENT)
        hass.config_entries.async_update_entry(entry, options=options, version=3)
        _LOGGER.info("Migrated %s: show %s", entry.title, options[CONF_SHOW_MODE])
    return True
