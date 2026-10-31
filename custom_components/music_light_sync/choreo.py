"""Party mode: a rule-based lighting designer.

Pure Python, no Home Assistant imports. It follows the house lighting rules
(project instructions) and the way a person at a lighting desk works:

Song structure ("progressive color storytelling")
    Every bar is classified into a section from the music's energy relative to
    the song itself (dB above/below its own running average) and its trend:
    WARMUP, GROOVE, BUILD, DROP, PEAK, BREAKDOWN, OUTRO. Sections change only
    on bar lines. A build is cool, dark and half-time; the drop switches
    instantly to warm high-contrast colors with a white impact bar; the outro
    lands softly on amber and warm white.

Two-color rule
    Each section uses one curated color pair for its mood (moody: deep blue +
    purple...; euphoric: cyan + magenta...; energetic: red + amber...), biased
    toward the album art and never the previous section's pair. A look uses at
    most those two colors. True white only appears in the drop's impact bar.
    Variety comes from the pairs changing section by section.

Looks
    A look = a rhythm x a grouping x a rate, lasting 1-4 bars. Groupings come
    from the bulbs' physical layout (left/right, top/bottom, nearest-neighbor
    pairs such as 2+2+1, 3+2, 4+1, 1+2+2, center vs. outer, singles, or a
    walking order along x, y or around the room), so the same code works for
    any room. Rhythms: unison, call and response, canon (rotating groups),
    hold-and-play, chase, bounce, comet, sparkle, wave, swell, spotlight,
    build-up and impact. Looks never repeat back to back.

Tempo and safety
    Every event lands on the beat grid (bar, half note, beat or eighth). The
    rate is the fastest the Zigbee budget allows at the current tempo. Flash
    instants across the room are kept >= 100 ms apart (<= 10 Hz, inside the
    1-13 Hz strobe-safe band, well away from 15-20 Hz), a bulb never flashes
    faster than 8 Hz, and instant (0 ms) attacks are only allowed in high
    energy sections at full volume. Quiet music gets gentle, slow, shallow
    brightness changes.
"""

from __future__ import annotations

import itertools
import math
import random
import statistics
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum

from .model import PRIO_BEAT, PRIO_COLOR, PRIO_LEVEL, RGB, Command, ds
from .palette import hue_distance, hue_of

# ------------------------------------------------------------------- colors

WHITE: RGB = (255, 255, 255)
WARM_WHITE: RGB = (255, 180, 105)
DEEP_BLUE: RGB = (0, 35, 255)
VIOLET: RGB = (95, 0, 255)
PURPLE: RGB = (165, 0, 255)
CYAN: RGB = (0, 215, 255)
MAGENTA: RGB = (255, 0, 170)
GREEN: RGB = (0, 255, 70)
RED: RGB = (255, 0, 0)
AMBER: RGB = (255, 105, 0)

MOOD_PAIRS: dict[str, list[tuple[RGB, RGB]]] = {
    "moody": [(DEEP_BLUE, PURPLE), (DEEP_BLUE, VIOLET), (VIOLET, MAGENTA)],
    "euphoric": [(CYAN, MAGENTA), (MAGENTA, GREEN), (CYAN, GREEN), (GREEN, PURPLE)],
    "energetic": [(RED, AMBER), (AMBER, DEEP_BLUE), (RED, CYAN)],
    "outro": [(AMBER, WARM_WHITE)],
}
BUILD_TONES: list[RGB] = [DEEP_BLUE, CYAN, VIOLET]

# ------------------------------------------------------------------- timing

DECAY_DELAY_US = 120_000  # a hit holds its peak this long before fading
HIT_COLOR_LEAD_US = 250_000  # a new look's colors go out up to this long before its first hit
MIN_ROOM_FLASH_GAP_US = 100_000  # <= 10 Hz across the room
MIN_BULB_FLASH_GAP_US = 125_000  # <= 8 Hz per bulb
COLOR_TRANSITION_DS = 3
HOLD_TRANSITION_DS = 6

# ------------------------------------------------------------------- energy

ENERGY_SPAN_DB = 8.0  # 0/1 = 4 dB below/above the song's running average
SLOW_ALPHA = 0.015
FAST_ALPHA = 0.4
DROP_DB = 5.0  # out of a breakdown
DROP_BIG_DB = 8.0  # anywhere
DROP_AFTER_BUILD_DB = 1.5  # above the build's loudest bar


class Section(StrEnum):
    """Where we are in the song."""

    WARMUP = "warmup"
    GROOVE = "groove"
    BUILD = "build"
    DROP = "drop"
    PEAK = "peak"
    BREAKDOWN = "breakdown"
    OUTRO = "outro"


@dataclass(frozen=True, slots=True)
class Profile:
    """Brightness envelope for a section (fractions of the min..max range)."""

    peak: float
    floor: float
    attack_ds: int
    instant: bool  # 0 ms attacks allowed


PROFILES: dict[Section, Profile] = {
    Section.WARMUP: Profile(0.45, 0.08, 5, False),
    Section.GROOVE: Profile(0.80, 0.06, 1, False),
    Section.BUILD: Profile(0.55, 0.04, 3, False),
    Section.DROP: Profile(1.00, 0.02, 0, True),
    Section.PEAK: Profile(1.00, 0.03, 0, True),
    Section.BREAKDOWN: Profile(0.40, 0.06, 5, False),
    Section.OUTRO: Profile(0.50, 0.20, 6, False),
}

