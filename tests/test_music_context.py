"""The lights follow the speaker they play with: volume and track timing."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.music_light_sync.music_context import music_context

KITCHEN, DINING = "EzDwweg6", "huE3OukO"
NODE, SUNROOM = "90568298A7D8", "90568241A436"
LIVING_SYNC, SUNROOM_SYNC = "syncgroup_u8hz94fm", "syncgroup_sun"


class Players(dict):
    def __iter__(self):
        return iter(self.values())


def _player(pid: str, **kw) -> SimpleNamespace:
    base = {
        "player_id": pid,
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


def _house(playing_group: str, room_active_group: str | None) -> Players:
    media = SimpleNamespace(
        uri="tidal://track/1", title="Strobe", duration=600, elapsed_time=500, elapsed_time_last_updated=1000.0
    )
    players = Players()
    players[NODE] = _player(NODE, volume_level=6)
    players[SUNROOM] = _player(SUNROOM, volume_level=40, volume_muted=False)
    players[KITCHEN] = _player(KITCHEN, active_group=room_active_group)
    players[DINING] = _player(DINING)
    for gid, speaker in ((LIVING_SYNC, NODE), (SUNROOM_SYNC, SUNROOM)):
        players[gid] = _player(
            gid,
            group_members=[speaker, KITCHEN, DINING],
            playback_state="playing" if gid == playing_group else "idle",
            current_media=media if gid == playing_group else None,
            group_volume=23,
        )
    return players


def test_follows_the_speaker_of_the_group_it_plays_in() -> None:
    players = _house(SUNROOM_SYNC, SUNROOM_SYNC)
    speaker_for = {LIVING_SYNC: NODE, SUNROOM_SYNC: SUNROOM}
    context = music_context(players, KITCHEN, room_ids=[DINING], speaker_for=speaker_for, now=lambda: 1010.0)
    assert context.group_id == SUNROOM_SYNC and context.speaker_id == SUNROOM
    assert context.volume == 0.40  # the Sunroom speaker, not the Living Room one
    assert context.remaining_s == 90.0  # 600 - (500 + 10 s since the update)
    assert context.track == ("tidal://track/1", "Strobe")


def test_finds_the_playing_group_without_active_group_and_skips_light_players() -> None:
    players = _house(LIVING_SYNC, None)
    context = music_context(players, KITCHEN, room_ids=[DINING], now=lambda: 1000.0)
    assert context.group_id == LIVING_SYNC
    assert context.speaker_id == NODE  # first member with a volume that isn't a light room
    assert context.volume == 0.06


def test_muted_speaker_and_unknown_room() -> None:
    players = _house(LIVING_SYNC, LIVING_SYNC)
    players[NODE].volume_muted = True
    assert music_context(players, KITCHEN, speaker_for={LIVING_SYNC: NODE}).volume is None
    assert music_context(players, "not-a-player") is None
    assert music_context(None, KITCHEN) is None


def test_not_grouped_has_no_speaker() -> None:
    players = _house("none", None)
    context = music_context(players, KITCHEN)
    assert context.group_id is None and context.speaker_id is None and context.volume is None
    assert context.remaining_s is None and context.track is None
