"""Party mode: varied, structured, budget-aware choreography."""

from __future__ import annotations

import itertools
from collections import Counter, defaultdict

from custom_components.music_light_sync.choreo import BeatInfo, Choreographer
from custom_components.music_light_sync.engine import EffectEngine, EngineSettings, Mode
from custom_components.music_light_sync.model import PRIO_BEAT
from custom_components.music_light_sync.palette import build_palette, hue_distance, hue_of

INTERVAL = 560_748  # 107 BPM, Jon's test track


def play(choreo: Choreographer, bars: int, loudness=lambda bar: -12.0):
    """Feed 4/4 bars; return (beat timestamps, commands)."""
    beats, out = [], []
    ts = 1_000_000
    for bar in range(bars):
        for pos in range(4):
            beats.append(ts)
            out.extend(choreo.on_beat(BeatInfo(ts, INTERVAL, pos, 4, pos == 0, pos == 3, loudness(bar))))
            ts += INTERVAL
    return beats, out


def test_looks_vary_and_bulbs_move_in_groups() -> None:
    choreo = Choreographer(5, 40, 254, seed=7)
    choreo.set_palette(build_palette([(220, 30, 60)]))
    kinds, sizes = [], set()
    ts = 1_000_000
    for bar in range(48):
        for pos in range(4):
            choreo.on_beat(BeatInfo(ts, INTERVAL, pos, 4, pos == 0, pos == 3, -12.0 + 6 * ((bar // 8) % 2)))
            ts += INTERVAL
        f = choreo.formation
        if not kinds or kinds[-1] != f.kind:
            kinds.append(f.kind)
        sizes.update(len(g) for g in f.groups)
    assert len(set(kinds)) >= 5, kinds  # many different looks over 48 bars
    assert all(a != b for a, b in itertools.pairwise(kinds))  # never the same look twice running
    assert {1, 2, 3, 5} <= sizes  # solos, pairs, threes and everyone together all happen


def test_group_members_share_color_and_hit_together() -> None:
    choreo = Choreographer(5, 40, 254, seed=3)
    _beats, out = play(choreo, 32)
    flashes = defaultdict(set)
    for c in out:
        if c.priority == PRIO_BEAT:
            flashes[c.effect_us].add(c.target)
    # Several bulbs hit on the same instant most of the time (not random scatter).
    multi = sum(1 for bulbs in flashes.values() if len(bulbs) >= 2)
    assert multi >= len(flashes) * 0.4
    # Bulbs of a group got the same color when the look started.
    colors = defaultdict(dict)
    for c in out:
        if c.rgb is not None:
            colors[c.effect_us][c.target] = c.rgb
    assert any(len(set(v.values())) < len(v) for v in colors.values())


def test_every_flash_has_a_fade_and_lands_on_the_grid() -> None:
    choreo = Choreographer(5, 40, 254, seed=11)
    beats, out = play(choreo, 24)
    grid = set(beats) | {b + INTERVAL // 2 for b in beats}
    flashes = [c for c in out if c.priority == PRIO_BEAT]
    assert flashes
    assert all(c.effect_us in grid and c.transition_ds == 0 for c in flashes)
    fades = {(c.target, c.effect_us) for c in out if c.level == 40 and c.transition_ds > 0}
    assert all((c.target, c.effect_us + 90_000) in fades for c in flashes)


def test_colors_cover_the_wheel_over_a_song() -> None:
    choreo = Choreographer(5, 40, 254, seed=5)
    choreo.set_palette(build_palette([(220, 30, 60), (200, 40, 120)]))  # a red/pink cover
    _, out = play(choreo, 64)
    hues = [hue_of(c.rgb) for c in out if c.rgb is not None]
    assert any(hue_distance(h, 0.97) > 0.4 for h in hues)  # blues/greens/cyans, far from red
    buckets = Counter(int(h * 6) for h in hues)  # six 60-degree sectors
    assert len(buckets) >= 5


def test_energy_drives_the_choice_and_a_drop_hits_everyone() -> None:
    choreo = Choreographer(5, 40, 254, seed=2)
    quiet_kinds = []
    ts = 1_000_000
    for bar in range(24):  # long quiet passage
        for pos in range(4):
            choreo.on_beat(BeatInfo(ts, INTERVAL, pos, 4, pos == 0, pos == 3, -20.0 if bar > 4 else -12.0))
            ts += INTERVAL
        if bar > 12:
            quiet_kinds.append(choreo.formation.kind)
    assert {"swell", "spotlight", "hold_pairs", "call_response"} >= set(quiet_kinds)
    # The drop: loud on a downbeat.
    out = choreo.on_beat(BeatInfo(ts, INTERVAL, 0, 4, True, False, -4.0))
    assert choreo.formation.kind == "unison"
    assert {c.target for c in out if c.priority == PRIO_BEAT} == set(range(5))


def test_party_mode_through_engine_fits_twenty_frames_per_second() -> None:
    engine = EffectEngine(EngineSettings(bulbs=5, min_level=40), Mode.PARTY, seed=1)
    engine.tracks_downbeats = True
    ts = 1_000_000
    beats = []
    for i in range(64):  # 16 bars at 107 BPM
        engine.add_beat(ts, i % 4 == 0)
        beats.append(ts)
        ts += INTERVAL
    for t in range(1_000_000, ts, 66_000):
        engine.add_loudness(t, 45_000)
    out = []
    now = 0
    while now <= beats[-1]:
        out.extend(engine.step(now, 80_000))
        now += 20_000
    seconds = (beats[-1] - beats[0]) / 1e6
    assert len(out) / seconds <= 20  # within the default budget before any coalescing
    assert len(out) / seconds >= 5  # and actually busy
