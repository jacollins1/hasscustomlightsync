"""House-wide state shared by every room (config entry).

* ``airtime``: one Zigbee budget for the whole house. All rooms talk through
  the same coordinator, so two rooms playing share 20 frames/s, not 40.
* ``directors``: the shared show for rooms that match each other while they
  play in the same Music Assistant group (keyed by Sendspin group id).
* ``groups``: housekeeping for the rooms' players in Music Assistant (shown
  once; the old "<speaker> - Light sync" groups removed).
* ``pins``: bulb positions from the floor plan, re-read when it is saved.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.core import Event, callback
from homeassistant.helpers.storage import Store

from .choreo import Director
from .const import (
    CONF_GROUP_MIN_INTERVAL,
    CONF_LAYOUT_SOURCE,
    CONF_LIGHTS,
    CONF_RATE_MAX,
    DEFAULT_GROUP_MIN_INTERVAL,
    DEFAULT_RATE_MAX,
    DOMAIN,
    LAYOUT_FLOOR_PLAN,
    LAYOUT_MANUAL,
    SHOW_HOUSE,
)
from .floorplan import FLOORPLAN_DASHBOARD, Pin, async_load_pins, device_ids, room_positions
from .governor import Airtime
from .ma_groups import ClientApi, MusicAssistantApi, Room, RoomPlayers

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DATA_HUB = f"{DOMAIN}_hub"
EVENT_LOVELACE_UPDATED = "lovelace_updated"


@dataclass
class Hub:
    """Shared by all rooms."""

    hass: HomeAssistant
    airtime: Airtime
    groups: RoomPlayers | None = None
    directors: dict[str, tuple[Director, set[int]]] = field(default_factory=dict)
    rooms: dict[str, Any] = field(default_factory=dict)  # entry_id -> controller
    _api: MusicAssistantApi | None = None
    _api_client: Any = None
    pins: dict[str, Pin] | None = None  # None: no floor plan dashboard
    _pins_loaded: bool = False
    _pins_unsub: Any = None

    # ------------------------------------------------------------ floor plan

    async def async_ensure_pins(self) -> None:
        """Read the floor plan once, and follow it from then on."""
        if self._pins_loaded:
            return
        self._pins_loaded = True
        self.pins = await async_load_pins(self.hass)
        self._pins_unsub = self.hass.bus.async_listen(EVENT_LOVELACE_UPDATED, self._on_dashboard_saved)

    @callback
    def _on_dashboard_saved(self, event: Event) -> None:
        if event.data.get("url_path") == FLOORPLAN_DASHBOARD:
            self.hass.async_create_task(self.async_reload_pins(), "floor plan positions")

    async def async_reload_pins(self) -> None:
        old = self.pins or {}
        self.pins = await async_load_pins(self.hass)
        new = self.pins or {}
        newly_pinned = {
            d for d, pin in new.items() if d not in old or (old[d].area != pin.area and pin.area is not None)
        }
        if newly_pinned:
            self._adopt_floor_plan(newly_pinned)
        for controller in list(self.rooms.values()):
            controller.pins_changed()
        for group in {c.show_group for c in self.rooms.values()} - {None}:
            self.refresh_canvas(group)

    def _adopt_floor_plan(self, newly_pinned: set[str]) -> None:
        """A light was pinned: rooms using it switch to the floor plan once all their bulbs are pinned."""
        from .zigbee import BulbResolveError, expand_lights  # noqa: PLC0415

        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.options.get(CONF_LAYOUT_SOURCE, LAYOUT_MANUAL) == LAYOUT_FLOOR_PLAN:
                continue
            try:
                lights = expand_lights(self.hass, list(entry.data.get(CONF_LIGHTS, [])))
            except BulbResolveError:
                continue
            if not lights or not (device_ids(self.hass, lights) & newly_pinned):
                continue
            positions, _missing = room_positions(self.hass, self.pins, lights)
            if positions is not None:
                _LOGGER.info("%s: every bulb is pinned on the floor plan; using its layout", entry.title)
                self.hass.config_entries.async_update_entry(
                    entry, options={**entry.options, CONF_LAYOUT_SOURCE: LAYOUT_FLOOR_PLAN}
                )

    # ------------------------------------------------------------ house-wide shows

    def refresh_canvas(self, group_id: str) -> None:
        """Rooms set to house-wide that play in this group draw one show across all their bulbs.

        Needs at least two such rooms with every bulb pinned on the floor plan;
        otherwise each runs the shared show on its own bulbs (mirrored).
        """
        rooms = sorted(
            (
                c
                for c in self.rooms.values()
                if c.show_group == group_id and c.show_mode == SHOW_HOUSE and c.engine is not None
            ),
            key=lambda c: c.entry.entry_id,
        )
        placed = [c for c in rooms if c.floor_positions is not None]
        if len(placed) < 2:
            for c in rooms:
                c.engine.choreo.use_canvas(None)
            return
        positions: list[tuple[float, float]] = []
        spans: dict[int, list[int]] = {}
        for c in placed:
            spans[id(c)] = list(range(len(positions), len(positions) + len(c.floor_positions)))
            positions += c.floor_positions
        key = ("house", group_id)
        for c in rooms:
            if id(c) in spans:
                c.engine.choreo.use_canvas(key, positions, spans[id(c)])
                c.engine.choreo.set_canvas_rooms(len(placed))
            else:
                c.engine.choreo.use_canvas(None)

    # ------------------------------------------------------------ rooms

    async def async_add_room(self, controller: Any) -> None:
        self.rooms[controller.entry.entry_id] = controller
        self.update_budget()
        if self.groups is None:
            store: Store[dict[str, Any]] = Store(self.hass, 1, f"{DOMAIN}.light_sync_groups")

            async def _save(mapping: dict[str, Any]) -> None:
                await store.async_save(mapping)

            self.groups = RoomPlayers(self.music_assistant, self.ma_rooms, store.async_load, _save)
            await self.groups.async_start()
        else:
            self.groups.request()

    async def async_remove_room(self, controller: Any) -> bool:
        """Forget a room; True when it was the last one."""
        self.rooms.pop(controller.entry.entry_id, None)
        if self.rooms:
            self.update_budget()
            if self.groups:
                self.groups.request()
            return False
        if self.groups:
            await self.groups.async_stop()
            self.groups = None
        if self._pins_unsub:
            self._pins_unsub()
            self._pins_unsub = None
        self._pins_loaded = False
        return True

    def update_budget(self) -> None:
        """The lowest frame budget set in any room caps the whole house."""
        opts = [{**c.entry.data, **c.entry.options} for c in self.rooms.values()]
        if not opts:
            return
        self.airtime.set_rate_max(min(float(o.get(CONF_RATE_MAX, DEFAULT_RATE_MAX)) for o in opts))
        self.airtime.group_min_interval_s = max(
            float(o.get(CONF_GROUP_MIN_INTERVAL, DEFAULT_GROUP_MIN_INTERVAL)) for o in opts
        )

    def ma_rooms(self) -> list[Room]:
        return [Room(c.link.client_id) for c in self.rooms.values() if c.link.client_id]

    # ------------------------------------------------------------ Music Assistant

    def music_assistant(self) -> MusicAssistantApi | None:
        """The Home Assistant Music Assistant integration's live connection."""
        for entry in self.hass.config_entries.async_loaded_entries("music_assistant"):
            mass = getattr(getattr(entry, "runtime_data", None), "mass", None)
            if mass is not None and hasattr(mass, "send_command"):
                if mass is not self._api_client:
                    self._api_client, self._api = mass, ClientApi(mass)
                return self._api
        return None

    def players(self) -> Any:
        """Music Assistant's live player models (``mass.players``), if connected."""
        if self.music_assistant() is None:
            return None
        return getattr(self._api_client, "players", None)

    # ------------------------------------------------------------ shared shows

    def acquire_director(self, group_id: str, room: int) -> Director:
        director, members = self.directors.get(group_id, (None, set()))
        if director is None:
            director = Director()
            self.directors[group_id] = (director, members)
        members.add(room)
        return director

    def release_director(self, group_id: str | None, room: int) -> None:
        if group_id is None or group_id not in self.directors:
            return
        _director, members = self.directors[group_id]
        members.discard(room)
        if not members:
            del self.directors[group_id]


def async_get_hub(hass: HomeAssistant) -> Hub:
    if (hub := hass.data.get(DATA_HUB)) is None:
        hub = hass.data[DATA_HUB] = Hub(hass, Airtime(rate_max=DEFAULT_RATE_MAX))
    return hub
