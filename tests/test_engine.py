"""Effect engine behaviour: timing, Zigbee budget, mode semantics."""

from __future__ import annotations

import itertools

from custom_components.music_light_sync.engine import (
    GROUP,
    PRIO_BEAT,
    Command,
    EffectEngine,
    EngineSettings,
    Mode,
)

BEAT = 500_000  # 120 BPM
LEAD = 80_000


def run(engine: EffectEngine, start: int, end: int, tick: int = 20_000, lead: int = LEAD) -> list[tuple[int, Command]]:
    """Step the engine like the controller does; return (send_time, command)."""
    out: list[tuple[int, Command]] = []
    now = start
    while now <= end:
        out.extend((now, cmd) for cmd in engine.step(now, lead))
        now += tick
    return out


def feed_beats(engine: EffectEngine, first: int, count: int, interval: int = BEAT, bar: int = 4) -> list[int]:
    stamps = [first + i * interval for i in range(count)]
    for i, ts in enumerate(stamps):
        engine.add_beat(ts, i % bar == 0)
    return stamps


def test_breathe_flashes_on_beats_one_and_three() -> None:
    engine = EffectEngine(EngineSettings(bulbs=5, min_level=40, max_level=254), Mode.BREATHE)
    engine.budget_per_s = 20  # 5 frames/pulse x 2 beats/s = 10 <= 0.75 x 20: pulse every beat
    engine.tracks_downbeats = True
    beats = feed_beats(engine, 1_000_000, 16)
    sent = run(engine, 0, beats[-1])
    levels = [(t, c) for t, c in sent if c.level is not None and c.target == 0]
    # One level frame per beat for bulb 0, sent `lead` before the beat.
    assert [c.effect_us for _, c in levels] == beats
    assert all(LEAD - 20_000 < c.effect_us - t <= LEAD for t, c in levels)
    for i, (_, cmd) in enumerate(levels):
        if i % 4 in (0, 2):  # beats 1 and 3: instant attack to full
            assert cmd.level > 40 and cmd.transition_ds == 0, f"beat {i} should flash"
        else:  # beats 2 and 4: release over most of the beat
            assert cmd.level == 40 and cmd.transition_ds == 4, f"beat {i} should release"
    # Colors only change on flashes.
    color_times = {c.effect_us for _, c in sent if c.rgb is not None}
    assert color_times <= {ts for i, ts in enumerate(beats) if i % 4 in (0, 2)}


def test_breathe_halves_pulse_rate_when_budget_is_tight() -> None:
    engine = EffectEngine(EngineSettings(bulbs=5), Mode.BREATHE)
    engine.budget_per_s = 12  # 10 level frames/s would exceed 75 % of 12
    engine.tracks_downbeats = True
    beats = feed_beats(engine, 1_000_000, 16)
    sent = run(engine, 0, beats[-1])
    frames = [c for _, c in sent]
    seconds = (beats[-1] - beats[0]) / 1e6
    assert len(frames) / seconds <= 0.75 * 12 + 1
    bulb0 = [c for c in frames if c.level is not None and c.target == 0]
    assert [c.effect_us for c in bulb0] == beats[::2]  # beats 1 and 3
    assert [c.transition_ds for c in bulb0[:2]] == [0, 9]  # flash, then release over 2 beats


def test_breathe_fast_tempo_pulses_per_bar_and_still_alternates() -> None:
    engine = EffectEngine(EngineSettings(bulbs=5, min_level=40), Mode.BREATHE)
    engine.budget_per_s = 7  # 5 frames x 3.3 pulses/s: divisor 4, one pulse per bar
    engine.tracks_downbeats = True
    beats = feed_beats(engine, 1_000_000, 32, interval=300_000)  # 200 BPM
    sent = run(engine, 0, beats[-1])
    bulb0 = [c for _, c in sent if c.level is not None and c.target == 0]
    assert [c.effect_us for c in bulb0] == beats[::4]
    assert [c.transition_ds == 0 for c in bulb0] == [i % 2 == 0 for i in range(len(bulb0))]


def test_stale_beats_are_skipped_not_sent_late() -> None:
    engine = EffectEngine(EngineSettings(bulbs=2), Mode.BREATHE)
    engine.budget_per_s = 20
    feed_beats(engine, 1_000_000, 4)
    # First step happens 1 s after the first beat should have fired.
    out = engine.step(2_000_000, LEAD)
    assert all(c.effect_us >= 2_000_000 + LEAD - 150_000 for c in out)


def test_bar_shift_uses_one_group_frame_per_bar_and_respects_spacing() -> None:
    settings = EngineSettings(bulbs=5, has_group=True, group_min_interval_us=2_000_000)
    engine = EffectEngine(settings, Mode.BAR)
    engine.tracks_downbeats = True
    beats = feed_beats(engine, 1_000_000, 32, interval=400_000)  # 150 BPM: bar = 1.6 s
    sent = run(engine, 0, beats[-1])
    groups = [c for _, c in sent if c.target == GROUP]
    assert groups and all(c.rgb is not None for c in groups)
    assert len([c for _, c in sent if c.target != GROUP]) == 0
    gaps = [b.effect_us - a.effect_us for a, b in itertools.pairwise(groups)]
    assert min(gaps) >= 2_000_000  # every other bar at 150 BPM
    assert len({c.rgb for c in groups[:2]}) == 2  # color actually changes


