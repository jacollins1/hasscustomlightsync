"""Effect engine: timestamped music features in, per-bulb Zigbee commands out.

Pure Python with no Home Assistant imports so it can be unit-tested alone.

Every timestamp is in microseconds on the Sendspin *client* clock, already
translated from server time, and means "when this is audible". Music
Assistant delivers features 0.3-1.5 s before they are audible, so the engine
can emit a command early enough that it reaches the bulb on time:
``step(now, lead)`` returns every command whose effect time is at or before
``now + lead``, where ``lead`` is the measured Zigbee delivery delay plus the
user's offset.

Each command is exactly one ZCL frame (level OR color), because that is the
unit the Zigbee network budget is measured in.
"""

from __future__ import annotations

import itertools
import statistics
from collections import deque
from dataclasses import dataclass
from enum import StrEnum

from .choreo import BeatInfo, Choreographer
from .model import (  # noqa: F401 - re-exported for callers and tests
    GROUP,
    PRIO_AMBIENT,
    PRIO_BEAT,
    PRIO_COLOR,
    PRIO_LEVEL,
    STALE_US,
    Command,
)
from .model import ds as _ds
from .palette import FALLBACK_PALETTE, RGB

# Beats/peaks older than this no longer count as "music has a pulse".
PULSE_TIMEOUT_US = 4_000_000
PARTY_PREP_US = 250_000  # party plans beats this far ahead of their send time
# Pseudo-beats derived from onsets never come closer together than this.
MIN_PSEUDO_BEAT_US = 300_000
PEAK_MIN_STRENGTH = 110  # of 255

DEFAULT_BEAT_US = 500_000  # 120 BPM until we know better
MIN_BEAT_US = 230_000  # ~260 BPM
MAX_BEAT_US = 1_500_000  # 40 BPM

FEATURE_KEEP_US = 3_000_000
# Hard caps so a stalled consumer can never grow memory without bound.
MAX_QUEUED_BEATS = 2048
MAX_QUEUED_FRAMES = 1024

# Loudness/spectrum are dB-scaled: -60 dB -> 0, 0 dB -> 65535.
DB_UNIT = 65535 / 60
AGC_RANGE = 24 * DB_UNIT  # show the top 24 dB of the recent dynamic range
AGC_DECAY_PER_S = 3 * DB_UNIT  # running max sinks 3 dB/s after a loud passage


class Mode(StrEnum):
    """Effect modes exposed to the user."""

    PARTY = "party"
    BREATHE = "beat_breathe"
    BAR = "bar_color_shift"
    REACTIVE = "max_reactive"
    AMBIENT = "ambient_palette"


@dataclass(slots=True)
class EngineSettings:
    """Tunables, set from the config entry options."""

    bulbs: int
    min_level: int = 40
    max_level: int = 254
    has_group: bool = False
    group_min_interval_us: int = 2_000_000
    bar_transition_ds: int = 3
    ambient_period_us: int = 10_000_000
    color_every_bars: int = 2
    layout: list[tuple[float, float]] | None = None  # bulb positions, 0..1, in bulb order


class _Agc:
    """Running-max automatic gain control on the dB-scaled feature values."""

    __slots__ = ("_last_us", "_peak")

    def __init__(self) -> None:
        self._peak = 0.0
        self._last_us: int | None = None

    def norm(self, value: float, ts_us: int) -> float:
        if self._last_us is not None and ts_us > self._last_us:
            self._peak -= AGC_DECAY_PER_S * (ts_us - self._last_us) / 1e6
        self._last_us = ts_us
        self._peak = max(self._peak, value, AGC_RANGE)
        return min(1.0, max(0.0, (value - (self._peak - AGC_RANGE)) / AGC_RANGE))


