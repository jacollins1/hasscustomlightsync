"""Keeps the rooms' players in Music Assistant tidy.

* Music Assistant hides visualizer players by default: each room's player is
  shown once so it can be found and grouped (hide it again and it stays
  hidden).
* 0.3.1-0.3.3 kept a "<speaker> - Light sync" sync group per BluOS speaker.
  Rooms are now grouped with a speaker on the fly in Music Assistant instead,
  so those groups are removed: the ones this integration made or adopted
  (recorded in its storage), plus any other "<name> - Light sync" sync group
  whose members are only speakers and light rooms.

Talks to Music Assistant through the Home Assistant Music Assistant
integration's existing connection, so it needs no credentials of its own.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .const import LIGHT_SYNC_SUFFIX

_LOGGER = logging.getLogger(__name__)

SPEAKER_PROVIDER = "bluesound"
GROUP_PROVIDER = "sync_group"
EVENTS = ("player_added", "player_removed", "player_config_updated")
DEBOUNCE_S = 3.0
RESYNC_EVERY_S = 600.0


class MusicAssistantApi(Protocol):
    """The two things this module needs from a Music Assistant connection."""

    async def command(self, command: str, **args: Any) -> Any:
        """Send an API command and return its result."""

    def subscribe(self, callback: Callable[[str, str | None], None]) -> Callable[[], None]:
        """Call ``callback(event, object_id)`` for player added/removed/config events."""


@dataclass(frozen=True, slots=True)
class Room:
    """A light room, as Music Assistant sees it."""

    player_id: str  # our Sendspin visualizer player


def _domain(provider: str | None) -> str:
    return (provider or "").split("--", 1)[0]


def _value(config: dict[str, Any], key: str) -> Any:
    entry = (config.get("values") or {}).get(key)
    return entry.get("value") if isinstance(entry, dict) else entry


def _name(config: dict[str, Any]) -> str:
    return config.get("name") or config.get("default_name") or config["player_id"]


class RoomPlayers:
    """Shows each room's player once; removes the old Light sync groups."""

    def __init__(
        self,
        get_api: Callable[[], MusicAssistantApi | None],
        get_rooms: Callable[[], list[Room]],
        load: Callable[[], Awaitable[dict[str, Any] | None]],
        save: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self._get_api = get_api
        self._get_rooms = get_rooms
        self._load = load
        self._save = save
        self.legacy_groups: set[str] = set()  # Light sync group ids still to remove
        self.known_rooms: set[str] = set()  # every room player seen (a deleted room's too)
        self.revealed: set[str] = set()  # room players we un-hid once (a later hide is the user's call)
        self.removed: list[str] = []  # names of the groups removed, for the log and status
        self._api: MusicAssistantApi | None = None
        self._unsub: Callable[[], None] | None = None
        self._pending: asyncio.TimerHandle | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self.last_error: str | None = None

    async def async_start(self) -> None:
        stored = await self._load() or {}
        if "legacy_groups" in stored:
            self.legacy_groups = set(stored["legacy_groups"])
        else:  # 0.3.1-0.3.3: {"groups": {speaker: group}, ...} (or just the mapping)
            groups = stored.get("groups", {}) if "rooms" in stored or "groups" in stored else stored
            self.legacy_groups = {v for v in groups.values() if isinstance(v, str) and v}
        self.known_rooms = set(stored.get("rooms", []))
        self.revealed = set(stored.get("revealed", []))
        self.request()

    async def async_stop(self) -> None:
        if self._pending:
            self._pending.cancel()
            self._pending = None
        if self._unsub:
            self._unsub()
            self._unsub = None
        if self._task and not self._task.done():
            self._task.cancel()

    # ------------------------------------------------------------ triggers

    def request(self, delay: float = DEBOUNCE_S) -> None:
        """Check soon (debounced)."""
        loop = asyncio.get_running_loop()
        if self._pending:
            self._pending.cancel()
        self._pending = loop.call_later(delay, self._run)

    def _run(self) -> None:
        self._pending = None
        if self._task and not self._task.done():
            self.request()
            return
        self._task = asyncio.get_running_loop().create_task(self.async_reconcile())

    def _on_event(self, event: str, object_id: str | None) -> None:
        self.request()

    def _connect(self) -> MusicAssistantApi | None:
        api = self._get_api()
        if api is not self._api:
            if self._unsub:
                self._unsub()
                self._unsub = None
            self._api = api
            if api is not None:
                self._unsub = api.subscribe(self._on_event)
        return api

    # ------------------------------------------------------------ the work

    async def async_reconcile(self) -> None:
        async with self._lock:
            api = self._connect()
            if api is None:
                self.last_error = "Music Assistant integration not loaded"
                return
            try:
                await self._reconcile(api)
                self.last_error = None
            except Exception as err:  # noqa: BLE001 - try again on the next event or resync
                self.last_error = str(err) or type(err).__name__
                _LOGGER.warning("Could not update the rooms' Music Assistant players: %s", self.last_error)
            finally:
                if self._pending is None:
                    self._pending = asyncio.get_running_loop().call_later(RESYNC_EVERY_S, self._run)

    async def _reconcile(self, api: MusicAssistantApi) -> None:
        configs: list[dict[str, Any]] = await api.command("config/players", include_values=True)
        by_id = {c["player_id"]: c for c in configs}
        rooms = {r.player_id for r in self._get_rooms()}
        changed = not rooms <= self.known_rooms
        self.known_rooms |= rooms

        # Show each room's player once.
        for room_id in sorted(rooms & set(by_id)):
            if room_id in self.revealed:
                continue
            if _value(by_id[room_id], "hide_in_ui"):
                _LOGGER.info("Showing %s in Music Assistant", _name(by_id[room_id]))
                await api.command("config/players/save", player_id=room_id, values={"hide_in_ui": False})
            self.revealed.add(room_id)
            changed = True

        # Remove the Light sync groups.
        speakers = {c["player_id"] for c in configs if _domain(c.get("provider")) == SPEAKER_PROVIDER}
        lights = rooms | self.known_rooms
        for config in configs:
            if _domain(config.get("provider")) != GROUP_PROVIDER:
                continue
            group_id = config["player_id"]
            members = set(_value(config, "group_members") or [])
            ours = group_id in self.legacy_groups or (
                _name(config).endswith(LIGHT_SYNC_SUFFIX) and members <= speakers | lights
            )
            if not ours:
                continue
            _LOGGER.info("Removing %s from Music Assistant (rooms are grouped with speakers directly)", _name(config))
            await api.command("players/remove_group_player", player_id=group_id)
            self.removed.append(_name(config))
            self.legacy_groups.discard(group_id)
            changed = True
        gone = self.legacy_groups - set(by_id)
        if gone:
            self.legacy_groups -= gone
            changed = True

        if changed:
            await self._save(
                {
                    "legacy_groups": sorted(self.legacy_groups),
                    "rooms": sorted(self.known_rooms),
                    "revealed": sorted(self.revealed),
                }
            )


class ClientApi:
    """Adapter over the Home Assistant Music Assistant integration's client."""

    def __init__(self, mass: Any) -> None:
        self.mass = mass

    async def command(self, command: str, **args: Any) -> Any:
        return await self.mass.send_command(command, **args)

    def subscribe(self, callback: Callable[[str, str | None], None]) -> Callable[[], None]:
        try:
            from music_assistant_models.enums import EventType  # noqa: PLC0415
        except ImportError:
            return lambda: None
        events = tuple(EventType(e) for e in EVENTS)

        def _on_event(event: Any) -> None:
            kind = getattr(getattr(event, "event", None), "value", None) or str(getattr(event, "event", ""))
            callback(kind, getattr(event, "object_id", None))

        return self.mass.subscribe(_on_event, events)
