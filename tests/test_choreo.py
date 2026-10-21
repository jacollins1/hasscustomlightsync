"""Party mode (the lighting designer) against the project's lighting rules.

Every test drives a whole simulated song through the real engine, with beats
and loudness arriving ahead of time the way Music Assistant streams them.
"""

from __future__ import annotations

import itertools
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import pytest

from custom_components.music_light_sync.choreo import (
    AMBER,
    BUILD_TONES,
    WARM_WHITE,
    WHITE,
    Layout,
    Look,
    Section,
    default_layout,
)
from custom_components.music_light_sync.controller import parse_layout
from custom_components.music_light_sync.engine import DB_UNIT, EffectEngine, EngineSettings, Mode
from custom_components.music_light_sync.model import GROUP, PRIO_BEAT, Command

# The kitchen: 1 top center, 2 left upper, 3 right middle, 4 left lower, 5 right bottom.
KITCHEN = [(0.43, 0.13), (0.22, 0.36), (0.62, 0.48), (0.23, 0.65), (0.63, 0.81)]

# (loudness dB or "ramp", bars, label): a typical club track.
SONG = [
    (-18, 8, "intro"),
    (-12, 8, "verse"),
    ("ramp", 8, "build"),
    (-4, 8, "drop"),
    (-16, 4, "break"),
    ("ramp", 8, "build"),
    (-4, 8, "drop"),
    (-10, 8, "outro"),
]
TICK_US = 20_000
LEAD_US = 100_000


@dataclass
class Show:
    beats: list[int]
    bar_starts: list[int]
    labels: list[str]  # per bar
    commands: list[Command]
    looks: list[tuple[Look, Section, int]] = field(default_factory=list)
    sections: list[tuple[int, Section]] = field(default_factory=list)  # (time, section) at each change

    @property
    def attacks(self) -> list[Command]:
        return [c for c in self.commands if c.priority == PRIO_BEAT and c.level is not None]

    def bar_of(self, ts: int) -> int:
        return max(i for i, b in enumerate(self.bar_starts) if b <= ts)


def run_song(
    bpm: float = 107,
    *,
    volume: float | None = 0.45,
    volume_at=None,
    intensity: float = 1.0,
    group: bool = False,
    seed: int = 1,
    budget: float = 20.0,
    ahead_s: float = 1.0,
    layout=KITCHEN,
    bulbs: int = 5,
) -> Show:
    interval = int(60_000_000 / bpm)
    engine = EffectEngine(
        EngineSettings(bulbs=bulbs, min_level=10, max_level=254, layout=layout, has_group=group), Mode.PARTY, seed=seed
    )
    engine.tracks_downbeats = True
    engine.budget_per_s = budget
    engine.intensity = intensity
    feed: list[tuple[int, str, object]] = []
    beats, bar_starts, labels = [], [], []
    ts = 2_000_000
    for loud, bars, label in SONG:
        for bar in range(bars):
            db = (-12 + 5 * bar / (bars - 1)) if loud == "ramp" else loud
            bar_starts.append(ts)
            labels.append(label)
            for pos in range(4):
                feed.append((ts, "beat", pos == 0))
                feed.extend((ts + k, "loud", int((db + 60) * DB_UNIT)) for k in range(0, interval, 66_000))
                beats.append(ts)
                ts += interval
    feed.sort(key=lambda item: item[0])
    show = Show(beats, bar_starts, labels, [])
    outro_start = bar_starts[-8]
    now = 0
    while now <= beats[-1] + interval:
        while feed and feed[0][0] <= now + ahead_s * 1e6:
            t, kind, value = feed.pop(0)
            if kind == "beat":
                engine.add_beat(t, value)
            else:
                engine.add_loudness(t, value)
        choreo = engine.choreo
        choreo.volume = volume_at(now) if volume_at else volume
        choreo.track_remaining_s = (beats[-1] - now) / 1e6 if now >= outro_start - 1_000_000 else 200
        show.commands.extend(engine.step(now, LEAD_US))
        if choreo.look is not None and (not show.looks or show.looks[-1][0] is not choreo.look):
            show.looks.append((choreo.look, choreo.section, now))
        if not show.sections or show.sections[-1][1] is not choreo.section:
            show.sections.append((now, choreo.section))
        now += TICK_US
    return show


@pytest.fixture(scope="module")
def show() -> Show:
    return run_song()


# --------------------------------------------------------------- layout