SECTION_MOOD: dict[Section, str] = {
    Section.WARMUP: "moody",
    Section.GROOVE: "euphoric",
    Section.BUILD: "build",
    Section.DROP: "energetic",
    Section.PEAK: "energetic",
    Section.BREAKDOWN: "moody",
    Section.OUTRO: "outro",
}

RATES: dict[str, float] = {"eighth": 2.0, "beat": 1.0, "half": 0.5, "bar": 0.25}
RATE_ORDER = ["eighth", "beat", "half", "bar"]


@dataclass(frozen=True, slots=True)
class LookSpec:
    """A look a section may use."""

    rhythm: str
    rates: tuple[str, ...]  # fastest first
    groupings: tuple[str, ...]  # grouping kinds from the layout catalog
    bars: tuple[int, int]
    weight: int


LOOKS: dict[Section, list[LookSpec]] = {
    Section.WARMUP: [
        LookSpec("swell", ("bar",), ("all", "k2", "pairs"), (2, 4), 3),
        LookSpec("wave", ("half", "bar"), ("order",), (2, 4), 3),
        LookSpec("spotlight", ("bar",), ("center",), (2, 4), 2),
        LookSpec("hold_play", ("half", "bar"), ("k2", "k3"), (2, 2), 1),
    ],
    Section.GROOVE: [
        LookSpec("call_response", ("beat", "half"), ("k2",), (2, 2), 3),
        LookSpec("canon", ("beat", "half"), ("k3", "pairs"), (2, 2), 3),
        LookSpec("hold_play", ("beat", "half"), ("k2", "k3"), (2, 2), 3),
        LookSpec("chase", ("beat", "half"), ("order",), (2, 4), 2),
        LookSpec("bounce", ("beat", "half"), ("order",), (2, 2), 2),
        LookSpec("unison", ("half", "bar"), ("all", "k2"), (1, 2), 1),
        LookSpec("wave", ("beat", "half"), ("order",), (2, 2), 1),
    ],
    Section.BUILD: [
        LookSpec("build_add", ("half", "bar"), ("order",), (4, 4), 3),
        LookSpec("wave", ("half", "bar"), ("order",), (2, 4), 2),
        LookSpec("chase", ("half", "bar"), ("order",), (2, 4), 2),
        LookSpec("swell", ("bar",), ("all", "k2"), (2, 2), 1),
    ],
    Section.DROP: [LookSpec("impact", ("beat", "half"), ("all",), (1, 1), 1)],
    Section.PEAK: [
        LookSpec("unison", ("beat", "half"), ("all", "k2", "k3"), (1, 2), 3),
        LookSpec("chase", ("eighth", "beat"), ("order",), (2, 2), 3),
        LookSpec("comet", ("eighth", "beat"), ("order",), (2, 2), 2),
        LookSpec("sparkle", ("beat", "half"), ("solo",), (2, 2), 2),
        LookSpec("call_response", ("beat", "half"), ("k2",), (2, 2), 3),
        LookSpec("canon", ("beat", "half"), ("k3", "pairs"), (2, 2), 2),
        LookSpec("hold_play", ("beat", "half"), ("k2", "k3"), (2, 2), 2),
        LookSpec("bounce", ("eighth", "beat"), ("order",), (2, 2), 1),
    ],
    Section.BREAKDOWN: [
        LookSpec("spotlight", ("bar",), ("center",), (2, 4), 3),
        LookSpec("swell", ("bar",), ("k2", "all"), (2, 4), 3),
        LookSpec("wave", ("half", "bar"), ("order",), (2, 4), 2),
    ],
    Section.OUTRO: [
        LookSpec("swell", ("bar",), ("all", "k2"), (4, 4), 3),
        LookSpec("wave", ("bar",), ("order",), (4, 4), 2),
    ],
}

# Frames a rhythm costs per event, per bulb that plays (2 = attack + fade).
_FRAMES_PER_BULB = {"wave": 1, "swell": 1, "spotlight": 1}


# ------------------------------------------------------------------- layout


def default_layout(count: int) -> list[tuple[float, float]]:
    """Bulbs in a row, in configured order, when no layout is given."""
    if count == 1:
        return [(0.5, 0.5)]
    return [(i / (count - 1), 0.5) for i in range(count)]


def _contiguous_splits(order: list[int], parts: int) -> list[list[list[int]]]:
    n = len(order)
    out = []
    for cuts in itertools.combinations(range(1, n), parts - 1):
        bounds = (0, *cuts, n)
        out.append([order[bounds[i] : bounds[i + 1]] for i in range(parts)])
    return out


def _canonical(groups: list[list[int]]) -> tuple[tuple[int, ...], ...]:
    return tuple(sorted(tuple(sorted(g)) for g in groups))


