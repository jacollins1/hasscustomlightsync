"""Party mode: a rule-based light jockey.

A human operating lights at a party doesn't treat every bulb the same on every
beat. They work in *phrases*: for a couple of bars two lights answer three
others, then everything hits together for a bar, then one light holds a color
while the rest chase. They follow the music's energy (calm verse, building
pre-chorus, the drop), they change looks on bar lines, and they rarely repeat
themselves right away.

This module does the same, with no Home Assistant imports:

* A **formation** splits the bulbs into groups. Each group shares color,
  brightness and rhythm. A formation lasts 1-4 bars.
* Formations are picked by **energy tier** (the loudness of the music relative
  to the rest of the song), with weights and a no-immediate-repeat rule. An
  8-bar phrase boundary always brings a new look, and a sudden jump in
  energy (a drop) triggers an immediate all-bulb hit.
* The next formation's **colors are sent half a beat before** its first bar,
  while the bulbs are fading, so the downbeat only needs brightness frames.
* Colors come from the album-art scheme (which spans the wheel), with groups
  given hues far apart, and the scheme drifts a little with every formation, so
  a song visits much of the color wheel.
* Every flash is two frames: an instant jump to full brightness on the beat,
  then a fade back down before the bulb's next hit.

All randomness comes from one seeded ``random.Random``, so tests are repeatable.
"""

from __future__ import annotations

import colorsys
import random
from dataclasses import dataclass, field

from .model import PRIO_BEAT, PRIO_COLOR, PRIO_LEVEL, RGB, Command, ds
from .palette import FALLBACK_PALETTE, hue_distance, hue_of

DECAY_DELAY_US = 90_000  # a flash holds full brightness this long before fading
COLOR_TRANSITION_DS = 2
HOLD_TRANSITION_DS = 4
DRIFT_PER_FORMATION = 0.035  # scheme hue rotation per new look (~1/28 of the wheel)

# Energy is how much louder (or quieter) the music is right now than its own
# running average, so it works for a quiet acoustic track and a brickwalled
# dance track alike: 0.5 = typical for this song, 0/1 = 4 dB below/above.
ENERGY_SPAN_DB = 8.0
SLOW_ALPHA = 0.015  # running average over ~16 bars of beats
FAST_ALPHA = 0.4
LOW_ENERGY = 0.35
HIGH_ENERGY = 0.7
DROP_DB = 5.0  # a downbeat this much louder than the previous bar is a drop

# How often each look is picked, per energy tier.
WEIGHTS: dict[str, dict[str, int]] = {
    "low": {"swell": 4, "spotlight": 3, "hold_pairs": 2, "call_response": 1},
    "mid": {"call_response": 3, "hold_pairs": 3, "build": 2, "chase": 2, "unison": 1, "swell": 1},
    "high": {"unison": 3, "chase": 3, "sparkle": 3, "call_response": 2, "hold_pairs": 1, "build": 1},
}
# (min, max) bars per look; build is driven by how many steps it has.
BARS: dict[str, tuple[int, int]] = {
    "unison": (1, 2),
    "call_response": (2, 2),
    "hold_pairs": (2, 2),
    "chase": (2, 4),
    "swell": (2, 4),
    "spotlight": (2, 4),
    "sparkle": (2, 2),
    "build": (4, 4),
}


@dataclass(slots=True)
class BeatInfo:
    """One beat, as the choreographer sees it."""

    ts: int  # when the beat is heard (client clock, µs)
    interval: int  # µs to the next beat
    position: int  # beat within the bar, 0 = downbeat
    bar_len: int
    bar_start: bool
    next_is_bar: bool  # the next beat starts a bar
    loudness_db: float | None  # mean loudness over this beat, dBFS (None if unknown)


@dataclass
class Formation:
    """A look: bulbs split into groups that share color, brightness and rhythm."""

    kind: str
    groups: list[list[int]]
    colors: list[RGB]
    bars: int
    bars_done: int = 0
    beats_done: int = 0
    order: list[int] = field(default_factory=list)

    def group_of(self, bulb: int) -> int:
        for index, members in enumerate(self.groups):
            if bulb in members:
                return index
        return 0


