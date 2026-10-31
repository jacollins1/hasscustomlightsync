"""Bulb positions from the house floor plan.

The floor plan dashboard (ha-floorplan card with its device layer) keeps the
positions people drag devices to in a hidden dashboard, ``floorplan-positions``,
under ``floorplan_device_positions``: ``{device_id: {"area", "x", "y"}}``, in
feet, one coordinate frame for the whole house. It is read through Home
Assistant's dashboard storage, never from ``.storage`` directly.

A pin belongs to the area the device was in when it was placed; once the
device moves to another area the floor plan stops using that pin, so it isn't
used here either. Unpinned devices are drawn wherever the dashboard puts them
at render time; those spots aren't stored and aren't real positions.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

FLOORPLAN_DASHBOARD = "floorplan-positions"
POSITIONS_KEY = "floorplan_device_positions"


@dataclass(frozen=True, slots=True)
class Pin:
    """Where a device was placed on the floor plan (feet)."""

    x: float
    y: float
    area: str | None


def parse_pins(config: dict[str, Any] | None) -> dict[str, Pin]:
    """device_id -> Pin from the positions dashboard's config."""
    raw = (config or {}).get(POSITIONS_KEY)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    if not isinstance(raw, dict):
        return {}
    pins: dict[str, Pin] = {}
    for device_id, value in raw.items():
        if not isinstance(value, dict):
            continue
        try:
            pins[str(device_id)] = Pin(float(value["x"]), float(value["y"]), value.get("area"))
        except KeyError, TypeError, ValueError:
            continue
    return pins


async def async_load_pins(hass: HomeAssistant) -> dict[str, Pin] | None:
    """The floor plan's pins; None when there is no floor plan dashboard."""
    try:
        from homeassistant.components.lovelace.const import LOVELACE_DATA  # noqa: PLC0415
    except ImportError:
        return None
    data = hass.data.get(LOVELACE_DATA)
    dashboard = data.dashboards.get(FLOORPLAN_DASHBOARD) if data is not None else None
    if dashboard is None:
        return None
    try:
        config = await dashboard.async_load(False)
    except Exception as err:  # noqa: BLE001 - ConfigNotFound and friends: no plan saved yet
        _LOGGER.debug("Floor plan positions not readable: %s", err)
        return None
    return parse_pins(config)


def pinned_position(hass: HomeAssistant, pins: dict[str, Pin], entity_id: str) -> tuple[float, float] | None:
    """A light's floor plan position, or None if its device isn't (validly) pinned."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None or entry.device_id is None or (pin := pins.get(entry.device_id)) is None:
        return None
    device = dr.async_get(hass).async_get(entry.device_id)
    area = device.area_id if device is not None else None
    if pin.area and area and pin.area != area:
        return None  # placed in another room; the floor plan dropped it too
    return (pin.x, pin.y)


def room_positions(
    hass: HomeAssistant, pins: dict[str, Pin] | None, entity_ids: list[str]
) -> tuple[list[tuple[float, float]] | None, list[str]]:
    """Positions of a room's bulbs (in order), or None plus the unpinned bulbs."""
    if pins is None:
        return None, list(entity_ids)
    positions: list[tuple[float, float]] = []
    missing: list[str] = []
    for entity_id in entity_ids:
        if (position := pinned_position(hass, pins, entity_id)) is None:
            missing.append(entity_id)
        else:
            positions.append(position)
    return (None, missing) if missing else (positions, [])


def device_ids(hass: HomeAssistant, entity_ids: list[str]) -> set[str]:
    registry = er.async_get(hass)
    return {e.device_id for i in entity_ids if (e := registry.async_get(i)) is not None and e.device_id}