@dataclass
class Layout:
    """Groupings and walking orders derived from where the bulbs are."""

    positions: list[tuple[float, float]]
    groupings: dict[str, list[list[list[int]]]] = field(default_factory=dict)
    orders: list[list[int]] = field(default_factory=list)

    @classmethod
    def build(cls, positions: list[tuple[float, float]]) -> Layout:
        n = len(positions)
        everyone = list(range(n))
        by_x = sorted(everyone, key=lambda i: (positions[i][0], positions[i][1]))
        by_y = sorted(everyone, key=lambda i: (positions[i][1], positions[i][0]))
        cx = sum(p[0] for p in positions) / n
        cy = sum(p[1] for p in positions) / n
        ring = sorted(everyone, key=lambda i: math.atan2(positions[i][1] - cy, positions[i][0] - cx))
        diag = sorted(everyone, key=lambda i: positions[i][0] + positions[i][1])

        def dist(a: int, b: int) -> float:
            return math.dist(positions[a], positions[b])

        seen: set[tuple[tuple[int, ...], ...]] = set()

        def unique(candidates: list[list[list[int]]]) -> list[list[list[int]]]:
            result = []
            for groups in candidates:
                key = _canonical(groups)
                if key not in seen and all(groups):
                    seen.add(key)
                    result.append(groups)
            return result

        k2: list[list[list[int]]] = []
        k3: list[list[list[int]]] = []
        for order in (by_x, by_y, diag):
            k2 += _contiguous_splits(order, 2) if n >= 2 else []
            k3 += _contiguous_splits(order, 3) if n >= 3 else []

        # Nearest-neighbor pairs (+ a single when odd): 2+2+1 for five bulbs.
        pairs: list[list[int]] = []
        free = set(everyone)
        for a, b in sorted(itertools.combinations(everyone, 2), key=lambda ab: dist(*ab)):
            if a in free and b in free:
                pairs.append([a, b])
                free -= {a, b}
        pairs += [[i] for i in sorted(free)]

        center = min(everyone, key=lambda i: math.dist(positions[i], (cx, cy)))
        rest = [i for i in everyone if i != center]

        layout = cls(positions)
        layout.groupings = {
            "all": [[everyone]],
            "solo": [[[i] for i in everyone]],
            "pairs": [pairs] if n >= 3 else [],
            "center": [[[center], rest]] if n >= 2 else [],
            "k2": unique(k2),
            "k3": unique(k3) if n >= 4 else [],
        }
        orders = [by_y, by_x, ring, diag, everyone]
        layout.orders = []
        for order in [*orders, *(o[::-1] for o in orders)]:
            if order not in layout.orders:
                layout.orders.append(order)
        return layout


# ------------------------------------------------------------------- beats


@dataclass(slots=True)
class BeatInfo:
    """One beat, as the choreographer sees it."""

    ts: int
    interval: int
    position: int
    bar_len: int
    bar_start: bool
    next_is_bar: bool
    loudness_db: float | None
    budget_per_s: float = 20.0


@dataclass
class Look:
    """What the bulbs are doing for the next few bars."""

    rhythm: str
    rate: str
    groups: list[list[int]]
    colors: list[RGB]  # per group
    order: list[int]
    bars: int
    bars_done: int = 0
    events: int = 0
    held: bool = False

    def color_of(self, bulb: int) -> RGB:
        for index, members in enumerate(self.groups):
            if bulb in members:
                return self.colors[index]
        return self.colors[0]


# ------------------------------------------------------------------- director

TYPICAL_GROUPS = {"all": 1, "k2": 2, "k3": 3, "center": 2, "order": 2}
RESET_SETTLE_BEATS = 8  # a shared director ignores further resets this soon after one


def grouping_options(layout: Layout, bulbs: int, spec: LookSpec, kind: str) -> list[list[list[int]]]:
    """The concrete groupings a room's layout offers for a look's grouping kind."""
    if kind == "order":
        return [[list(range(bulbs))]] if bulbs >= 2 else []
    options = layout.groupings.get(kind, [])
    if spec.rhythm in ("call_response", "hold_play", "canon"):
        options = [g for g in options if len(g) >= 2]
    if spec.rhythm == "canon":
        options = [g for g in options if len(g) >= 3]
    return options


def bulbs_at_once(spec: LookSpec, bulbs: int, groups: int, energy: float) -> float:
    """How many bulbs a look changes at the same instant (one frame each)."""
    n = bulbs
    k = max(1, groups)
    return {
        "unison": n,
        "impact": n,
        "call_response": n / k,
        "canon": n / k,
        "hold_play": (n - n / k) / max(1, k - 1),
        "chase": 1,
        "bounce": 1,
        "comet": 1,
        "sparkle": 2 if energy > 0.7 else 1,
        "wave": n,
        "swell": n,
        "spotlight": n,
        "build_add": n * 0.7,
    }.get(spec.rhythm, n)


def frames_per_beat(spec: LookSpec, rate: str, bulbs: int, groups: int, energy: float) -> float:
    """Zigbee frames a look costs per beat in a room of ``bulbs`` split into ``groups``."""
    per_bulb = _FRAMES_PER_BULB.get(spec.rhythm, 2)
    return RATES[rate] * bulbs_at_once(spec, bulbs, groups, energy) * per_bulb


