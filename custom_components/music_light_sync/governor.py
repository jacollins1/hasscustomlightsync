"""Zigbee traffic governor: rate cap, coalescing, staleness and adaptive backoff.

The Zigbee mesh is shared with every other device in the house (door and
alarm sensors included), so this module keeps the light show from crowding
them out:

* a token bucket caps unicast frames per second;
* group (multicast) frames, which are flooded through the mesh and use the
  coordinator's small broadcast table, get their own minimum spacing;
* a newer command for the same bulb and attribute replaces an unsent older one;
* a command whose moment has passed is dropped rather than sent late;
* the rate backs off multiplicatively when frames come back slow or fail, and
  creeps back up while the network is healthy (AIMD, as in TCP).

All rooms share one coordinator, so the budget (frames per second, frames in
flight, multicast spacing, round-trip estimate) lives in one ``Airtime``
object for the whole house. Each room's ``Governor`` keeps its own queue and
gets a fair share: with two rooms playing, each may use half the frames in
flight and half the rate.

Pure Python; times are passed in so it can be tested deterministically.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .engine import GROUP, STALE_US, Command

# Hue bulbs on ZHA typically answer in 100-250 ms, and a burst of one frame
# per bulb queues briefly, so judge the smoothed round trip, not single frames.
RTT_HIGH_S = 0.45  # smoothed round trip above this: the mesh is struggling
RTT_SPIKE_S = 1.2  # a single frame this slow counts as trouble on its own
RTT_LOW_S = 0.30  # smoothed round trip below this: room to speed up again
BACKOFF_FACTOR = 0.75
BACKOFF_EVERY_S = 2.0  # at most one backoff per window, not one per slow frame
INCREASE_STEP = 1.0
INCREASE_EVERY_S = 2.0
DEFAULT_ONE_WAY_S = 0.06  # until we have measurements


@dataclass(slots=True)
class GovernorStats:
    """Counters exposed on the diagnostics sensor."""

    sent: int = 0
    dropped_stale: int = 0
    coalesced: int = 0
    errors: int = 0


ACTIVE_WINDOW_S = 2.0  # a room that sent within this window counts as playing


class Airtime:
    """The house-wide Zigbee budget, shared by every room's governor."""

    def __init__(
        self,
        *,
        rate_max: float,
        rate_min: float = 2.0,
        burst: int = 12,
        max_inflight: int = 6,
        group_min_interval_s: float = 2.0,
    ) -> None:
        self.rate_max = rate_max
        self.rate_min = min(rate_min, rate_max)
        self.rate = rate_max
        self.burst = burst
        self.max_inflight = max_inflight
        self.group_min_interval_s = group_min_interval_s
        self.rtt_s: float | None = None
        self.inflight = 0
        self.tokens = float(burst)
        # Multicasts: on average one per group_min_interval_s for the whole house
        # (the coordinator's broadcast table is small), but two rooms dropping on
        # the same beat may each send theirs.
        self.group_burst = 2
        self.group_tokens = float(self.group_burst)
        self.last_group_s: float | None = None
        self._last_refill_s: float | None = None
        self._last_increase_s = 0.0
        self._last_backoff_s = -BACKOFF_EVERY_S
        self._active: dict[int, float] = {}

    def set_rate_max(self, rate_max: float) -> None:
        """Change the cap (the lowest budget configured in any room applies)."""
        self.rate_max = rate_max
        self.rate_min = min(self.rate_min, rate_max)
        self.rate = min(self.rate, rate_max)

    def refill(self, now_s: float) -> None:
        if self._last_refill_s is not None and now_s > self._last_refill_s:
            elapsed = now_s - self._last_refill_s
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.group_tokens = min(self.group_burst, self.group_tokens + elapsed / self.group_min_interval_s)
        self._last_refill_s = now_s if self._last_refill_s is None else max(now_s, self._last_refill_s)

    def touch(self, room: int, now_s: float) -> None:
        """A room is playing (it has frames to send)."""
        self._active[room] = now_s

    def rooms_playing(self, now_s: float) -> int:
        for room, seen in list(self._active.items()):
            if now_s - seen > ACTIVE_WINDOW_S:
                del self._active[room]
        return max(1, len(self._active))

    def room_inflight_limit(self, now_s: float) -> int:
        return max(1, -(-self.max_inflight // self.rooms_playing(now_s)))

    def room_rate(self, now_s: float) -> float:
        return self.rate / self.rooms_playing(now_s)

    def _unicast_instead(self, cmd: Command) -> None:
        queue = self._pending.get(cmd.key, [])
        if cmd in queue:
            queue.remove(cmd)
            if not queue:
                self._pending.pop(cmd.key, None)
        if self.bulbs:
            self.offer(
                [
                    Command(cmd.effect_us, bulb, cmd.level, cmd.rgb, cmd.transition_ds, cmd.priority)
                    for bulb in range(self.bulbs)
                ]
            )

    def on_result(self, now_s: float, rtt_s: float | None, ok: bool) -> None:
        """Feed back one frame's outcome: adjusts the RTT estimate and the rate."""
        self.inflight = max(0, self.inflight - 1)
        if ok and rtt_s is not None:
            self.rtt_s = rtt_s if self.rtt_s is None else 0.8 * self.rtt_s + 0.2 * rtt_s
        struggling = (
            not ok
            or (rtt_s is not None and rtt_s > RTT_SPIKE_S)
            or (self.rtt_s is not None and self.rtt_s > RTT_HIGH_S)
        )
        if struggling:
            if now_s - self._last_backoff_s >= BACKOFF_EVERY_S:
                self.rate = max(self.rate_min, self.rate * BACKOFF_FACTOR)
                self._last_backoff_s = now_s
                self._last_increase_s = now_s
        elif self.rtt_s is not None and self.rtt_s < RTT_LOW_S and now_s - self._last_increase_s >= INCREASE_EVERY_S:
            self.rate = min(self.rate_max, self.rate + INCREASE_STEP)
            self._last_increase_s = now_s


class Governor:
    """Decides which of one room's pending commands go out on the air right now."""

    def __init__(
        self,
        *,
        rate_max: float = 20.0,
        rate_min: float = 2.0,
        burst: int = 12,
        max_inflight: int = 6,
        group_min_interval_s: float = 2.0,
        airtime: Airtime | None = None,
    ) -> None:
        self.airtime = airtime or Airtime(
            rate_max=rate_max,
            rate_min=rate_min,
            burst=burst,
            max_inflight=max_inflight,
            group_min_interval_s=group_min_interval_s,
        )
        self.stats = GovernorStats()
        self.inflight = 0  # this room's frames in flight
        self.bulbs = 0  # set by the room: lets a blocked multicast fall back to unicasts
        self._last_group_s: float | None = None  # this room's last multicast
        self._room = id(self)
        self._now_s = 0.0
        # Per bulb+attribute, every queued command in effect-time order. A bulb can
        # legitimately have a flash now and its fade 90 ms later both queued.
        self._pending: dict[tuple[int, str], list[Command]] = {}
        self._sent_at: deque[float] = deque(maxlen=400)

    # The shared budget, seen from this room.

    @property
    def rate(self) -> float:
        """Frames per second this room may use (its share of the house budget)."""
        return self.airtime.room_rate(self._now_s)

    @property
    def rate_limit(self) -> float:
        """The house-wide rate after backoff."""
        return self.airtime.rate

    @property
    def rtt_s(self) -> float | None:
        return self.airtime.rtt_s

    @property
    def _tokens(self) -> float:
        return self.airtime.tokens

    @_tokens.setter
    def _tokens(self, value: float) -> None:
        self.airtime.tokens = value

    def reset(self) -> None:
        """Drop everything queued (stream cleared or session ended)."""
        self._pending.clear()

    @property
    def one_way_s(self) -> float:
        """Estimated time from send to the bulb acting: half a round trip."""
        return DEFAULT_ONE_WAY_S if self.rtt_s is None else self.rtt_s / 2

    def frames_per_s(self, now_s: float, window_s: float = 10.0) -> float:
        """Frames actually sent per second over the recent window."""
        recent = [t for t in self._sent_at if now_s - t <= window_s]
        return round(len(recent) / window_s, 2)

    def offer(self, commands: list[Command]) -> None:
        """Queue commands; one for the same bulb, attribute and moment replaces the old one."""
        for cmd in commands:
            queue = self._pending.setdefault(cmd.key, [])
            for index, queued in enumerate(queue):
                if queued.effect_us == cmd.effect_us:
                    queue[index] = cmd
                    self.stats.coalesced += 1
                    break
            else:
                queue.append(cmd)
                queue.sort(key=lambda c: c.effect_us)

    @property
    def pending_count(self) -> int:
        """Commands queued but not yet sent."""
        return sum(len(q) for q in self._pending.values())

    def take(self, now_s: float, horizon_us: int) -> list[Command]:
        """Pop the commands that may be sent now, highest priority first.

        At equal priority and moment a color goes before a level, so a bulb
        never flashes up in its old color.

        Per bulb and attribute only the latest due command matters: if a flash
        and its fade are both due (the flash could not go out in time), only
        the fade is sent.
        """
        air = self.airtime
        self._now_s = now_s
        air.refill(now_s)

        candidates: list[Command] = []
        for key in list(self._pending):
            queue = self._pending[key]
            due = [c for c in queue if c.effect_us <= horizon_us]
            if not due:
                continue
            latest = due[-1]
            for older in due[:-1]:
                queue.remove(older)
                self.stats.coalesced += 1
            if latest.effect_us < horizon_us - STALE_US:
                queue.remove(latest)
                self.stats.dropped_stale += 1
            else:
                candidates.append(latest)
            if not queue:
                del self._pending[key]
        if not candidates:
            return []
        air.touch(self._room, now_s)
        room_limit = air.room_inflight_limit(now_s)

        ready: list[Command] = []
        for cmd in sorted(candidates, key=lambda c: (-c.priority, c.effect_us, c.rgb is None)):
            if air.inflight >= air.max_inflight or self.inflight >= room_limit or air.tokens < 1:
                break
            if cmd.target == GROUP:
                if air.group_tokens < 1 or (
                    self._last_group_s is not None and now_s - self._last_group_s < air.group_min_interval_s
                ):
                    # The broadcast budget is spent: send this one bulb by bulb.
                    self._unicast_instead(cmd)
                    continue
                air.group_tokens -= 1
                air.last_group_s = self._last_group_s = now_s
            air.tokens -= 1
            air.inflight += 1
            self.inflight += 1
            queue = self._pending.get(cmd.key, [])
            if cmd in queue:
                queue.remove(cmd)
                if not queue:
                    self._pending.pop(cmd.key, None)
            ready.append(cmd)
        for _ in ready:
            self._sent_at.append(now_s)
        self.stats.sent += len(ready)
        return ready

    def _unicast_instead(self, cmd: Command) -> None:
        queue = self._pending.get(cmd.key, [])
        if cmd in queue:
            queue.remove(cmd)
            if not queue:
                self._pending.pop(cmd.key, None)
        if self.bulbs:
            self.offer(
                [
                    Command(cmd.effect_us, bulb, cmd.level, cmd.rgb, cmd.transition_ds, cmd.priority)
                    for bulb in range(self.bulbs)
                ]
            )

    def on_result(self, now_s: float, rtt_s: float | None, ok: bool) -> None:
        """Feed back one frame's outcome: adjusts the RTT estimate and the rate."""
        self.inflight = max(0, self.inflight - 1)
        if not ok:
            self.stats.errors += 1
        self.airtime.on_result(now_s, rtt_s, ok)
