"""Shared value types: one Zigbee frame, targets and priorities."""

from __future__ import annotations

from dataclasses import dataclass

RGB = tuple[int, int, int]

GROUP = -1  # Command.target for a single Zigbee group (multicast) frame

PRIO_AMBIENT = 0
PRIO_COLOR = 1
PRIO_LEVEL = 2
PRIO_BEAT = 3

# A command whose effect time is this far in the past when it could be sent is
# no longer worth sending: the moment has gone.
STALE_US = 150_000

MAX_TRANSITION_DS = 600


def ds(us: float) -> int:
    """Microseconds -> ZCL transition time (tenths of a second), clamped."""
    return max(0, min(MAX_TRANSITION_DS, round(us / 100_000)))


@dataclass(slots=True)
class Command:
    """One Zigbee frame: a level OR a color change for a bulb or the group."""

    effect_us: int
    target: int
    level: int | None
    rgb: RGB | None
    transition_ds: int
    priority: int

    @property
    def key(self) -> tuple[int, str]:
        """Bulb + attribute: a later due command with the same key supersedes this one."""
        return (self.target, "level" if self.level is not None else "color")
