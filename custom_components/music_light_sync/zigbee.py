"""Send single ZCL frames to ZHA bulbs (unicast) or a ZHA group (multicast).

Commands go straight to the zigpy cluster objects that ZHA already owns. That
skips the Home Assistant service layer, so the event bus and recorder don't
see a ``call_service`` event for every frame. If ZHA's internals ever move,
it falls back to the public ``zha.issue_zigbee_*_command`` services.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.core import CoreState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import color as color_util

from .const import HUE_GAMUT_C
from .engine import GROUP, Command

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

LEVEL_CLUSTER = 0x0008
COLOR_CLUSTER = 0x0300
MOVE_TO_LEVEL = 0x00
MOVE_TO_COLOR = 0x07
SEND_TIMEOUT_S = 3.0

_ZHA_UNIQUE_ID = re.compile(r"^(?P<ieee>[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){7})-(?P<ep>\d+)")
_GAMUT = color_util.GamutType(
    color_util.XYPoint(*HUE_GAMUT_C[0]),
    color_util.XYPoint(*HUE_GAMUT_C[1]),
    color_util.XYPoint(*HUE_GAMUT_C[2]),
)


class BulbResolveError(HomeAssistantError):
    """A configured light is not a ZHA light."""


class BulbsNotReady(BulbResolveError):
    """A configured light (group) exists but has no state yet (still starting up)."""


@dataclass(frozen=True, slots=True)
class Bulb:
    """A ZHA light endpoint."""

    entity_id: str
    ieee: str
    endpoint: int


def expand_lights(hass: HomeAssistant, entity_ids: list[str]) -> list[str]:
    """Expand light groups (helpers) into their member lights, keeping order."""
    registry = er.async_get(hass)
    result: list[str] = []
    for entity_id in entity_ids:
        entry = registry.async_get(entity_id)
        state = hass.states.get(entity_id)
        if (entry is None or entry.platform != "zha") and state is None:
            # A registered entity without a state is still loading; an unknown one
            # is only worth waiting for while Home Assistant is starting up.
            if entry is not None or hass.state is not CoreState.running:
                raise BulbsNotReady(f"{entity_id} is not available yet")
            raise BulbResolveError(f"{entity_id} does not exist")
        members = state.attributes.get("entity_id") if state else None
        if (entry is None or entry.platform != "zha") and isinstance(members, list | tuple):
            result.extend(expand_lights(hass, list(members)))
        else:
            result.append(entity_id)
    return list(dict.fromkeys(result))


def resolve_bulbs(hass: HomeAssistant, entity_ids: list[str]) -> list[Bulb]:
    """Map light entities to ZHA IEEE address + endpoint."""
    registry = er.async_get(hass)
    bulbs: list[Bulb] = []
    for entity_id in expand_lights(hass, entity_ids):
        entry = registry.async_get(entity_id)
        if entry is None or entry.platform != "zha":
            raise BulbResolveError(f"{entity_id} is not a ZHA light")
        match = _ZHA_UNIQUE_ID.match(entry.unique_id)
        if match is None:
            raise BulbResolveError(f"Cannot read IEEE/endpoint from {entity_id} ({entry.unique_id})")
        bulbs.append(Bulb(entity_id, match["ieee"].lower(), int(match["ep"])))
    if not bulbs:
        raise BulbResolveError("No ZHA lights selected")
    return bulbs


def rgb_to_zcl_xy(rgb: tuple[int, int, int]) -> tuple[int, int]:
    """RGB -> CIE xy clamped to the Hue gamut, as ZCL uint16 values."""
    x, y = color_util.color_RGB_to_xy(rgb[0], rgb[1], rgb[2], _GAMUT)
    return (min(0xFEFF, round(x * 65536)), min(0xFEFF, round(y * 65536)))


class ZhaOutput:
    """Turns engine commands into ZCL frames and reports each frame's round trip."""

    def __init__(self, hass: HomeAssistant, bulbs: list[Bulb], group_id: int | None) -> None:
        self._hass = hass
        self.bulbs = bulbs
        self.group_id = group_id
        self._clusters: dict[tuple[int, int], Any] = {}
        self._direct = True

    def _cluster(self, target: int, cluster_id: int) -> Any:
        key = (target, cluster_id)
        if key in self._clusters:
            return self._clusters[key]
        from homeassistant.components.zha.helpers import get_zha_gateway  # noqa: PLC0415
        from zigpy.types import EUI64  # noqa: PLC0415

        try:
            gateway = get_zha_gateway(self._hass)
        except ValueError as err:  # ZHA not loaded yet (boot) or being reloaded
            raise HomeAssistantError("ZHA is not ready") from err
        if target == GROUP:
            group = gateway.get_group(self.group_id)
            if group is None:
                raise HomeAssistantError(f"ZHA group {self.group_id} not found")
            cluster = group.endpoint[cluster_id]
        else:
            bulb = self.bulbs[target]
            device = gateway.get_device(EUI64.convert(bulb.ieee))
            if device is None:
                raise HomeAssistantError(f"ZHA device {bulb.ieee} not found")
            try:
                cluster = device.async_get_cluster(bulb.endpoint, cluster_id)
            except KeyError as err:
                raise HomeAssistantError(f"{bulb.entity_id} has no cluster 0x{cluster_id:04x}") from err
        self._clusters[key] = cluster
        return cluster

    def invalidate(self) -> None:
        """Forget cached cluster objects (ZHA reloaded)."""
        self._clusters.clear()

    async def async_send(self, cmd: Command) -> float:
        """Send one frame; return its round-trip time in seconds. Raises on failure."""
        if cmd.target == GROUP and self.group_id is None:
            raise HomeAssistantError("Group command without a ZHA group configured")
        start = time.monotonic()
        async with asyncio.timeout(SEND_TIMEOUT_S):
            if self._direct:
                try:
                    await self._send_direct(cmd)
                except AttributeError, ImportError, TypeError:
                    # Possibly a cluster object cached from before a ZHA reload: retry
                    # once with fresh objects before concluding ZHA's internals changed.
                    self.invalidate()
                    try:
                        await self._send_direct(cmd)
                    except (AttributeError, ImportError, TypeError) as err:
                        _LOGGER.warning("Direct ZHA access failed (%r); using ZHA services instead", err)
                        self._direct = False
                        self.invalidate()
                except BaseException:
                    # Radio/delivery failure, or ZHA not (yet) loaded. Drop cached
                    # cluster objects in case ZHA reloaded, and let the caller count it.
                    self.invalidate()
                    raise
            if not self._direct:
                await self._send_service(cmd)
        return time.monotonic() - start

    async def _send_direct(self, cmd: Command) -> None:
        if cmd.level is not None:
            cluster = self._cluster(cmd.target, LEVEL_CLUSTER)
            result = await cluster.move_to_level(level=cmd.level, transition_time=cmd.transition_ds)
        else:
            assert cmd.rgb is not None
            x, y = rgb_to_zcl_xy(cmd.rgb)
            cluster = self._cluster(cmd.target, COLOR_CLUSTER)
            result = await cluster.move_to_color(color_x=x, color_y=y, transition_time=cmd.transition_ds)
        status = getattr(result, "status", None)
        if status is not None and int(status) != 0:
            raise HomeAssistantError(f"Bulb answered with ZCL status {status}")

    async def _send_service(self, cmd: Command) -> None:
        if cmd.level is not None:
            cluster_id, command = LEVEL_CLUSTER, MOVE_TO_LEVEL
            params: dict[str, int] = {"level": cmd.level, "transition_time": cmd.transition_ds}
        else:
            assert cmd.rgb is not None
            x, y = rgb_to_zcl_xy(cmd.rgb)
            cluster_id, command = COLOR_CLUSTER, MOVE_TO_COLOR
            params = {"color_x": x, "color_y": y, "transition_time": cmd.transition_ds}
        if cmd.target == GROUP:
            await self._hass.services.async_call(
                "zha",
                "issue_zigbee_group_command",
                {"group": self.group_id, "cluster_id": cluster_id, "command": command, "args": list(params.values())},
                blocking=True,
            )
            return
        bulb = self.bulbs[cmd.target]
        await self._hass.services.async_call(
            "zha",
            "issue_zigbee_cluster_command",
            {
                "ieee": bulb.ieee,
                "endpoint_id": bulb.endpoint,
                "cluster_id": cluster_id,
                "cluster_type": "in",
                "command": command,
                "command_type": "server",
                "params": params,
            },
            blocking=True,
        )