def test_kitchen_layout_groupings() -> None:
    layout = Layout.build(KITCHEN)
    sets = lambda grouping: {frozenset(g) for g in grouping}  # noqa: E731
    # Pairs follow the room: left column (2, 4), right column (3, 5), top (1) alone = 2 + 2 + 1.
    assert sets(layout.groupings["pairs"][0]) == {frozenset({1, 3}), frozenset({2, 4}), frozenset({0})}
    k2 = [sets(g) for g in layout.groupings["k2"]]
    assert {frozenset({1, 3}), frozenset({0, 2, 4})} in k2  # left vs right
    assert {frozenset({0, 1}), frozenset({2, 3, 4})} in k2  # top vs bottom
    assert {(1, 4), (2, 3)} <= {tuple(sorted(map(len, g))) for g in layout.groupings["k2"]}
    assert (1, 2, 2) in {tuple(sorted(map(len, g))) for g in layout.groupings["k3"]}
    assert layout.groupings["solo"] and layout.groupings["all"]


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 6, 8, 12])
def test_any_room_size_partitions_every_bulb(count: int) -> None:
    layout = Layout.build(default_layout(count))
    for kind, options in layout.groupings.items():
        for grouping in options:
            members = sorted(b for group in grouping for b in group)
            assert members == list(range(count)), (kind, grouping)
    for order in layout.orders:
        assert sorted(order) == list(range(count))


def test_parse_layout() -> None:
    assert parse_layout("43, 13\n22, 36\n62, 48\n23, 65\n63, 81", 5) == [
        (0.43, 0.13),
        (0.22, 0.36),
        (0.62, 0.48),
        (0.23, 0.65),
        (0.63, 0.81),
    ]
    assert parse_layout("1: 10, 20; 2: 30 40", 2) == [(0.1, 0.2), (0.3, 0.4)]
    assert parse_layout("", 5) is None
    with pytest.raises(ValueError):
        parse_layout("10, 20", 2)
    with pytest.raises(ValueError):
        parse_layout("10, 20, 30\n1, 2", 2)


@pytest.mark.parametrize("bulbs", [1, 3, 8])
def test_other_rooms_run(bulbs: int) -> None:
    show = run_song(layout=None, bulbs=bulbs)
    assert show.attacks
    assert {c.target for c in show.commands} <= set(range(bulbs))


# --------------------------------------------------------------- song story


def test_song_structure(show: Show) -> None:
    order = [s for _, s in show.sections]
    assert order[0] is Section.WARMUP
    assert order[-1] is Section.OUTRO
    assert order.count(Section.DROP) == 2
    for drop in (i for i, s in enumerate(order) if s is Section.DROP):
        assert order[drop - 1] is Section.BUILD  # the drop releases a build
        assert order[drop + 1] is Section.PEAK
    assert Section.BREAKDOWN in order
    # Each drop is recognised on the bar the music drops (one bar of grace).
    drop_bars = [i for i, label in enumerate(show.labels) if label == "drop" and show.labels[i - 1] == "build"]
    seen = [show.bar_of(t + LEAD_US + 300_000) for t, s in show.sections if s is Section.DROP]
    for expected, got in zip(drop_bars, seen, strict=True):
        assert expected <= got <= expected + 1


def test_build_is_one_cool_tone_drop_is_white_outro_is_warm(show: Show) -> None:
    for look, section, _ in show.looks:
        if section is Section.BUILD:
            assert len(set(look.colors)) == 1 and look.colors[0] in BUILD_TONES
        if section is Section.DROP:
            assert look.colors == [WHITE] and look.rhythm == "impact"
        if section is Section.OUTRO:
            assert set(look.colors) <= {AMBER, WARM_WHITE}
    # Outro moves slowly: bar-rate looks only.
    assert all(look.rate == "bar" for look, s, _ in show.looks if s is Section.OUTRO)


def test_two_color_rule_and_white_reserved(show: Show) -> None:
    for look, section, _ in show.looks:
        colors = set(look.colors) - {WHITE}
        assert len(colors) <= 2, (section, look.colors)
        if section is not Section.DROP:
            assert WHITE not in look.colors
    drop_times = [t for t, s in show.sections if s is Section.DROP]
    whites = [c.effect_us for c in show.commands if c.rgb == WHITE]
    assert whites
    # White is sent only around the drop (it is pre-sent just before the hit).
    for w in whites:
        assert any(-500_000 <= w - (t + LEAD_US) <= 3_000_000 for t in drop_times)


def test_colors_change_between_sections(show: Show) -> None:
    pairs = [frozenset(look.colors) for look, s, _ in show.looks if s not in (Section.DROP, Section.OUTRO)]
    assert len(set(pairs)) >= 4  # the song travels across the wheel, one pair at a time


# --------------------------------------------------------------- variety


def test_variety_of_looks_and_groupings(show: Show) -> None:
    rhythms = {look.rhythm for look, _, _ in show.looks}
    assert len(rhythms) >= 9, rhythms
    shapes = {tuple(sorted(len(g) for g in look.groups)) for look, _, _ in show.looks}
    shapes |= {tuple(sorted(len(g) for g in look.groups)) for look, _, _ in run_song(seed=2).looks}
    assert (5,) in shapes
    assert (1, 1, 1, 1, 1) in shapes
    assert {(1, 4), (2, 3)} & shapes
    assert (1, 2, 2) in shapes
    assert any(len(s) == 2 for s in shapes) and any(len(s) == 3 for s in shapes)


