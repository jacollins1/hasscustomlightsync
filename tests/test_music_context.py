"""The lights follow the speaker they are grouped with: volume and track timing."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.music_light_sync.music_context import music_context

KITCHEN, DINING = "EzDwweg6", "huE3OukO"
NODE, SUNROOM = "90568298A7D8", "90568241A436"
PARTY = "syncgroup_party"  # a group player someone made by hand


class Players(dict):
    def __iter__(self):
        return iter(self.values())


MEDIA = SimpleNamespace(
    uri="tidal://track/1", title="Strobe", duration=600, elapsed_time=500, elapsed_time_last_updated=1000.0
)


def _player(pid: str, **kw) -> SimpleNamespace:
    base = {
        "player_id": pid,
        "type": "player",
        "playback_state": "idle",
        "active_group": None,
        "synced_to": None,
        "group_members": [],
        "volume_level": None,
        "volume_muted": False,
        "group_volume": None,
        "group_volume_muted": False,
        "current_media": None,
        "elapsed_time": None,
        "elapsed_time_last_updated": None,
    }
    return SimpleNamespace(**{**base, **kw})


def _house() -> Players:
    players = Players()
    players[NODE] = _player(NODE, volume_level=6)
    players[SUNROOM] = _player(SUNROOM, volume_level=40)
    players[KITCHEN] = _player(KITCHEN, type="visualizer")
    players[DINING] = _player(DINING, type="visualizer")
    return players


def _group_on_the_fly(players: Players, leader: str, *members: str) -> None:
    """What Music Assistant shows after grouping players onto a leader."""
    lead = players[leader]
    lead.group_members = [leader, *members]
    lead.playback_state = "playing"
    lead.current_media = MEDIA
    for member in members:
        players[member].synced_to = leader


def test_kitchen_grouped_onto_the_living_room_follows_the_living_room() -> None:
    players = _house()
    _group_on_the_fly(players, NODE, KITCHEN)
    context = music_context(players, KITCHEN, room_ids=[DINING], now=lambda: 1010.0)
    assert context.group_id == NODE and context.speaker_id == NODE
    assert context.volume == 0.06
    assert context.remaining_s == 90.0  # 600 - (500 + 10 s since the update)
    assert context.track == ("tidal://track/1", "Strobe")
    # The dining room isn't grouped: no speaker, no timing.
    alone = music_context(players, DINING, room_ids=[KITCHEN])
    assert alone.speaker_id is None and alone.volume is None and alone.remaining_s is None


def test_speaker_grouped_onto_the_lights_still_counts() -> None:
    players = _house()
    _group_on_the_fly(players, KITCHEN, SUNROOM)  # grouped from the light player's side
    context = music_context(players, KITCHEN, room_ids=[DINING], now=lambda: 1000.0)
    assert context.speaker_id == SUNROOM and context.volume == 0.40
    assert context.remaining_s == 100.0


def test_a_hand_made_group_player_uses_its_speaker() -> None:
    players = _house()
    players[PARTY] = _player(
        PARTY,
        type="group",
        group_members=[KITCHEN, SUNROOM, DINING],
        playback_state="playing",
        current_media=MEDIA,
        group_volume=23,
    )
    players[KITCHEN].active_group = PARTY
    context = music_context(players, KITCHEN, room_ids=[DINING], now=lambda: 1000.0)
    assert context.group_id == PARTY
    assert context.speaker_id == SUNROOM  # first member that isn't a light room
    assert context.volume == 0.40


def test_muted_speaker_and_unknown_room() -> None:
    players = _house()
    _group_on_the_fly(players, NODE, KITCHEN)
    players[NODE].volume_muted = True
    assert music_context(players, KITCHEN).volume is None
    assert music_context(players, "not-a-player") is None
    assert music_context(None, KITCHEN) is None
