"""Music Light Sync: drive ZHA color bulbs from Music Assistant's music analysis."""

from __future__ import annotations

import contextlib
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .controller import LightSyncController
from .zigbee import BulbResolveError, BulbsNotReady

PLATFORMS = [Platform.SENSOR, Platform.SWITCH, Platform.SELECT, Platform.NUMBER]

type LightSyncConfigEntry = ConfigEntry[LightSyncController]


async def async_setup_entry(hass: HomeAssistant, entry: LightSyncConfigEntry) -> bool:
    """Set up from a config entry."""
    controller = LightSyncController(hass, entry)
    try:
        controller.prepare()
    except BulbsNotReady as err:
        raise ConfigEntryNotReady(str(err)) from err
    except BulbResolveError as err:
        raise ConfigEntryError(str(err)) from err
    entry.runtime_data = controller
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await controller.async_connect()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
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