# Frames the house can have in the air at once (the governor's in-flight limit).
# Rooms matching each other hit on the same instant, so their looks together
# must not need more than this, or the extra hits arrive late and are dropped.
HOUSE_SLOTS = 6


@dataclass(frozen=True, slots=True)
class LookCue:
    """A look as the director decides it, before any room maps it onto its bulbs."""

    id: int
    spec: LookSpec
    kind: str  # grouping kind ("all", "pairs", "k2", ...)
    rate: str
    bars: int
    seed: int
    pair: tuple[RGB, ...]


@dataclass(frozen=True, slots=True)
class BeatCue:
    """What the director decided for one beat; the same object for every room."""

    ts: int
    section: Section
    look: LookCue
    look_started: bool  # this beat opens the look
    first: bool  # first look since a reset
    bars_done: int
    next_look: LookCue | None  # prepared on this beat for the next bar
    energy: float
    build_bars: int


@dataclass(slots=True)
class RoomInfo:
    """What the director needs to know about a room to pick looks every room can play."""

    layout: Layout
    bulbs: int
    budget_per_s: float


class Director:
    """The song-level half of the lighting designer.

    It reads the music (energy, sections, drops), picks the color pair and the
    look (rhythm, grouping kind, rate, length) bar by bar. Rooms map each look
    onto their own bulbs. A room normally has its own director; rooms set to
    match each other share one while they play in the same group, so they
    change color, rhythm, rate and section on the same beat.
    """

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)
        self._album_hues: list[float] = []
        self.track_remaining_s: float | None = None
        self.rooms: dict[object, RoomInfo] = {}
        self._cue_id = 0
        self._beats_since_reset = RESET_SETTLE_BEATS
        self.reset(force=True)

    # ----------------------------------------------------------- inputs

    def reset(self, *, force: bool = False) -> None:
        """New song or seek: start the story over (once, however many rooms ask)."""
        if not force and self._beats_since_reset < RESET_SETTLE_BEATS and len(self.rooms) > 1:
            return
        self._beats_since_reset = 0
        self.section = Section.WARMUP
        self._section_bars = 0
        self._bar_count = 0
        self._bar_dbs: deque[float] = deque(maxlen=128)  # this track's bars, mean dBFS
        self._bar_db_sum = 0.0
        self._bar_db_beats = 0
        self._energy = 0.5
        self._fast: float | None = None
        self._slow: float | None = None
        self._recent: deque[str] = deque(maxlen=2)
        self._pair: tuple[RGB, ...] = MOOD_PAIRS["moody"][0]
        self._last_pair: tuple[RGB, ...] | None = None
        self._build_bars = 0
        self._look: LookCue | None = None
        self._next: LookCue | None = None
        self._bars_done = 0
        self._cues: deque[BeatCue] = deque(maxlen=8)

    def set_palette(self, colors: list[RGB]) -> None:
        """Album-art colors: used to bias which mood pair is picked."""
        self._album_hues = [hue_of(c) for c in colors[:3]]

    def update_room(self, key: object, info: RoomInfo) -> None:
        self.rooms[key] = info

    def remove_room(self, key: object) -> None:
        self.rooms.pop(key, None)

    @property
    def energy(self) -> float:
        return self._energy

    # ----------------------------------------------------------- the beat

    def cue(self, beat: BeatInfo) -> BeatCue:
        """The decision for this beat. Rooms sharing the director get the same
        object (their clocks agree to well within a quarter beat)."""
        tolerance = min(beat.interval // 4, 80_000)
        for cue in self._cues:
            if abs(cue.ts - beat.ts) <= tolerance:
                return cue
        cue = self._decide(beat)
        self._cues.append(cue)
        return cue

    def _decide(self, beat: BeatInfo) -> BeatCue:
        self._beats_since_reset += 1
        self._update_energy(beat)
        started = False
        first = self._look is None
        if beat.bar_start or self._look is None:
            started = self._on_bar(beat)
        if beat.loudness_db is not None:
            self._bar_db_sum += beat.loudness_db
            self._bar_db_beats += 1
        assert self._look is not None
        prepared = None
        if beat.next_is_bar and self._bars_done + 1 >= self._look.bars and self._next is None:
            self._next = prepared = self._new_cue(beat)
        return BeatCue(
            beat.ts,
            self.section,
            self._look,
            started,
            first,
            self._bars_done,
            prepared,
            self._energy,
            self._build_bars,
        )

    def _on_bar(self, beat: BeatInfo) -> bool:
        first = self._look is None
        if beat.bar_start:
            self._bar_count += 1
        if self._bar_db_beats:
            self._bar_dbs.append(self._bar_db_sum / self._bar_db_beats)
        self._bar_db_sum = 0.0
        self._bar_db_beats = 0
        drop = self._is_drop(beat)
        if drop:
            self._energy = 1.0
            self._fast = beat.loudness_db

        section = self._next_section(beat, bool(drop))
        new_section = section is not self.section or first
        if new_section:
            self.section = section
            self._section_bars = 0
            self._pair = self._pick_pair()
            self._next = None
        self._section_bars += 1
        self._build_bars = self._build_bars + 1 if self.section is Section.BUILD else 0

        if self._look is not None and not first:
            self._bars_done += 1
        if first or new_section or self._look is None or self._bars_done >= self._look.bars:
            self._look = self._next or self._new_cue(beat)
            self._next = None
            self._bars_done = 0
            return True
        return False

    # ----------------------------------------------------------- song structure

    def _update_energy(self, beat: BeatInfo) -> None:
        if beat.loudness_db is None:
            return
        loud = beat.loudness_db
        self._fast = loud if self._fast is None else self._fast + (loud - self._fast) * FAST_ALPHA
        self._slow = loud if self._slow is None else self._slow + (loud - self._slow) * SLOW_ALPHA
        reference = statistics.median(self._bar_dbs) if len(self._bar_dbs) >= 4 else self._slow
        self._energy = min(1.0, max(0.0, 0.5 + (self._fast - reference) / ENERGY_SPAN_DB))

    def _rising(self) -> bool:
        """Bars of steadily rising loudness (3 bars by 2 dB, or 4 by 2 dB slowly): a build."""
        h = list(self._bar_dbs)
        for span in (3, 4):
            w = h[-span:]
            if len(w) == span and all(b >= a - 0.5 for a, b in itertools.pairwise(w)) and w[-1] - w[0] >= 2.0:
                return True
        return False

    def _is_drop(self, beat: BeatInfo) -> bool:
        """The moment a build or breakdown releases into the loudest part."""
        if not beat.bar_start or beat.loudness_db is None or not self._bar_dbs:
            return False
        cur, prev = beat.loudness_db, self._bar_dbs[-1]
        if cur - prev >= DROP_BIG_DB:
            return True
        if self.section is Section.BUILD or self._rising():
            return cur >= max(list(self._bar_dbs)[-4:]) + DROP_AFTER_BUILD_DB
        return self.section is Section.BREAKDOWN and cur - prev >= DROP_DB

    def _next_section(self, beat: BeatInfo, drop: bool) -> Section:  # noqa: PLR0911
        """Song structure. Warm 'peak' looks are earned by a drop, as in a club."""
        remaining = self.track_remaining_s
        if remaining is not None and 0 < remaining < 20:
            return Section.OUTRO
        if drop:
            return Section.DROP
        # Judge the bar by its own downbeat, not the smoothed energy (which lags a bar).
        e = self._energy
        if beat.loudness_db is not None and len(self._bar_dbs) >= 4:
            e = min(1.0, max(0.0, 0.5 + (beat.loudness_db - statistics.median(self._bar_dbs)) / ENERGY_SPAN_DB))
        if self.section is Section.DROP:
            return Section.PEAK
        if self.section is Section.PEAK:
            if e >= 0.6:
                return Section.PEAK
            return Section.BREAKDOWN if e < 0.45 else Section.GROOVE
        if self._rising() and e < 0.9:
            return Section.BUILD
        if self.section is Section.BUILD and self._section_bars < 16 and len(self._bar_dbs) >= 2:
            last, before = self._bar_dbs[-1], self._bar_dbs[-2]
            if last >= before - 1.0:
                return Section.BUILD  # a build holds until it drops or falls away
        if self._section_bars < 2 and self.section is not Section.WARMUP:
            return self.section  # dwell at least two bars
        if self._bar_count <= 8 and e < 0.6:
            return Section.WARMUP
        if e < 0.3:
            return Section.BREAKDOWN if self._bar_count > 16 else Section.WARMUP
        if e >= 0.92 and self._section_bars >= 8 and self._bar_count > 16 and self.section is Section.GROOVE:
            return Section.PEAK  # a sustained high section with no clear drop
        return Section.GROOVE

    def _pick_pair(self) -> tuple[RGB, ...]:
        mood = SECTION_MOOD[self.section]
        if mood == "build":
            choices: list[tuple[RGB, ...]] = [(tone,) for tone in BUILD_TONES]
        else:
            choices = list(MOOD_PAIRS[mood])
        if self.section in (Section.DROP, Section.PEAK) and self._last_pair:
            # The bigger the spectral jump at the drop, the stronger the impact.
            last_hue = hue_of(self._last_pair[0])
            choices.sort(key=lambda p: -hue_distance(hue_of(p[0]), last_hue))
            choices = choices[:2]
        if len(choices) > 1 and self._last_pair in choices:
            choices.remove(self._last_pair)

        def weight(pair: tuple[RGB, ...]) -> float:
            if not self._album_hues:
                return 1.0
            closest = min(hue_distance(hue_of(c), h) for c in pair for h in self._album_hues)
            return 1.0 + 3.0 * (1.0 - closest / 0.5)

        pair = self._rng.choices(choices, weights=[weight(p) for p in choices])[0]
        self._last_pair = pair
        return pair

    # ----------------------------------------------------------- looks

    def _playable(self, spec: LookSpec, kind: str) -> bool:
        """Every room in the show can play this grouping kind, and together they fit the air."""
        if not all(grouping_options(r.layout, r.bulbs, spec, kind) for r in self.rooms.values()):
            return False
        if len(self.rooms) < 2 or spec.rhythm == "impact":
            return True  # one room always fits; the drop's impact is worth a late bulb
        at_once = sum(
            bulbs_at_once(spec, r.bulbs, r.bulbs if kind == "solo" else TYPICAL_GROUPS.get(kind, 2), self._energy)
            for r in self.rooms.values()
        )
        return at_once <= HOUSE_SLOTS

    def _new_cue(self, beat: BeatInfo) -> LookCue:
        section_looks = LOOKS[self.section]
        specs = [s for s in section_looks if s.rhythm not in self._recent] or section_looks
        fallback = (
            LookSpec("chase", ("half", "bar"), ("order",), (2, 2), 1)  # one bulb per room at a time
            if len(self.rooms) > 1
            else LookSpec("unison", ("half", "bar"), ("all",), (1, 2), 1)
        )
        specs = [s for s in specs if any(self._playable(s, g) for g in s.groupings)] or [fallback]
        spec = self._rng.choices(specs, weights=[s.weight for s in specs])[0]
        self._recent.append(spec.rhythm)
        kind = self._rng.choice([g for g in spec.groupings if self._playable(spec, g)] or ["all"])

        def fits(rate: str) -> bool:
            for room in self.rooms.values() or [RoomInfo(Layout.build(default_layout(1)), 1, beat.budget_per_s)]:
                budget = room.budget_per_s * beat.interval / 1e6 * 0.85
                groups = room.bulbs if kind == "solo" else TYPICAL_GROUPS.get(kind, -(-room.bulbs // 2))
                if frames_per_beat(spec, rate, room.bulbs, groups, self._energy) > budget:
                    return False
            return True

        rate = next((r for r in spec.rates if fits(r)), spec.rates[-1])
        while not fits(rate) and rate != "bar":
            rate = RATE_ORDER[RATE_ORDER.index(rate) + 1]
        bars = self._rng.randint(*spec.bars)
        if spec.rhythm == "build_add":
            most = max((r.bulbs for r in self.rooms.values()), default=4)
            bars = max(1, min(4, most - 1))
        self._cue_id += 1
        return LookCue(self._cue_id, spec, kind, rate, bars, self._rng.randrange(1 << 30), self._pair)


class Choreographer:
    """Decides, beat by beat, what every bulb in one room does."""

    def __init__(
        self,
        bulbs: int,
        min_level: int,
        max_level: int,
        seed: int | None = None,
        layout: list[tuple[float, float]] | None = None,
    ) -> None:
        self.bulbs = bulbs
        self.min_level = min_level
        self.max_level = max_level
        self._rng = random.Random(seed)
        self.layout = Layout.build(layout if layout and len(layout) == bulbs else default_layout(bulbs))
        self.volume: float | None = None  # 0..1 from the music player, None = unknown
        self.intensity = 1.0  # the Intensity control, 0.1..1
        self._volume_ref: float | None = None  # the loudest recent setting = "party volume"
        self._own_director = Director(seed)
        self.director = self._own_director
        self.budget_per_s = 20.0
        self.reset()

    # ----------------------------------------------------------- director

    def use_director(self, director: Director | None) -> None:
        """Follow a shared director (rooms matching each other), or our own (None)."""
        director = director or self._own_director
        if director is self.director:
            return
        self.director.remove_room(self)
        self.director = director
        self._room_reset()

    @property
    def section(self) -> Section:
        return self.director.section

    @property
    def track_remaining_s(self) -> float | None:
        return self.director.track_remaining_s

    @track_remaining_s.setter
    def track_remaining_s(self, value: float | None) -> None:
        self.director.track_remaining_s = value

    @property
    def _energy(self) -> float:
        return self.director.energy

    # ----------------------------------------------------------- inputs

    def reset(self) -> None:
        """New song or seek: start the story over."""
        self.director.reset()
        self._room_reset()

    def _room_reset(self) -> None:
        self.look: Look | None = None
        self._look_id: int | None = None
        self._next: Look | None = None
        self._next_id: int | None = None
        self._level: list[int | None] = [None] * self.bulbs
        self._color: list[RGB | None] = [None] * self.bulbs
        self._room_flashes: deque[int] = deque(maxlen=32)
        self._bulb_flash: list[int | None] = [None] * self.bulbs
        self._last_sparkle: set[int] = set()

    def set_palette(self, colors: list[RGB]) -> None:
        """Album-art colors: used to bias which mood pair is picked."""
        self.director.set_palette(colors)

    # ----------------------------------------------------------- levels

    def _profile(self) -> Profile:
        section = self.director.section
        base = PROFILES[section]
        peak = base.peak
        if section is Section.BUILD:  # brighten as the build goes on
            peak = 0.3 + min(1.0, self.director._build_bars / 8) * (base.peak - 0.3)  # noqa: SLF001
        factor = self._volume_factor()
        peak = base.floor + (peak - base.floor) * factor
        quiet = factor < 0.6 or self._energy < 0.3
        attack = max(base.attack_ds, 3) if quiet else base.attack_ds
        return Profile(peak, base.floor, attack, base.instant and not quiet)

    def _volume_factor(self) -> float:
        """How hard the lights may hit: the Intensity control, scaled down further
        when the music is turned down from its recent level.

        Volume is relative, not absolute: a Node at 4 % into a loud amp is party
        volume, so it is compared with the highest recent setting (that memory
        halves over about 45 minutes of music).
        """
        factor = 1.0
        if self.volume is not None:
            ref = max(self.volume, self._volume_ref or 0.0)
            self._volume_ref = ref
            if ref > 0:
                rel = self.volume / ref
                factor = 0.3 + 0.7 * rel**1.5
            else:
                factor = 0.3
        return max(0.1, min(1.0, factor * self.intensity))

    def _lvl(self, fraction: float) -> int:
        return max(1, round(self.min_level + (self.max_level - self.min_level) * fraction))

    # ----------------------------------------------------------- looks

    def _look_from(self, cue: LookCue) -> Look:
        """Map the director's look onto this room's bulbs."""
        rng = random.Random(cue.seed)
        spec = cue.spec
        options = grouping_options(self.layout, self.bulbs, spec, cue.kind) or self.layout.groupings["all"]
        groups = [list(g) for g in rng.choice(options)]
        order = list(rng.choice(self.layout.orders))
        pair = cue.pair
        if spec.rhythm in ("chase", "bounce", "comet", "wave", "build_add") and len(pair) > 1:
            # Walking looks alternate the pair's two colors along the path.
            groups = [order[0::2], order[1::2]]
        colors = [WHITE] if spec.rhythm == "impact" else [pair[i % len(pair)] for i in range(len(groups))]
        return Look(spec.rhythm, cue.rate, groups, colors, order, cue.bars)

    def _send_colors(self, look: Look, effect_us: int, out: list[Command], *, with_hit: bool = False) -> None:
        """Recolor the bulbs. A color landing together with a hit (a section
        change, e.g. the drop's white) must reach the bulb no later than the
        hit, so it gets the hit's priority."""
        priority = PRIO_BEAT if with_hit else PRIO_COLOR
        for bulb in range(self.bulbs):
            rgb = look.color_of(bulb)
            if self._color[bulb] != rgb:
                out.append(Command(effect_us, bulb, None, rgb, COLOR_TRANSITION_DS, priority))
                self._color[bulb] = rgb

    # ----------------------------------------------------------- beat

    def on_beat(self, beat: BeatInfo) -> list[Command]:
        """Commands for one beat (some take effect later within the beat)."""
        out: list[Command] = []
        self.budget_per_s = beat.budget_per_s
        self.director.update_room(self, RoomInfo(self.layout, self.bulbs, beat.budget_per_s))
        cue = self.director.cue(beat)
        if beat.bar_start and self._volume_ref is not None:
            self._volume_ref *= 0.9995  # halves over ~1400 bars (~45 min)

        if cue.look_started or self.look is None or self._look_id != cue.look.id:
            self._start_look(cue, beat, out)
        look = self.look
        assert look is not None
        look.bars_done = cue.bars_done

        for offset in self._event_offsets(look.rate, beat):
            self._play(look, beat, beat.ts + offset, out)
            look.events += 1

        # Prepare the next look's colors half a beat before its first bar.
        # (If the section changes at that bar, the section's own look replaces it.)
        if cue.next_look is not None and self._next_id != cue.next_look.id:
            self._next = self._look_from(cue.next_look)
            self._next_id = cue.next_look.id
            self._send_colors(self._next, beat.ts + beat.interval // 2, out)
        # One command per bulb, attribute and instant: the last one wins
        # (e.g. a new look's floor settle replaced by a hit at the same moment).
        latest: dict[tuple[tuple[int, str], int], Command] = {}
        for cmd in out:
            latest.pop((cmd.key, cmd.effect_us), None)
            latest[(cmd.key, cmd.effect_us)] = cmd
        return list(latest.values())

    def _start_look(self, cue: BeatCue, beat: BeatInfo, out: list[Command]) -> None:
        first = self.look is None
        if self._next is not None and self._next_id == cue.look.id:
            self.look = self._next
        else:
            self.look = self._look_from(cue.look)
        self._look_id = cue.look.id
        self._next = None
        self._next_id = None
        # Recolor just ahead of the first hit (bulbs are near their floor
        # then), so colors and hits don't compete for the same airtime.
        lead = 0 if first or cue.first else min(beat.interval // 2, HIT_COLOR_LEAD_US)
        self._send_colors(self.look, beat.ts - lead, out, with_hit=True)
        if self.look.rhythm not in ("swell", "spotlight", "wave"):
            # New look: bulbs without a part settle to the floor (a hit
            # offered below for the same instant replaces this).
            floor = self._lvl(self._profile().floor)
            for bulb in range(self.bulbs):
                self._set(bulb, floor, beat.ts, HOLD_TRANSITION_DS, out)

    @staticmethod
    def _event_offsets(rate: str, beat: BeatInfo) -> list[int]:
        if rate == "eighth":
            return [0, beat.interval // 2]
        if rate == "beat":
            return [0]
        if rate == "half":
            return [0] if beat.position % 2 == 0 else []
        return [0] if beat.position == 0 else []

    def _event_gap(self, look: Look, beat: BeatInfo) -> int:
        return int(beat.interval / RATES[look.rate])

    # ----------------------------------------------------------- primitives

    def _allowed_flash(self, bulb: int, ts: int) -> bool:
        """Strobe safety: room <= 10 Hz, bulb <= 8 Hz (simultaneous hits share an instant)."""
        last = self._bulb_flash[bulb]
        if last is not None and 0 < ts - last < MIN_BULB_FLASH_GAP_US:
            return False
        return all(t == ts or abs(ts - t) >= MIN_ROOM_FLASH_GAP_US for t in self._room_flashes)

    def _hit(
        self,
        bulb: int,
        ts: int,
        gap_us: int,
        out: list[Command],
        *,
        peak: float | None = None,
        tail: float = 0.85,
    ) -> None:
        """Rise to the peak on the grid, then fade to the floor before the next hit."""
        profile = self._profile()
        if not self._allowed_flash(bulb, ts):
            return
        top = self._lvl(profile.peak if peak is None else peak)
        floor = self._lvl(profile.floor)
        attack = 0 if profile.instant else profile.attack_ds
        out.append(Command(ts, bulb, top, None, attack, PRIO_BEAT))
        hold = max(DECAY_DELAY_US, attack * 100_000)
        fade_us = max(300_000 if not profile.instant else 200_000, int((gap_us - hold) * tail))
        out.append(Command(ts + hold, bulb, floor, None, ds(fade_us), PRIO_LEVEL))
        self._level[bulb] = floor
        self._bulb_flash[bulb] = ts
        if not self._room_flashes or self._room_flashes[-1] != ts:
            self._room_flashes.append(ts)

    def _set(self, bulb: int, level: int, ts: int, transition_ds: int, out: list[Command]) -> None:
        if self._level[bulb] != level:
            out.append(Command(ts, bulb, level, None, transition_ds, PRIO_LEVEL))
            self._level[bulb] = level

    # ----------------------------------------------------------- rhythms

    def _play(self, look: Look, beat: BeatInfo, ts: int, out: list[Command]) -> None:  # noqa: C901, PLR0912, PLR0915
        profile = self._profile()
        gap = self._event_gap(look, beat)
        e = look.events
        groups = look.groups
        order = look.order
        n = len(order)
        rhythm = look.rhythm

        if rhythm in ("unison", "impact"):
            for group in groups:
                for bulb in group:
                    self._hit(bulb, ts, gap, out)

        elif rhythm == "call_response" or rhythm == "canon":
            for bulb in groups[e % len(groups)]:
                self._hit(bulb, ts, gap * len(groups), out)

        elif rhythm == "hold_play":
            holder, *players = groups
            if not look.held:
                mid = self._lvl(profile.floor + (profile.peak - profile.floor) * 0.6)
                for bulb in holder:
                    self._set(bulb, mid, ts, HOLD_TRANSITION_DS, out)
                look.held = True
            if players:
                for bulb in players[e % len(players)]:
                    self._hit(bulb, ts, gap * len(players), out)

        elif rhythm == "chase":
            self._hit(order[e % n], ts, gap * min(n, 2), out)

        elif rhythm == "comet":
            self._hit(order[e % n], ts, gap * 3, out, tail=1.0)

        elif rhythm == "bounce":
            cycle = max(1, 2 * n - 2)
            step = e % cycle
            index = step if step < n else cycle - step
            self._hit(order[index], ts, gap * 2, out)

        elif rhythm == "sparkle":
            hits = 2 if self._energy > 0.7 else 1
            choices = [b for b in range(self.bulbs) if b not in self._last_sparkle] or list(range(self.bulbs))
            picked = self._rng.sample(choices, min(hits, len(choices)))
            for bulb in picked:
                self._hit(bulb, ts, gap, out)
            self._last_sparkle = set(picked)

        elif rhythm == "wave":
            period = max(2, n)  # one full wave travels across the room every n events
            for index, bulb in enumerate(order):
                phase = 0.5 + 0.5 * math.sin(2 * math.pi * (e / period - index / n))
                level = self._lvl(profile.floor + (profile.peak - profile.floor) * phase)
                last = self._level[bulb]
                if last is None or abs(level - last) >= 6:
                    self._set(bulb, level, ts, ds(gap * 0.95), out)

        elif rhythm == "swell":
            for index, group in enumerate(groups):
                up = (e + index) % 2 == 0
                level = self._lvl(profile.peak if up else profile.floor + (profile.peak - profile.floor) * 0.2)
                for bulb in group:
                    self._set(bulb, level, ts, ds(gap * 0.95), out)

        elif rhythm == "spotlight":
            holder, rest = groups[0], (groups[1] if len(groups) > 1 else [])
            for bulb in holder:
                self._set(bulb, self._lvl(profile.peak), ts, HOLD_TRANSITION_DS, out)
            glow = profile.floor + (profile.peak - profile.floor) * (0.35 if e % 2 == 0 else 0.1)
            for bulb in rest:
                self._set(bulb, self._lvl(glow), ts, ds(gap * 0.95), out)

        elif rhythm == "build_add":
            size = min(self.bulbs, 2 + look.bars_done)
            for bulb in order[:size]:
                self._hit(bulb, ts, gap, out)
            floor = self._lvl(profile.floor)
            for bulb in order[size:]:
                self._set(bulb, floor, ts, HOLD_TRANSITION_DS, out)
