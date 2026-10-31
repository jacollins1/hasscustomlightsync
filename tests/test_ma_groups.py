"""'<speaker> - Light sync' players kept in step with Music Assistant's BluOS speakers."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from custom_components.music_light_sync.ma_groups import LightSyncGroups, Room

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
    ma.speaker(OFFICE, "Office", [LIVING, SUNROOM])  # can't group with light players
    ma.visualizer(KITCHEN, "Kitchen Lights")
    ma.group("syncgroup_u8hz94fm", "Living Room - Light sync", [LIVING, KITCHEN])  # made by hand earlier
    return ma


_MANAGERS: list[LightSyncGroups] = []


@pytest.fixture(autouse=True)
async def _stop_managers():
    yield
    for groups in _MANAGERS:
        await groups.async_stop()
    _MANAGERS.clear()


def manager(ma: FakeMusicAssistant, rooms: list[Room], stored: dict | None = None) -> tuple[LightSyncGroups, dict]:
    saved: dict[str, Any] = {"value": stored}

    async def load() -> dict | None:
        return saved["value"]

    async def save(value: dict) -> None:
        saved["value"] = value

    groups = LightSyncGroups(lambda: ma, lambda: rooms, load, save)
    _MANAGERS.append(groups)
    return groups, saved


async def test_one_group_per_speaker_existing_one_adopted(house: FakeMusicAssistant) -> None:
    groups, saved = manager(house, [Room(KITCHEN, None)])
    groups.mapping = {}
    await groups.async_reconcile()
    by_name = house.groups_by_name()
    assert set(by_name) == {"Living Room - Light sync", "Sunroom - Light sync", "Office - Light sync"}
    assert by_name["Living Room - Light sync"] == "syncgroup_u8hz94fm"  # adopted, not duplicated
    assert house.members(by_name["Sunroom - Light sync"]) == [SUNROOM, KITCHEN]
    assert house.members(by_name["Office - Light sync"]) == [OFFICE]  # lights can't group with it
    assert saved["value"]["groups"][LIVING] == "syncgroup_u8hz94fm"
    # Nothing to do the second time.
    house.calls.clear()
    await groups.async_reconcile()
    assert house.changes() == []


async def test_rooms_join_only_their_speakers_and_keep_hand_added_members(house: FakeMusicAssistant) -> None:
    house.visualizer(DINING, "Dining Room Lights")
    house.configs["syncgroup_u8hz94fm"]["values"]["group_members"]["value"] = [LIVING, KITCHEN, SUNROOM]
    groups, _ = manager(house, [Room(KITCHEN, frozenset({SUNROOM})), Room(DINING, None)])
    await groups.async_reconcile()
    living = house.groups_by_name()["Living Room - Light sync"]
    assert house.members(living) == [LIVING, SUNROOM, DINING]  # kitchen opted out; sunroom was added by hand
    sunroom = house.members(house.groups_by_name()["Sunroom - Light sync"])
    assert sunroom[0] == SUNROOM and set(sunroom) == {SUNROOM, DINING, KITCHEN}  # speaker leads


async def test_disable_and_enable_follow_the_speaker(house: FakeMusicAssistant) -> None:
    groups, _ = manager(house, [Room(KITCHEN, None)])
    await groups.async_reconcile()
    sunroom_group = house.groups_by_name()["Sunroom - Light sync"]
    house.configs[SUNROOM]["enabled"] = False
    del house.live[SUNROOM]
    await groups.async_reconcile()
    assert house.configs[sunroom_group]["enabled"] is False
    assert house.members(sunroom_group) == [SUNROOM, KITCHEN]  # membership kept while disabled
    house.configs[SUNROOM]["enabled"] = True
    house.live[SUNROOM] = {"player_id": SUNROOM, "can_group_with": [KITCHEN]}
    await groups.async_reconcile()
    assert house.configs[sunroom_group]["enabled"] is True


async def test_new_speaker_gets_a_group_and_a_removed_one_loses_it(house: FakeMusicAssistant) -> None:
    groups, _ = manager(house, [Room(KITCHEN, None)])
    await groups.async_reconcile()
    house.speaker("90568246FC38", "Fancy Room", [KITCHEN])
    await groups.async_reconcile()
    fancy = house.groups_by_name()["Fancy Room - Light sync"]
    assert house.members(fancy) == ["90568246FC38", KITCHEN]
    # Removed from Music Assistant (config gone, event received).
    del house.configs["90568246FC38"]
    del house.live["90568246FC38"]
    groups._on_event("player_removed", "90568246FC38")  # noqa: SLF001
    groups._pending.cancel()  # noqa: SLF001 - run it now instead of after the debounce
    await groups.async_reconcile()
    assert fancy not in house.configs
    assert "90568246FC38" not in groups.mapping


async def test_provider_restart_is_not_a_removal(house: FakeMusicAssistant) -> None:
    groups, _ = manager(house, [Room(KITCHEN, None)])
    await groups.async_reconcile()
    sunroom_group = house.groups_by_name()["Sunroom - Light sync"]
    # BluOS provider reloading: its configs are briefly not listed, and no removal event came.
    house.providers[0]["available"] = False
    saved = house.configs.pop(SUNROOM)
    await groups.async_reconcile()
    assert sunroom_group in house.configs
    house.configs[SUNROOM] = saved
    house.providers[0]["available"] = True


async def test_deleted_room_leaves_every_group(house: FakeMusicAssistant) -> None:
    rooms = [Room(KITCHEN, None)]
    groups, _ = manager(house, rooms)
    await groups.async_reconcile()
    rooms.clear()  # the Kitchen entry was deleted
    await groups.async_reconcile()
    assert house.members(house.groups_by_name()["Living Room - Light sync"]) == [LIVING]
    assert house.members(house.groups_by_name()["Sunroom - Light sync"]) == [SUNROOM]


async def test_renamed_speaker_renames_its_group(house: FakeMusicAssistant) -> None:
    groups, _ = manager(house, [Room(KITCHEN, None)])
    await groups.async_reconcile()
    house.configs[SUNROOM]["name"] = "Porch"
    await groups.async_reconcile()
    assert "Porch - Light sync" in house.groups_by_name()


async def test_room_players_are_shown_once(house: FakeMusicAssistant) -> None:
    house.visualizer(DINING, "Music Sync - Dining Room")
    house.configs[DINING]["values"] = {"hide_in_ui": {"key": "hide_in_ui", "value": True}}
    groups, saved = manager(house, [Room(KITCHEN, None), Room(DINING, None)])
    await groups.async_reconcile()
    assert ("config/players/save", {"player_id": DINING, "values": {"hide_in_ui": False}}) in house.calls
    assert saved["value"]["revealed"] == sorted([KITCHEN, DINING])
    # Hidden again by hand: left hidden.
    house.configs[DINING]["values"]["hide_in_ui"]["value"] = True
    house.calls.clear()
    await groups.async_reconcile()
    assert house.changes() == []
