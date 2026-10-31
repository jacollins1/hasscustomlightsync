"""The rooms' players in Music Assistant: shown once; the old Light sync groups removed."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from custom_components.music_light_sync.ma_groups import Room, RoomPlayers

LIVING, OFFICE, SUNROOM = "90568298A7D8", "905682433636", "90568241A436"
KITCHEN, DINING = "EzDwweg6C1289aBYgXle8EoXRDG9Gc3degC6AbYpK20", "dining-visualizer"


class FakeMusicAssistant:
    """Just enough of Music Assistant 2.10's player API (shapes captured from the house's server)."""

    def __init__(self) -> None:
        self.configs: dict[str, dict[str, Any]] = {}
        self.live: dict[str, dict[str, Any]] = {}
        self.providers = [{"instance_id": "bluesound", "domain": "bluesound", "available": True}]
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.listeners: list[Any] = []
        self._n = 0

    def speaker(self, pid: str, name: str, can_group_with: list[str], enabled: bool = True) -> None:
        self.configs[pid] = {
            "player_id": pid,
            "provider": "bluesound",
            "default_name": name,
            "enabled": enabled,
            "player_type": "player",
        }
        if enabled:
            self.live[pid] = {"player_id": pid, "can_group_with": can_group_with}

    def visualizer(self, pid: str, name: str) -> None:
        self.configs[pid] = {
            "player_id": pid,
            "provider": "sendspin",
            "default_name": name,
            "enabled": True,
            "player_type": "visualizer",
        }
        self.live[pid] = {"player_id": pid, "can_group_with": []}

    def group(self, pid: str, name: str, members: list[str]) -> None:
        self.configs[pid] = {
            "player_id": pid,
            "provider": "sync_group",
            "default_name": name,
            "enabled": True,
            "player_type": "group",
            "values": {"group_members": {"key": "group_members", "value": list(members)}},
        }

    def members(self, pid: str) -> list[str]:
        return self.configs[pid]["values"]["group_members"]["value"]

    def groups_by_name(self) -> dict[str, str]:
        return {
            c.get("name") or c["default_name"]: pid for pid, c in self.configs.items() if c["provider"] == "sync_group"
        }

    # The MusicAssistantApi protocol.

    async def command(self, command: str, **args: Any) -> Any:
        self.calls.append((command, args))
        if command == "config/players":
            return copy.deepcopy(list(self.configs.values()))
        if command == "players/all":
            return copy.deepcopy(list(self.live.values()))
        if command == "providers":
            return copy.deepcopy(self.providers)
        if command == "players/create_group_player":
            self._n += 1
            pid = f"syncgroup_new{self._n}"
            kept = [m for m in args["members"] if m in self.live]
            self.group(pid, args["name"], kept)
            return {"player_id": pid, "group_members": kept}
        if command == "config/players/save":
            config = self.configs[args["player_id"]]
            for key, value in args["values"].items():
                if key in ("enabled", "name"):
                    config[key] = value
                else:
                    config.setdefault("values", {})[key] = {"key": key, "value": value}
            return config
        if command == "players/remove_group_player":
            del self.configs[args["player_id"]]
            return None
        raise AssertionError(command)

    def subscribe(self, callback: Any) -> Any:
        self.listeners.append(callback)
        return lambda: self.listeners.remove(callback)

    def changes(self) -> list[str]:
        return [c for c, _ in self.calls if c not in ("config/players", "players/all", "providers")]


@pytest.fixture
def house() -> FakeMusicAssistant:
    ma = FakeMusicAssistant()
    ma.speaker(LIVING, "Living Room", [OFFICE, SUNROOM, KITCHEN, DINING])
    ma.speaker(SUNROOM, "Sunroom", [LIVING, OFFICE, KITCHEN, DINING])
    ma.speaker(OFFICE, "Office", [LIVING, SUNROOM])
    ma.visualizer(KITCHEN, "Music Sync - Kitchen Lights")
    ma.visualizer(DINING, "Music Sync - Dining Room")
    # What 0.3.3 left behind (the Living Room one was made by hand and adopted).
    ma.group("syncgroup_u8hz94fm", "Living Room - Light sync", [LIVING, KITCHEN, DINING])
    ma.group("syncgroup_wxilcei8", "Sunroom - Light sync", [SUNROOM, KITCHEN, DINING])
    ma.group("syncgroup_uachoch9", "Office - Light sync", [OFFICE])
    return ma


STORED_033 = {
    "groups": {LIVING: "syncgroup_u8hz94fm", SUNROOM: "syncgroup_wxilcei8", OFFICE: "syncgroup_uachoch9"},
    "rooms": [KITCHEN, DINING],
    "revealed": [KITCHEN],
}

_MANAGERS: list[RoomPlayers] = []


@pytest.fixture(autouse=True)
async def _stop_managers():
    yield
    for players in _MANAGERS:
        await players.async_stop()
    _MANAGERS.clear()


def manager(ma: FakeMusicAssistant, rooms: list[Room], stored: dict | None = None) -> tuple[RoomPlayers, dict]:
    saved: dict[str, Any] = {"value": stored}

    async def load() -> dict | None:
        return saved["value"]

    async def save(value: dict) -> None:
        saved["value"] = value

    players = RoomPlayers(lambda: ma, lambda: rooms, load, save)
    _MANAGERS.append(players)
    return players, saved


async def test_old_light_sync_groups_are_removed(house: FakeMusicAssistant) -> None:
    house.group("syncgroup_party", "Party", [LIVING, KITCHEN])  # someone's own group: kept
    house.group("syncgroup_other", "Garage - Light sync", [LIVING, "some-other-player"])  # not ours: kept
    players, saved = manager(house, [Room(KITCHEN), Room(DINING)], copy.deepcopy(STORED_033))
    await players.async_start()
    players._pending.cancel()  # noqa: SLF001 - run it now instead of after the debounce
    await players.async_reconcile()
    assert set(house.groups_by_name()) == {"Party", "Garage - Light sync"}
    assert sorted(players.removed) == ["Living Room - Light sync", "Office - Light sync", "Sunroom - Light sync"]
    assert saved["value"]["legacy_groups"] == []
    house.calls.clear()
    await players.async_reconcile()
    assert house.changes() == []  # nothing left to do


async def test_light_sync_groups_are_found_without_the_stored_record(house: FakeMusicAssistant) -> None:
    players, _ = manager(house, [Room(KITCHEN), Room(DINING)])
    await players.async_reconcile()
    assert house.groups_by_name() == {}  # named "- Light sync", only speakers and light rooms in them


async def test_room_players_are_shown_once(house: FakeMusicAssistant) -> None:
    house.configs[DINING]["values"] = {"hide_in_ui": {"key": "hide_in_ui", "value": True}}
    players, saved = manager(house, [Room(KITCHEN), Room(DINING)], {"legacy_groups": [], "revealed": [KITCHEN]})
    await players.async_start()
    players._pending.cancel()  # noqa: SLF001
    await players.async_reconcile()
    assert ("config/players/save", {"player_id": DINING, "values": {"hide_in_ui": False}}) in house.calls
    assert saved["value"]["revealed"] == sorted([KITCHEN, DINING])
    house.configs[DINING]["values"]["hide_in_ui"]["value"] = True  # hidden again by hand: left hidden
    house.calls.clear()
    await players.async_reconcile()
    assert ("config/players/save", {"player_id": DINING, "values": {"hide_in_ui": False}}) not in house.calls