class EffectEngine:
    """Turns beats, onsets, loudness and spectrum into light commands."""

    def __init__(self, settings: EngineSettings, mode: Mode = Mode.PARTY, seed: int | None = None) -> None:
        self.settings = settings
        self.choreo = Choreographer(
            settings.bulbs, settings.min_level, settings.max_level, seed=seed, layout=settings.layout
        )
        self.mode = mode
        self.budget_per_s = 10.0
        self._intensity = 1.0
        self.tracks_downbeats = False
        self._palette: list[RGB] = list(FALLBACK_PALETTE)
        self._beats: deque[tuple[int, bool]] = deque()
        self._peaks: deque[tuple[int, int]] = deque()
        self._loudness: deque[tuple[int, float]] = deque()
        self._spectrum: deque[tuple[int, list[float]]] = deque()
        self._beat_hist: deque[int] = deque(maxlen=12)
        self._loud_agc = _Agc()
        self._band_agc: list[_Agc] = []
        self.reset()

    # ------------------------------------------------------------------ input

    def reset(self) -> None:
        """Forget all queued features (stream start, seek, track skip)."""
        self._beats.clear()
        self._peaks.clear()
        self._loudness.clear()
        self._spectrum.clear()
        self._beat_hist.clear()
        self._band_agc = [_Agc() for _ in range(self.settings.bulbs)]
        self.choreo.reset()
        self._beat_index = 0
        self._beat_in_bar = 0
        self._last_pulse_us: int | None = None
        self._last_pseudo_us: int | None = None
        self._last_real_beat_us: int | None = None
        self._last_group_us: int | None = None
        self._bar_count = 0
        self._breathe_bars = 0
        self._last_flash = False
        self._bar_len = 4  # beats per bar, learned from downbeats when tracked
        self._palette_shift = 0
        self._recolor_idx = 0
        self._slot_us: int | None = None
        self._slot_bulb = 0
        self._last_level: list[int | None] = [None] * self.settings.bulbs
        self.beat_source = "none"

    def set_mode(self, mode: Mode) -> None:
        """Switch effect mode; periodic schedules restart, queued features stay."""
        self.mode = mode
        self._slot_us = None
        self._slot_bulb = 0

    def set_palette(self, colors: list[RGB]) -> None:
        """Replace the color palette (one entry per bulb or more)."""
        if colors:
            self._palette = list(colors)
            self.choreo.set_palette(colors)

    @property
    def palette(self) -> list[RGB]:
        """Current palette."""
        return list(self._palette)

    def add_beat(self, ts_us: int, downbeat: bool) -> None:
        """Queue a musical beat (from Music Assistant's beat tracker)."""
        if (self._beats and ts_us <= self._beats[-1][0]) or len(self._beats) >= MAX_QUEUED_BEATS:
            return
        self._beats.append((ts_us, downbeat))

    def add_peak(self, ts_us: int, strength: int) -> None:
        """Queue an energy onset (0-255)."""
        if len(self._peaks) < MAX_QUEUED_FRAMES:
            self._peaks.append((ts_us, strength))

    def add_loudness(self, ts_us: int, value: int) -> None:
        """Queue an overall loudness frame (dB-scaled uint16)."""
        if len(self._loudness) < MAX_QUEUED_FRAMES:
            self._loudness.append((ts_us, float(value)))

    def add_spectrum(self, ts_us: int, bins: list[int]) -> None:
        """Queue a spectrum frame (dB-scaled uint16 per bin, low to high)."""
        if len(self._spectrum) < MAX_QUEUED_FRAMES:
            self._spectrum.append((ts_us, [float(b) for b in bins]))

    # ---------------------------------------------------------------- derived

    @property
    def beat_interval_us(self) -> int:
        """Median spacing of recent and upcoming beats."""
        stamps = [*self._beat_hist, *(ts for ts, _ in itertools.islice(self._beats, 8))]
        diffs = [b - a for a, b in itertools.pairwise(stamps) if b > a]
        diffs = [d for d in diffs if MIN_BEAT_US <= d <= MAX_BEAT_US]
        if not diffs:
            return DEFAULT_BEAT_US
        return int(statistics.median(diffs[-8:]))

    @property
    def bpm(self) -> float | None:
        """Tempo estimate, or None without a pulse source."""
        if self.beat_source == "none":
            return None
        return round(60_000_000 / self.beat_interval_us, 1)

    def _pulse_divisor(self) -> int:
        """Pulse every d beats so per-bulb pulses fit in 75 % of the budget."""
        beats_per_s = 1e6 / self.beat_interval_us
        # Level frames only: color frames are lower priority and coalesce in the governor.
        frames_per_pulse = self.settings.bulbs
        divisor = 1
        while divisor < 8 and frames_per_pulse * beats_per_s / divisor > 0.75 * self.budget_per_s:
            divisor *= 2
        return divisor

    def _loud_norm(self, start_us: int, end_us: int) -> float | None:
        values = [v for ts, v in self._loudness if start_us <= ts <= end_us]
        if not values:
            return None
        return self._loud_agc.norm(statistics.fmean(values), end_us)

    def _band_norm(self, bulb: int, start_us: int, end_us: int) -> float | None:
        frames = [bins for ts, bins in self._spectrum if start_us <= ts <= end_us]
        if not frames:
            return None
        n = self.settings.bulbs
        nbins = len(frames[0])
        lo = bulb * nbins // n
        hi = max(lo + 1, (bulb + 1) * nbins // n)
        band = statistics.fmean(max(f[lo:hi]) for f in frames)
        return self._band_agc[bulb].norm(band, end_us)

    def _color(self, idx: int) -> RGB:
        return self._palette[idx % len(self._palette)]

    # ------------------------------------------------------------------ output

    @property
    def intensity(self) -> float:
        """0.1..1: how far above the floor the effects may go (the Intensity control)."""
        return self._intensity

    @intensity.setter
    def intensity(self, value: float) -> None:
        self._intensity = max(0.1, min(1.0, value))
        self.choreo.intensity = self._intensity

    def _top(self) -> int:
        s = self.settings
        return round(s.min_level + (s.max_level - s.min_level) * self._intensity)

    def step(self, now_us: int, lead_us: int) -> list[Command]:
        """Return the commands that must be sent now to land on time."""
        horizon = now_us + lead_us
        out: list[Command] = []

        self._run_due_pulses(horizon, out)
        has_pulse = self._has_pulse(horizon)

        if self.mode is Mode.REACTIVE:
            self._reactive(horizon, out)
        elif self.mode is Mode.AMBIENT or not has_pulse:
            self._ambient(horizon, out)

        self._prune(horizon)
        return out

    def _run_due_pulses(self, horizon: int, out: list[Command]) -> None:
        """Pop due beats (or, without beats, strong onsets) and act on each in order.

        Each pulse is handled right after it advances the bar position, so the
        handler sees the bar position and the upcoming-beat queue as of that
        pulse, even when one tick pops several.
        """
        # Party plans each beat a little ahead so a look change can recolor the
        # bulbs before the hit instead of in the same instant (which would put
        # 10 frames on the air at once at a drop).
        reach = horizon + (PARTY_PREP_US if self.mode is Mode.PARTY else 0)
        while self._beats and self._beats[0][0] <= reach:
            ts, downbeat = self._beats.popleft()
            self._beat_hist.append(ts)
            self._last_real_beat_us = ts
            self._advance_bar(downbeat)
            self.beat_source = "beats"
            self._on_pulse(ts, horizon, out)
        beats_recent = self._last_real_beat_us is not None and horizon - self._last_real_beat_us < PULSE_TIMEOUT_US
        while self._peaks and self._peaks[0][0] <= horizon:
            ts, strength = self._peaks.popleft()
            if beats_recent or self._beats or strength < PEAK_MIN_STRENGTH:
                continue
            if self._last_pseudo_us is not None and ts - self._last_pseudo_us < MIN_PSEUDO_BEAT_US:
                continue
            self._last_pseudo_us = ts
            self._beat_hist.append(ts)
            self._advance_bar(False)
            self.beat_source = "onsets"
            self._on_pulse(ts, horizon, out)

    def _on_pulse(self, ts: int, horizon: int, out: list[Command]) -> None:
        if ts < horizon - STALE_US:
            return  # counted for bar position, but too late to show
        bar_start = self._beat_in_bar == 0
        if self.mode is Mode.PARTY:
            self._party(ts, bar_start, out)
        elif self.mode is Mode.BREATHE:
            self._breathe(ts, bar_start, out)
        elif self.mode is Mode.BAR:
            self._bar(ts, bar_start, out)
        elif self.mode is Mode.REACTIVE:
            self._rolling_color(ts, bar_start, self.beat_interval_us, out)

    def _advance_bar(self, downbeat: bool) -> None:
        if self.tracks_downbeats and downbeat:
            if self._beat_index > 0 and self._beat_in_bar > 0:
                self._bar_len = self._beat_in_bar + 1  # learn the meter
            self._beat_in_bar = 0
        elif self._beat_index > 0:
            self._beat_in_bar = (self._beat_in_bar + 1) % (64 if self.tracks_downbeats else 4)
        self._beat_index += 1

    def _has_pulse(self, horizon: int) -> bool:
        recent = bool(self._beat_hist) and horizon - self._beat_hist[-1] < PULSE_TIMEOUT_US
        if not recent and not self._beats:
            self.beat_source = "none"
        return recent or bool(self._beats)

    # ------------------------------------------------------------------ modes

    def _next_pulse(self, ts: int, divisor: int) -> tuple[int, bool]:
        """When the next pulse beat is, and whether it starts a bar.

        Uses the queued upcoming beats (Music Assistant sends them well ahead),
        so odd meters and tempo changes are handled; falls back to the tempo
        estimate when the queue is empty.
        """
        bar_len = 64 if self.tracks_downbeats else 4
        position = self._beat_in_bar
        for next_ts, downbeat in self._beats:
            position = 0 if (self.tracks_downbeats and downbeat) else (position + 1) % bar_len
            if position % divisor == 0:
                return next_ts, position == 0
        meter = self._bar_len if self.tracks_downbeats else 4
        return ts + self.beat_interval_us * divisor, (self._beat_in_bar + divisor) % meter == 0

    def _party(self, ts: int, bar_start: bool, out: list[Command]) -> None:
        """Hand the beat to the light jockey."""
        interval = self.beat_interval_us
        next_ts, next_is_bar = self._next_pulse(ts, 1)
        values = [v for t, v in self._loudness if ts <= t <= next_ts]
        loudness_db = statistics.fmean(values) / DB_UNIT - 60 if values else None
        beat = BeatInfo(
            ts=ts,
            interval=max(MIN_BEAT_US, min(MAX_BEAT_US, next_ts - ts)) if next_ts > ts else interval,
            position=self._beat_in_bar,
            bar_len=self._bar_len if self.tracks_downbeats else 4,
            bar_start=bar_start,
            next_is_bar=next_is_bar,
            loudness_db=loudness_db,
            budget_per_s=self.budget_per_s,
        )
        out.extend(self._multicast(self.choreo.on_beat(beat)))

    def _multicast(self, commands: list[Command]) -> list[Command]:
        """With a ZHA group, send a whole-room change (same value for every
        bulb at the same moment, e.g. the drop's white flash) as one frame."""
        s = self.settings
        if not s.has_group or s.bulbs < 2:
            return commands
        buckets: dict[tuple, list[Command]] = {}
        for cmd in commands:
            buckets.setdefault((cmd.effect_us, cmd.level, cmd.rgb, cmd.transition_ds, cmd.priority), []).append(cmd)
        result: list[Command] = []
        for (effect_us, level, rgb, transition_ds, priority), cmds in buckets.items():
            whole_room = len({c.target for c in cmds}) == s.bulbs
            # Margin over the governor's own spacing so it never holds a group frame back.
            spaced = self._last_group_us is None or effect_us - self._last_group_us >= s.group_min_interval_us + 500_000
            if whole_room and spaced:
                self._last_group_us = effect_us
                result.append(Command(effect_us, GROUP, level, rgb, transition_ds, priority))
            else:
                result.extend(cmds)
        return result

    def _breathe(self, ts: int, bar_start: bool, out: list[Command]) -> None:
        """Flash on the beat, release before the next one.

        One frame per bulb per pulse: a flash pulse jumps to full brightness
        with no fade (the eye locks onto that attack), and the next pulse fades
        back down over the whole gap. Bar starts always flash, so in 4/4 the
        flashes land on beats 1 and 3, and in 3/4 on beat 1.
        """
        divisor = self._pulse_divisor()
        if divisor <= 2:
            if self._beat_in_bar % divisor and not bar_start:
                return
            next_ts, next_is_bar = self._next_pulse(ts, divisor)
        else:
            # Fast tempo or tight budget: pulse once per bar (or every 2 bars).
            if not bar_start:
                return
            self._breathe_bars += 1
            if divisor == 8 and self._breathe_bars % 2:
                return
            next_ts, next_is_bar = ts + self.beat_interval_us * divisor, True
        flash = not self._last_flash and (bar_start or not next_is_bar)
        self._last_flash = flash
        span = max(100_000, next_ts - ts)
        s = self.settings
        if flash:
            loud = self._loud_norm(ts, ts + span)
            depth = 0.55 + 0.45 * (loud if loud is not None else 1.0)
            level = round(s.min_level + (self._top() - s.min_level) * depth)
            transition = 0 if self.intensity >= 0.6 else 2  # no hard flashes when dialed down
        else:
            level = s.min_level
            transition = _ds(span * 0.9)
        for bulb in range(s.bulbs):
            out.append(Command(ts, bulb, level, None, transition, PRIO_BEAT))
            self._last_level[bulb] = level
        if flash:
            self._rolling_color(ts, bar_start, 200_000, out)

    def _rolling_color(self, ts: int, bar_start: bool, span_us: int, out: list[Command]) -> None:
        """Advance colors: whole group on every Nth bar, else one bulb per pulse."""
        s = self.settings
        if s.has_group:
            if not bar_start:
                return
            self._bar_count += 1
            if self._bar_count % max(1, s.color_every_bars):
                return
            if self._last_group_us is not None and ts - self._last_group_us < s.group_min_interval_us:
                return
            self._last_group_us = ts
            self._palette_shift += 1
            out.append(Command(ts, GROUP, None, self._color(self._palette_shift), _ds(span_us), PRIO_COLOR))
            return
        bulb = self._recolor_idx % s.bulbs
        self._recolor_idx += 1
        if bulb == 0:
            self._palette_shift += 1
        out.append(Command(ts, bulb, None, self._color(bulb + self._palette_shift), _ds(span_us), PRIO_COLOR))

    def _bar(self, ts: int, bar_start: bool, out: list[Command]) -> None:
        """Snap every bulb to the next palette color on each bar."""
        if not bar_start:
            return
        s = self.settings
        if s.has_group:
            if self._last_group_us is not None and ts - self._last_group_us < s.group_min_interval_us:
                return  # broadcast budget: skip this bar, take the next
            self._last_group_us = ts
            self._palette_shift += 1
            out.append(Command(ts, GROUP, None, self._color(self._palette_shift), s.bar_transition_ds, PRIO_BEAT))
            return
        self._palette_shift += 1
        for bulb in range(s.bulbs):
            out.append(Command(ts, bulb, None, self._color(bulb + self._palette_shift), s.bar_transition_ds, PRIO_BEAT))

    def _reactive(self, horizon: int, out: list[Command]) -> None:
        """Round-robin per-bulb levels from one frequency band each."""
        s = self.settings
        color_rate = min(1e6 / self.beat_interval_us, 0.2 * self.budget_per_s) if self._has_pulse(horizon) else 0
        level_rate = max(0.5, 0.85 * self.budget_per_s - color_rate)
        slot = int(1e6 / level_rate)  # one bulb per slot
        per_bulb = slot * s.bulbs  # each bulb's update period
        if self._slot_us is None or self._slot_us < horizon - per_bulb:
            self._slot_us = horizon
        while self._slot_us <= horizon:
            bulb = self._slot_bulb
            start = self._slot_us
            energy = self._band_norm(bulb, start, start + per_bulb)
            if energy is None:
                energy = self._loud_norm(start, start + per_bulb)
            if energy is not None:
                level = round(s.min_level + (self._top() - s.min_level) * energy**0.8)
                last = self._last_level[bulb]
                if last is None or abs(level - last) >= 4:
                    out.append(Command(start, bulb, level, None, _ds(per_bulb), PRIO_LEVEL))
                    self._last_level[bulb] = level
            self._slot_bulb = (bulb + 1) % s.bulbs
            self._slot_us += slot

    def _ambient(self, horizon: int, out: list[Command]) -> None:
        """Slowly walk each bulb through the palette, one bulb per slot."""
        s = self.settings
        slot = max(500_000, s.ambient_period_us // s.bulbs)
        if self._slot_us is None or self._slot_us < horizon - s.ambient_period_us:
            self._slot_us = horizon
        while self._slot_us <= horizon:
            bulb = self._slot_bulb
            start = self._slot_us
            if bulb == 0:
                self._palette_shift += 1
            transition = _ds(s.ambient_period_us * 0.8)
            out.append(Command(start, bulb, None, self._color(bulb + self._palette_shift), transition, PRIO_AMBIENT))
            loud = self._loud_norm(start - 2_000_000, start + 2_000_000)
            level = round(s.max_level * (0.6 + 0.4 * (loud if loud is not None else 1.0)))
            last = self._last_level[bulb]
            if last is None or abs(level - last) >= 10:
                out.append(Command(start, bulb, level, None, transition, PRIO_AMBIENT))
                self._last_level[bulb] = level
            self._slot_bulb = (bulb + 1) % s.bulbs
            self._slot_us += slot

    def _prune(self, horizon: int) -> None:
        cutoff = horizon - FEATURE_KEEP_US
        for queue in (self._loudness, self._spectrum, self._peaks):
            while queue and queue[0][0] < cutoff:
                queue.popleft()