def test_never_the_same_look_twice_running(show: Show) -> None:
    for (a, sa, _), (b, sb, _) in itertools.pairwise(show.looks):
        if sa is sb and sa is not Section.DROP:
            assert a.rhythm != b.rhythm or a.groups != b.groups


# --------------------------------------------------------------- safety and tempo


@pytest.mark.parametrize("bpm", [90, 107, 128, 140, 174])
def test_strobe_safety_and_grid(bpm: int) -> None:
    show = run_song(bpm)
    interval = int(60_000_000 / bpm)
    grid = set(show.beats) | {b + interval // 2 for b in show.beats}
    attacks = show.attacks
    assert attacks
    assert all(c.effect_us in grid for c in attacks)  # on the beat or the eighth, never between
    times = sorted({c.effect_us for c in attacks})
    assert min(b - a for a, b in itertools.pairwise(times)) >= 100_000  # room <= 10 Hz
    per_bulb = defaultdict(list)
    for c in attacks:
        for bulb in range(5) if c.target == GROUP else [c.target]:
            per_bulb[bulb].append(c.effect_us)
    for hits in per_bulb.values():
        hits.sort()
        assert all(b - a >= 125_000 for a, b in itertools.pairwise(hits))  # a bulb <= 8 Hz
    per_second = max(sum(1 for t in times if s <= t < s + 1_000_000) for s in times)
    assert per_second <= 10  # never in the 15-20 Hz photosensitive range


@pytest.mark.parametrize("bpm", [107, 174])
def test_frame_budget(bpm: int) -> None:
    show = run_song(bpm)
    seconds = (show.beats[-1] - show.beats[0]) / 1e6
    assert len(show.commands) / seconds <= 12
    per_second = Counter(c.effect_us // 1_000_000 for c in show.commands)
    assert max(per_second.values()) <= 20 + 12  # budget + the governor's burst


def test_drop_colors_go_out_before_the_hit(show: Show) -> None:
    for t, section in show.sections:
        if section is not Section.DROP:
            continue
        window = [c for c in show.commands if t <= c.effect_us <= t + 2_000_000]
        first_hit = min(c.effect_us for c in window if c.priority == PRIO_BEAT and c.level is not None)
        whites = [c.effect_us for c in window if c.rgb == WHITE]
        assert whites and max(whites) < first_hit


def test_zha_group_turns_whole_room_hits_into_one_frame() -> None:
    plain, grouped = run_song(128), run_song(128, group=True)
    group_frames = [c for c in grouped.commands if c.target == GROUP]
    assert group_frames
    assert len(grouped.commands) < len(plain.commands)
    times = sorted(c.effect_us for c in group_frames)
    assert all(b - a >= 2_000_000 for a, b in itertools.pairwise(times))  # broadcast table safety


# --------------------------------------------------------------- brightness at low volume


def _swing(show: Show) -> tuple[int, int, int]:
    """(highest attack level, instant attacks, largest single-step jump)."""
    attacks = show.attacks
    instant = sum(1 for c in attacks if c.transition_ds == 0)
    last: dict[int, int] = {}
    jump = 0
    for c in sorted((c for c in show.commands if c.level is not None), key=lambda c: c.effect_us):
        if c.transition_ds == 0 and c.target in last:
            jump = max(jump, abs(c.level - last[c.target]))
        last[c.target] = c.level
    return max(c.level for c in attacks), instant, jump


def test_full_volume_has_contrast() -> None:
    top, instant, _ = _swing(run_song(volume=0.45))
    assert top >= 240 and instant > 0


def test_turned_down_music_is_gentle() -> None:
    # Party volume for the first minute, then turned way down.
    show = run_song(volume_at=lambda now: 0.45 if now < 60_000_000 else 0.08)
    later = Show(show.beats, show.bar_starts, show.labels, [c for c in show.commands if c.effect_us > 70_000_000])
    top, instant, _ = _swing(later)
    assert instant == 0  # every rise takes at least 0.3 s
    assert top <= 0.45 * 254


def test_volume_is_relative_to_how_loud_they_usually_play() -> None:
    # A Node at 4 % into a loud amp is party volume: full contrast.
    top, instant, _ = _swing(run_song(volume=0.04))
    assert top >= 240 and instant > 0


def test_intensity_control() -> None:
    top, instant, jump = _swing(run_song(intensity=0.4))
    assert instant == 0 and jump == 0
    assert top <= 0.45 * 254