class Choreographer:
    """Decides, beat by beat, what every bulb does."""

    def __init__(self, bulbs: int, min_level: int, max_level: int, seed: int | None = None) -> None:
        self.bulbs = bulbs
        self.min_level = min_level
        self.max_level = max_level
        self._rng = random.Random(seed)
        self._scheme: list[RGB] = list(FALLBACK_PALETTE)
        self.reset()

    def reset(self) -> None:
        """New song or seek: start a fresh phrase."""
        self.formation: Formation | None = None
        self._next: Formation | None = None
        self._bar_count = 0
        self._recent: list[str] = []
        self._energy = 0.5
        self._fast: float | None = None
        self._slow: float | None = None
        self._bar_db_sum = 0.0
        self._bar_db_beats = 0
        self._drift = 0.0
        self._cursor = 0
        self._level: list[int | None] = [None] * self.bulbs
        self._color: list[RGB | None] = [None] * self.bulbs
        self._last_sparkle: set[int] = set()

    def set_palette(self, colors: list[RGB]) -> None:
        """Album-art scheme (spans the wheel)."""
        if colors:
            self._scheme = list(colors)

    # ----------------------------------------------------------------- levels

    def _high(self) -> int:
        return round(self.min_level + (self.max_level - self.min_level) * (0.75 + 0.25 * self._energy))

    def _mid(self) -> int:
        return round(self.min_level + (self.max_level - self.min_level) * 0.8)

    def _glow(self) -> int:
        return round(self.min_level + (self.max_level - self.min_level) * 0.3)

    # ----------------------------------------------------------------- colors

    def _drifted(self, rgb: RGB) -> RGB:
        h, s, v = colorsys.rgb_to_hsv(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)
        r, g, b = colorsys.hsv_to_rgb((h + self._drift) % 1.0, s, v)
        return (round(r * 255), round(g * 255), round(b * 255))

    def _pick_colors(self, count: int) -> list[RGB]:
        """``count`` colors from the scheme, as far apart on the wheel as possible."""
        scheme = [self._drifted(c) for c in self._scheme]
        first = scheme[self._cursor % len(scheme)]
        self._cursor += self._rng.choice((1, 2, 3))
        chosen = [first]
        while len(chosen) < count:
            best = max(
                (c for c in scheme if c not in chosen),
                key=lambda c: min(hue_distance(hue_of(c), hue_of(x)) for x in chosen),
                default=None,
            )
            if best is None:
                best = scheme[(len(chosen) * 3) % len(scheme)]
            chosen.append(best)
        return chosen

    # ------------------------------------------------------------- formations

    def _tier(self) -> str:
        if self._energy < LOW_ENERGY:
            return "low"
        if self._energy > HIGH_ENERGY:
            return "high"
        return "mid"

    def _choose_kind(self, forced: str | None = None) -> str:
        if forced:
            return forced
        weights = dict(WEIGHTS[self._tier()])
        if self.bulbs < 3:
            weights = {k: w for k, w in weights.items() if k in ("unison", "call_response", "chase", "swell")}
        for recent in self._recent[-2:]:  # don't repeat the last two looks
            if len(weights) > 2:
                weights.pop(recent, None)
        kinds, values = zip(*weights.items(), strict=True)
        return self._rng.choices(kinds, weights=values)[0]

    def _new_formation(self, forced: str | None = None) -> Formation:
        kind = self._choose_kind(forced)
        self._recent.append(kind)
        self._drift = (self._drift + DRIFT_PER_FORMATION) % 1.0
        everyone = list(range(self.bulbs))
        shuffled = everyone[:]
        self._rng.shuffle(shuffled)
        low, high = BARS[kind]
        bars = self._rng.randint(low, high)
        order: list[int] = []
        if kind in ("call_response",):
            cut = self.bulbs // 2
            groups = [shuffled[:cut], shuffled[cut:]]
        elif kind == "hold_pairs":
            rest = shuffled[1:]
            half = len(rest) // 2
            groups = [shuffled[:1], rest[:half], rest[half:]]
        elif kind == "spotlight":
            groups = [shuffled[:1], shuffled[1:]]
        elif kind == "swell":
            groups = [everyone] if self._rng.random() < 0.5 else [shuffled[::2], shuffled[1::2]]
        elif kind == "sparkle":
            groups = [[b] for b in everyone]
        elif kind == "chase":
            groups = [everyone]
            order = everyone[:] if self._rng.random() < 0.5 else everyone[::-1]
        elif kind == "build":
            groups = [everyone]
            order = shuffled
            bars = max(1, min(4, self.bulbs - 1))
        else:  # unison
            groups = [everyone]
        groups = [g for g in groups if g]
        if kind == "sparkle":
            palette = self._pick_colors(3)
            colors = [palette[i % 3] for i in range(len(groups))]
        else:
            colors = self._pick_colors(len(groups))
        return Formation(kind, groups, colors, bars, order=order)

    def _color_commands(self, formation: Formation, effect_us: int, out: list[Command]) -> None:
        for bulb in range(self.bulbs):
            rgb = formation.colors[formation.group_of(bulb)]
            if self._color[bulb] != rgb:
                out.append(Command(effect_us, bulb, None, rgb, COLOR_TRANSITION_DS, PRIO_COLOR))
                self._color[bulb] = rgb

    # ------------------------------------------------------------------- beat

    def on_beat(self, beat: BeatInfo) -> list[Command]:
        """Commands for one beat (some take effect later within the beat)."""
        out: list[Command] = []
        if beat.loudness_db is not None:
            loud = beat.loudness_db
            self._fast = loud if self._fast is None else self._fast + (loud - self._fast) * FAST_ALPHA
            self._slow = loud if self._slow is None else self._slow + (loud - self._slow) * SLOW_ALPHA
            self._energy = min(1.0, max(0.0, 0.5 + (self._fast - self._slow) / ENERGY_SPAN_DB))

        if beat.bar_start:
            self._bar_count += 1
            prev_bar_db = self._bar_db_sum / self._bar_db_beats if self._bar_db_beats else None
            self._bar_db_sum, self._bar_db_beats = 0.0, 0
            # Judged on this beat's own loudness: the smoothed energy lags a sudden drop.
            drop = (
                beat.loudness_db is not None
                and prev_bar_db is not None
                and beat.loudness_db - prev_bar_db >= DROP_DB
                and self._slow is not None
                and beat.loudness_db > self._slow
            )
            phrase = self._bar_count % 8 == 1 and self._bar_count > 1
            current = self.formation
            if current is not None:
                current.bars_done += 1
            if drop:
                self._energy = 1.0
                self._fast = beat.loudness_db
                self.formation = self._new_formation("unison")
                self.formation.bars = 1
                self._next = None
                self._color_commands(self.formation, beat.ts, out)
            elif current is None or current.bars_done >= current.bars or (phrase and current.bars_done >= 1):
                self.formation = self._next or self._new_formation()
                self._next = None
                self._color_commands(self.formation, beat.ts, out)
        elif self.formation is None:
            self.formation = self._new_formation()
            self._color_commands(self.formation, beat.ts, out)

        if beat.loudness_db is not None:
            self._bar_db_sum += beat.loudness_db
            self._bar_db_beats += 1
        formation = self.formation
        assert formation is not None
        if formation.beats_done == 0 and formation.kind not in ("swell", "spotlight"):
            # New look: bulbs with no part on this beat drop to the floor
            # (a flash offered below for the same moment replaces this).
            for bulb in range(self.bulbs):
                self._set(bulb, self.min_level, beat.ts, HOLD_TRANSITION_DS, out)
        self._play(formation, beat, out)
        formation.beats_done += 1

        # Prepare the next look half a beat before its first bar, while the
        # bulbs are fading, so its downbeat only costs brightness frames.
        last_bar = formation.bars_done + 1 >= formation.bars
        next_phrase = (self._bar_count + 1) % 8 == 1
        if beat.next_is_bar and (last_bar or next_phrase) and self._next is None:
            self._next = self._new_formation()
            self._color_commands(self._next, beat.ts + beat.interval // 2, out)
        return out

    def _flash(self, bulb: int, ts: int, gap_us: int, out: list[Command], level: int | None = None) -> None:
        """Instant jump to full on the beat, then fade to the floor before the next hit."""
        top = level if level is not None else self._high()
        out.append(Command(ts, bulb, top, None, 0, PRIO_BEAT))
        fade = max(100_000, int((gap_us - DECAY_DELAY_US) * 0.85))
        out.append(Command(ts + DECAY_DELAY_US, bulb, self.min_level, None, ds(fade), PRIO_LEVEL))
        self._level[bulb] = self.min_level

    def _set(self, bulb: int, level: int, effect_us: int, transition_ds: int, out: list[Command]) -> None:
        if self._level[bulb] != level:
            out.append(Command(effect_us, bulb, level, None, transition_ds, PRIO_LEVEL))
            self._level[bulb] = level

    def _play(self, f: Formation, beat: BeatInfo, out: list[Command]) -> None:  # noqa: C901, PLR0912
        ts, interval, pos = beat.ts, beat.interval, beat.position
        hot = self._energy > HIGH_ENERGY
        last_bar = f.bars_done + 1 >= f.bars
        fill = last_bar and beat.next_is_bar and self._energy >= LOW_ENERGY and f.kind not in ("swell", "spotlight")
        if fill:  # the operator's "hit" leading into the next look
            for bulb in range(self.bulbs):
                self._flash(bulb, ts, interval, out)
            return

        if f.kind == "unison":
            # Every beat only for a one-bar hit or the last bar; otherwise 1 and 3.
            every = (hot and f.bars == 1) or last_bar
            if every or pos % 2 == 0:
                for bulb in f.groups[0]:
                    self._flash(bulb, ts, interval * (1 if every else 2), out)

        elif f.kind == "call_response":
            group = f.groups[pos % 2] if len(f.groups) > 1 else f.groups[0]
            for bulb in group:
                self._flash(bulb, ts, interval * 2, out)

        elif f.kind == "hold_pairs":
            holder, *pairs = f.groups
            for bulb in holder:
                self._set(bulb, self._mid(), ts, HOLD_TRANSITION_DS, out)
            if pairs:
                for bulb in pairs[pos % len(pairs)]:
                    self._flash(bulb, ts, interval * len(pairs), out)

        elif f.kind == "chase":
            n = len(f.order)
            first = f.order[f.beats_done % n]
            self._flash(first, ts, interval, out)
            if hot:  # double time: another bulb on the off-beat
                second = f.order[(f.beats_done + n // 2) % n]
                self._flash(second, ts + interval // 2, interval // 2, out)

        elif f.kind == "build":
            size = min(self.bulbs, 2 + f.bars_done)
            active = f.order[:size]
            every = f.bars_done + 1 >= f.bars  # last step: hit every beat
            if every or pos % 2 == 0:
                for bulb in active:
                    self._flash(bulb, ts, interval * (1 if every else 2), out)
            for bulb in f.order[size:]:
                self._set(bulb, self.min_level, ts, HOLD_TRANSITION_DS, out)

        elif f.kind == "swell":
            if beat.bar_start:
                bar_us = interval * beat.bar_len
                for index, group in enumerate(f.groups):
                    up = (f.bars_done + index) % 2 == 0
                    level = self._high() if up else self._glow()
                    for bulb in group:
                        self._set(bulb, level, ts, ds(bar_us * 0.95), out)

        elif f.kind == "spotlight":
            holder, rest = f.groups[0], f.groups[1] if len(f.groups) > 1 else []
            for bulb in holder:
                self._set(bulb, self.max_level, ts, HOLD_TRANSITION_DS, out)
            if beat.bar_start:
                bar_us = interval * beat.bar_len
                level = self._glow() if f.bars_done % 2 == 0 else self.min_level
                for bulb in rest:
                    self._set(bulb, level, ts, ds(bar_us * 0.95), out)

        elif f.kind == "sparkle":
            hits = 2 if hot else 1
            choices = [b for b in range(self.bulbs) if b not in self._last_sparkle] or list(range(self.bulbs))
            picked = self._rng.sample(choices, min(hits, len(choices)))
            for bulb in picked:
                self._flash(bulb, ts, interval, out)
            if hot:
                extra = [b for b in range(self.bulbs) if b not in picked]
                if extra:
                    self._flash(self._rng.choice(extra), ts + interval // 2, interval // 2, out)
            self._last_sparkle = set(picked)