def test_bar_shift_without_group_rotates_colors_across_bulbs() -> None:
    engine = EffectEngine(EngineSettings(bulbs=3), Mode.BAR)
    engine.set_palette([(255, 0, 0), (0, 255, 0), (0, 0, 255)])
    engine.tracks_downbeats = True
    beats = feed_beats(engine, 1_000_000, 8)
    sent = run(engine, 0, beats[-1])
    bars = {}
    for _, c in sent:
        bars.setdefault(c.effect_us, {})[c.target] = c.rgb
    first, second = (bars[ts] for ts in sorted(bars)[:2])
    assert set(first.values()) == {(255, 0, 0), (0, 255, 0), (0, 0, 255)}
    assert first[0] == second[2] or first[0] != second[0]  # palette rotated


def test_reactive_stays_within_budget_and_follows_bands() -> None:
    engine = EffectEngine(EngineSettings(bulbs=5, min_level=10, max_level=254), Mode.REACTIVE)
    engine.budget_per_s = 12
    # Bass loud, treble silent; spectrum frames every 66 ms for 6 s.
    for i in range(90):
        ts = 1_000_000 + i * 66_000
        loud = 60_000 if (i // 8) % 2 else 20_000  # bass pumps
        engine.add_spectrum(ts, [loud, loud, 30_000, 30_000, 25_000, 25_000, 10_000, 10_000, 0, 0])
    sent = run(engine, 900_000, 6_900_000)
    frames = [c for _, c in sent]
    assert len(frames) / 6.0 <= 12
    bass = [c.level for c in frames if c.target == 0]
    treble = [c.level for c in frames if c.target == 4]
    assert max(bass) > 200 and min(bass) < 150  # follows the pumping bass
    assert all(level <= 60 for level in treble)  # silent band stays dim


def test_onsets_drive_pulses_when_no_beats_arrive() -> None:
    engine = EffectEngine(EngineSettings(bulbs=2), Mode.BREATHE)
    engine.budget_per_s = 20
    for i in range(10):
        engine.add_peak(1_000_000 + i * 450_000, 200)
        engine.add_peak(1_000_000 + i * 450_000 + 100_000, 200)  # too close: ignored
    sent = run(engine, 0, 6_000_000)
    pulses = sorted({c.effect_us for _, c in sent if c.level is not None and c.priority == PRIO_BEAT})
    assert pulses == [1_000_000 + i * 450_000 for i in range(10)]
    assert engine.beat_source == "onsets"


def test_ambient_is_gentle_and_cycles_palette() -> None:
    settings = EngineSettings(bulbs=5, ambient_period_us=10_000_000)
    engine = EffectEngine(settings, Mode.AMBIENT)
    engine.set_palette([(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255)])
    sent = run(engine, 0, 30_000_000)
    assert len(sent) / 30 <= 1.5
    colors = [c for _, c in sent if c.rgb is not None and c.target == 0]
    assert len({c.rgb for c in colors}) >= 3
    assert all(c.transition_ds == 80 for c in colors)


def test_no_pulse_falls_back_to_ambient_in_beat_modes() -> None:
    engine = EffectEngine(EngineSettings(bulbs=5), Mode.BREATHE)
    sent = run(engine, 0, 12_000_000)
    assert sent and all(c.transition_ds == 80 for _, c in sent)
    assert engine.beat_source == "none"


def test_tempo_estimate_uses_upcoming_beats() -> None:
    engine = EffectEngine(EngineSettings(bulbs=1), Mode.BREATHE)
    feed_beats(engine, 1_000_000, 8, interval=468_750)  # 128 BPM
    engine.step(1_000_000, 0)
    assert engine.bpm == 128.0


def test_breathe_in_three_four_flashes_on_every_downbeat() -> None:
    engine = EffectEngine(EngineSettings(bulbs=1, min_level=40), Mode.BREATHE)
    engine.budget_per_s = 20
    engine.tracks_downbeats = True
    beats = feed_beats(engine, 1_000_000, 13, bar=3)
    sent = run(engine, 0, beats[-1])
    pulses = {c.effect_us: c for _, c in sent if c.level is not None and c.priority == PRIO_BEAT}
    for i, ts in enumerate(beats):
        flash = pulses[ts].transition_ds == 0 and pulses[ts].level > 40
        assert flash == (i % 3 == 0), f"beat {i}"


def test_107_bpm_pulses_every_beat_at_default_budget() -> None:
    """Jon's test track: 107-108 BPM must flash on beats 1 and 3 at the default 12 frames/s."""
    engine = EffectEngine(EngineSettings(bulbs=5, min_level=40), Mode.BREATHE)
    engine.budget_per_s = 12
    engine.tracks_downbeats = True
    beats = feed_beats(engine, 1_000_000, 16, interval=560_748)  # 107 BPM
    sent = run(engine, 0, beats[-1])
    bulb0 = [c for _, c in sent if c.level is not None and c.target == 0]
    assert [c.effect_us for c in bulb0] == beats
    flashes = [c.effect_us for c in bulb0 if c.transition_ds == 0]
    assert flashes == [ts for i, ts in enumerate(beats) if i % 4 in (0, 2)]
    assert len(sent) / ((beats[-1] - beats[0]) / 1e6) <= 12
