"""Diagnostics download."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data

from .const import CONF_IDENTITY

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import LightSyncConfigEntry


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: LightSyncConfigEntry) -> dict[str, Any]:
    """Return config, bulbs and live state."""
    controller = entry.runtime_data
    return {
        "data": async_redact_data(dict(entry.data), {CONF_IDENTITY}),
        "options": dict(entry.options),
        "bulbs": [{"entity_id": b.entity_id, "ieee": b.ieee, "endpoint": b.endpoint} for b in controller.bulbs],
        "armed": controller.armed,
        "status": controller.status.value,
        "session": controller.session.value if controller.session else None,
        "attributes": controller.status_attributes(),
    }
