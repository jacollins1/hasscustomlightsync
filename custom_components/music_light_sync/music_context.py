"""What the room is playing: the speaker's volume and where the song is.

The lights get beats, loudness, spectrum and album colors over Sendspin, but
not the speaker's volume or the position in the track. Those come from Music
Assistant's own player model (through the Home Assistant Music Assistant
integration's live connection, no polling):

* the room's light player is grouped with a speaker (``synced_to`` /
  ``active_group``, the light player leading a group, or a group that lists it
  and is playing);
* the speaker is the player leading that group (Living Room grouped with
  Music Sync - Kitchen Lights), or for a group player the first member that
  has a volume and isn't a light room;
* volume comes from the speaker, the track and its timing from the group.

So grouping the kitchen lights with the Sunroom makes them follow the Sunroom
speaker, with no setting to keep in step.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

PLAYING = "playing"


class PlayerLookup(Protocol):
    """Music Assistant client's players controller (``mass.players``)."""

    def get(self, player_id: str) -> Any: ...

    def __iter__(self) -> Any: ...


@dataclass(frozen=True, slots=True)
class MusicContext:
    """What the show needs from the music, beyond the Sendspin features."""

    group_id: str | None
    speaker_id: str | None
    volume: float | None  # 0..1, None = unknown or muted
    remaining_s: float | None  # time left in the track while playing
    track: tuple[Any, Any] | None  # (uri, title), changes with the song


def _state(player: Any) -> str:
    state = getattr(player, "playback_state", None)
    return str(getattr(state, "value", state) or "")


def _group_of(players: PlayerLookup, room_id: str) -> Any:
    me = players.get(room_id)
    if me is None:
        return None
    for group_id in (getattr(me, "active_group", None), getattr(me, "synced_to", None)):
        if group_id and (group := players.get(group_id)) is not None:
            return group
    if [m for m in getattr(me, "group_members", None) or [] if m != room_id]:
        return me  # the light player leads a group (the speaker was grouped onto it)
    playing = [
        p
        for p in players
        if p is not me and room_id in (getattr(p, "group_members", None) or []) and _state(p) == PLAYING
    ]
    return playing[0] if playing else None


def _is_group_player(player: Any) -> bool:
    kind = getattr(player, "type", None)
    return str(getattr(kind, "value", kind) or "") == "group"


def _speaker_of(players: PlayerLookup, group: Any, room_ids: set[str]) -> Any:
    # Grouped on the fly (Living Room + Kitchen Lights): the leading speaker is the speaker.
    if (
        not _is_group_player(group)
        and group.player_id not in room_ids
        and getattr(group, "volume_level", None) is not None
    ):
        return group
    for member_id in getattr(group, "group_members", None) or []:
        if member_id in room_ids or member_id == group.player_id:
            continue
        member = players.get(member_id)
        if member is not None and getattr(member, "volume_level", None) is not None:
            return member
    return None


def music_context(
    players: PlayerLookup | None,
    room_id: str | None,
    *,
    room_ids: Iterable[str] = (),
    now: Callable[[], float] = time.time,
) -> MusicContext | None:
    """The room's music context, or None when Music Assistant doesn't know the room."""
    if players is None or not room_id:
        return None
    group = _group_of(players, room_id)
    source = group if group is not None else players.get(room_id)
    if source is None:
        return None
    speaker = None
    if group is not None:
        speaker = _speaker_of(players, group, {room_id, *room_ids})

    volume = None
    if speaker is not None and getattr(speaker, "volume_level", None) is not None:
        if not getattr(speaker, "volume_muted", False):
            volume = float(speaker.volume_level) / 100
    elif (
        group is not None
        and (group_volume := getattr(group, "group_volume", None)) is not None
        and not getattr(group, "group_volume_muted", False)
    ):
        volume = float(group_volume) / 100

    media = getattr(source, "current_media", None)
    remaining = None
    track = None
    if media is not None:
        track = (getattr(media, "uri", None), getattr(media, "title", None))
        duration = getattr(media, "duration", None)
        elapsed = getattr(media, "elapsed_time", None)
        if elapsed is None:
            elapsed = getattr(source, "elapsed_time", None)
        updated = getattr(media, "elapsed_time_last_updated", None) or getattr(
            source, "elapsed_time_last_updated", None
        )
        if duration and elapsed is not None and _state(source) == PLAYING:
            position = float(elapsed) + (max(0.0, now() - float(updated)) if updated else 0.0)
            remaining = float(duration) - position
    return MusicContext(
        group_id=getattr(group, "player_id", None),
        speaker_id=getattr(speaker, "player_id", None),
        volume=volume,
        remaining_s=remaining,
        track=track if track and any(track) else None,
    )
