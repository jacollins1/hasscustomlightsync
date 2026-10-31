"""One "<speaker> - Light sync" player in Music Assistant for every BluOS speaker.

Each is a Music Assistant sync group: the BluOS speaker plus the light rooms
that opted in to that speaker (room option "Light sync speakers"). Playing to
it plays the speaker and runs the lights in those rooms.

The groups follow their speaker:

* a BluOS speaker added to Music Assistant gets its group;
* a speaker disabled (or enabled again) in Music Assistant takes its group
  with it;
* a speaker removed from Music Assistant has its group removed.

Groups that already exist with the right name ("Living Room - Light sync")
are adopted, not duplicated. Members someone added by hand (another speaker)
are left alone; only the light rooms this integration manages are added or
removed. A room is only added to a speaker Music Assistant says it can group
with.

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
    speakers: frozenset[str] | None  # BluOS player ids to join; None = all


def _domain(provider: str | None) -> str:
    return (provider or "").split("--", 1)[0]


def _value(config: dict[str, Any], key: str) -> Any:
    entry = (config.get("values") or {}).get(key)
    return entry.get("value") if isinstance(entry, dict) else entry


def _name(config: dict[str, Any]) -> str:
    return config.get("name") or config.get("default_name") or config["player_id"]


def group_name(speaker_name: str) -> str:
    """ "Living Room" -> "Living Room - Light sync"."""
    return f"{speaker_name}{LIGHT_SYNC_SUFFIX}"


class LightSyncGroups:
    """Keeps a Light sync group next to every BluOS speaker in Music Assistant."""

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
        self.mapping: dict[str, str] = {}  # BluOS player id -> its group's player id
        self.known_rooms: set[str] = set()  # every room player we ever added (to remove it later)
        self.revealed: set[str] = set()  # room players we un-hid once (a later hide is the user's call)
        self._api: MusicAssistantApi | None = None
        self._unsub: Callable[[], None] | None = None
        self._pending: asyncio.TimerHandle | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._removed: set[str] = set()
        self.last_error: str | None = None
        self.speakers: dict[str, str] = {}  # BluOS player id -> name, as last seen

    async def async_start(self) -> None:
        stored = await self._load() or {}
        if "groups" in stored:
            self.mapping = dict(stored["groups"])
            self.known_rooms = set(stored.get("rooms", []))
            self.revealed = set(stored.get("revealed", []))
        else:  # 0.3.1 pre-release format: just the mapping
            self.mapping = {k: v for k, v in stored.items() if isinstance(v, str)}
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
        """Reconcile soon (debounced)."""
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
        if event == "player_removed" and object_id:
            self._removed.add(object_id)
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
        """Bring Music Assistant's Light sync groups in line with its speakers."""
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
                _LOGGER.warning("Could not update Music Assistant Light sync groups: %s", self.last_error)
            finally:
                loop = asyncio.get_running_loop()
                if self._pending is None:
                    self._pending = loop.call_later(RESYNC_EVERY_S, self._run)

    async def _reconcile(self, api: MusicAssistantApi) -> None:  # noqa: C901, PLR0912
        configs: list[dict[str, Any]] = await api.command("config/players", include_values=True)
        players: list[dict[str, Any]] = await api.command("players/all")
        providers: list[dict[str, Any]] = await api.command("providers")
        speaker_provider_up = any(
            _domain(p.get("domain") or p.get("instance_id")) == SPEAKER_PROVIDER and p.get("available", True)
            for p in providers
        )
        by_id = {c["player_id"]: c for c in configs}
        live = {p["player_id"]: p for p in players}
        speakers = [
            c
            for c in configs
            if _domain(c.get("provider")) == SPEAKER_PROVIDER and c.get("player_type", "player") == "player"
        ]
        groups = {c["player_id"]: c for c in configs if _domain(c.get("provider")) == GROUP_PROVIDER}
        all_rooms = self._get_rooms()
        rooms = [r for r in all_rooms if r.player_id in by_id]
        current = {r.player_id for r in all_rooms}
        managed = current | self.known_rooms  # a deleted room is taken out of every group
        changed = not current <= self.known_rooms
        self.known_rooms |= current
        self.speakers = {c["player_id"]: _name(c) for c in speakers}

        # Music Assistant hides visualizer players by default. Show each room's
        # player once so it can be found, played to and grouped by hand.
        for room in rooms:
            if room.player_id in self.revealed:
                continue
            if _value(by_id[room.player_id], "hide_in_ui"):
                _LOGGER.info("Showing %s in Music Assistant", _name(by_id[room.player_id]))
                await api.command("config/players/save", player_id=room.player_id, values={"hide_in_ui": False})
            self.revealed.add(room.player_id)
            changed = True

        # Speakers gone from Music Assistant: remove their groups. Only when the
        # speaker provider is up (a provider restart is not a removal), or when
        # Music Assistant told us the player was removed.
        for speaker_id, group_id in list(self.mapping.items()):
            if speaker_id in by_id:
                continue
            if not speaker_provider_up and speaker_id not in self._removed:
                continue
            if group_id in groups:
                _LOGGER.info("Removing %s: its speaker was removed from Music Assistant", _name(groups[group_id]))
                await api.command("players/remove_group_player", player_id=group_id)
                groups.pop(group_id, None)
            del self.mapping[speaker_id]
            changed = True
        self._removed.clear()

        taken = set(self.mapping.values())
        for speaker in speakers:
            speaker_id = speaker["player_id"]
            want_name = group_name(_name(speaker))
            group_id = self.mapping.get(speaker_id)
            if group_id not in groups:
                group_id = next(
                    (
                        gid
                        for gid, g in groups.items()
                        if gid not in taken and _name(g).casefold() == want_name.casefold()
                    ),
                    None,
                )
                if group_id is not None:
                    self.mapping[speaker_id] = group_id
                    taken.add(group_id)
                    changed = True

            allowed = set((live.get(speaker_id) or {}).get("can_group_with") or [])
            joining = [
                r.player_id
                for r in rooms
                if (r.speakers is None or speaker_id in r.speakers)
                and (r.player_id in allowed or speaker_id not in live)
            ]

            if group_id is None:
                created = await api.command(
                    "players/create_group_player",
                    provider=GROUP_PROVIDER,
                    name=want_name,
                    members=[speaker_id, *joining],
                    dynamic=False,
                )
                group_id = created["player_id"]
                _LOGGER.info("Created %s in Music Assistant", want_name)
                self.mapping[speaker_id] = group_id
                taken.add(group_id)
                changed = True
                # Music Assistant drops members that are offline when the group is
                # created; compare against what it actually kept.
                kept = list(created.get("group_members") or [])
                group: dict[str, Any] = {
                    "enabled": True,
                    "name": want_name,
                    "values": {"group_members": {"value": kept}},
                }
            else:
                group = groups[group_id]

            update: dict[str, Any] = {}
            current = list(_value(group, "group_members") or [])
            if speaker_id not in live:
                # Speaker offline or disabled: keep whichever of our rooms are there.
                joining = [m for m in current if m in managed]
            desired = [speaker_id, *[m for m in current if m not in managed and m != speaker_id], *sorted(joining)]
            if desired != current:
                update["group_members"] = desired
            enabled = bool(speaker.get("enabled", True))
            if bool(group.get("enabled", True)) != enabled:
                update["enabled"] = enabled
            name = _name(group)
            if name != want_name and name.endswith(LIGHT_SYNC_SUFFIX):
                update["name"] = want_name  # the speaker was renamed
            if update:
                _LOGGER.debug("Updating %s: %s", want_name, update)
                await api.command("config/players/save", player_id=group_id, values=update)

        if changed:
            await self._save(
                {"groups": dict(self.mapping), "rooms": sorted(self.known_rooms), "revealed": sorted(self.revealed)}
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
