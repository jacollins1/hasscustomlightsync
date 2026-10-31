"""House-wide state shared by every room (config entry).

* ``airtime``: one Zigbee budget for the whole house. All rooms talk through
  the same coordinator, so two rooms playing share 20 frames/s, not 40.
* ``directors``: the shared show for rooms that match each other while they
  play in the same Music Assistant group (keyed by Sendspin group id).
* ``groups``: the "<speaker> - Light sync" players in Music Assistant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store

from .choreo import Director
from .const import (
    CONF_GROUP_MIN_INTERVAL,
    CONF_RATE_MAX,
    CONF_SPEAKERS,
    DEFAULT_GROUP_MIN_INTERVAL,
    DEFAULT_RATE_MAX,
    DOMAIN,
)
from .governor import Airtime
from .ma_groups import ClientApi, LightSyncGroups, MusicAssistantApi, Room

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

DATA_HUB = f"{DOMAIN}_hub"


@dataclass
class Hub:
    """Shared by all rooms."""

    hass: HomeAssistant
    airtime: Airtime
    groups: LightSyncGroups | None = None
    directors: dict[str, tuple[Director, set[int]]] = field(default_factory=dict)
    rooms: dict[str, Any] = field(default_factory=dict)  # entry_id -> controller
    _api: MusicAssistantApi | None = None
    _api_client: Any = None

    # ------------------------------------------------------------ rooms

    async def async_add_room(self, controller: Any) -> None:
        self.rooms[controller.entry.entry_id] = controller
        self.update_budget()
        if self.groups is None:
            store: Store[dict[str, Any]] = Store(self.hass, 1, f"{DOMAIN}.light_sync_groups")

            async def _save(mapping: dict[str, Any]) -> None:
                await store.async_save(mapping)

            self.groups = LightSyncGroups(self.music_assistant, self.ma_rooms, store.async_load, _save)
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
        rooms = []
        for controller in self.rooms.values():
            speakers = controller.entry.options.get(CONF_SPEAKERS)
            rooms.append(Room(controller.link.client_id, None if speakers is None else frozenset(speakers)))
        return rooms

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
