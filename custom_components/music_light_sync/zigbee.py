"""Send single ZCL frames to ZHA bulbs (unicast) or a ZHA group (multicast).

Commands go straight to the zigpy cluster objects that ZHA already owns. That
skips the Home Assistant service layer, so the event bus and recorder don't
see a ``call_service`` event for every frame. If ZHA's internals ever move,
it falls back to the public ``zha.issue_zigbee_*_command`` services.
"""

from __future__ import annotations

import asyncio
import colorsys
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CoreState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
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
MOVE_TO_HUE_SAT = 0x06
MOVE_TO_COLOR = 0x07

COLOR_XY = "xy"
COLOR_HS = "hs"

# Bulbs this integration is built and tested for (Home Assistant model names).
# Anything else is allowed after a warning, as long as it is a ZHA light.
SUPPORTED_MODELS: dict[str, str] = {
    "LCB002": "Philips Hue White and Color Ambiance BR30 (R578096-4)",
    "LCA005": "Philips Hue White and Color Ambiance A19 (562785)",
    "9290022266": "Philips Hue White and Color Ambiance A19 (590851)",
    "3RCB01057Z": "Third Reality color bulb",
}
THIRD_REALITY = "third reality"
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
    model: str | None = None
    manufacturer: str | None = None
    color_format: str = COLOR_XY

    @property
    def is_hue(self) -> bool:
        return (self.manufacturer or "").lower().startswith(("signify", "philips"))

    @property
    def is_third_reality(self) -> bool:
        return THIRD_REALITY in (self.manufacturer or "").lower().replace("thirdreality", "third reality")

    @property
    def supported(self) -> bool:
        return (self.model or "") in SUPPORTED_MODELS


def default_color_format(bulb: Bulb, room_format: str) -> str:
    """How color is sent to a bulb.

    ``room_format`` is the room's Color format option: ``auto`` sends each bulb
    its native format (Hue: CIE xy clamped to its gamut; Third Reality: hue and
    saturation, the same thing its "Native hue/saturation color" quirk does),
    ``hs`` or ``xy`` force one format for every bulb in the room. Choosing per
    frame means nothing on the bulbs has to be switched and switched back.
    """
    if room_format in (COLOR_XY, COLOR_HS):
        return room_format
    return COLOR_HS if bulb.is_third_reality else COLOR_XY


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
        elif (entry is not None and entry.platform == "group") or (
            (entry is None or entry.platform != "zha")
            and state is not None
            and state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)
        ):
            # A group helper reports its members only once it has loaded; at boot
            # it is a restored "unavailable" placeholder without them.
            raise BulbsNotReady(f"{entity_id} has not loaded its members yet")
        else:
            result.append(entity_id)
    return list(dict.fromkeys(result))


def room_area_of(hass: HomeAssistant, light_ids: list[str]) -> str | None:
    """The area most of a room's lights are in (entity area, else device area)."""
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    counts: dict[str, int] = {}
    for entity_id in light_ids:
        entity = entities.async_get(entity_id)
        area = entity.area_id if entity else None
        if area is None and entity is not None and entity.device_id:
            device = devices.async_get(entity.device_id)
            area = device.area_id if device else None
        if area is None:
            state = hass.states.get(entity_id)
            members = state.attributes.get("entity_id") if state else None
            if isinstance(members, list | tuple) and members:
                area = room_area_of(hass, list(members))
        if area:
            counts[area] = counts.get(area, 0) + 1
    return max(counts, key=lambda a: counts[a]) if counts else None


_ZHA_GROUP_UNIQUE_ID = re.compile(r"_zha_group_0x(?P<gid>[0-9a-fA-F]{1,4})$")


def _zha_group_ieees(hass: HomeAssistant, group_id: int) -> set[str]:
    """IEEE addresses of a ZHA group's members (empty if ZHA isn't loaded)."""
    try:
        from homeassistant.components.zha.helpers import get_zha_gateway  # noqa: PLC0415

        group = get_zha_gateway(hass).groups.get(group_id)
    except Exception:  # noqa: BLE001 - ZHA not loaded
        return set()
    if group is None:
        return set()
    return {str(m.device.ieee).lower() for m in group.members}


def _ieees(hass: HomeAssistant, entity_ids: list[str], depth: int = 0) -> set[str]:
    """The Zigbee devices behind lights: ZHA lights, ZHA group lights and light group helpers.

    ZHA group lights have no member list in their state (unlike group helpers),
    so their members come from ZHA itself. Best effort; unknown lights add nothing.
    """
    registry = er.async_get(hass)
    found: set[str] = set()
    if depth > 3:
        return found
    for entity_id in entity_ids:
        entry = registry.async_get(entity_id)
        if entry is not None and entry.platform == "zha":
            if match := _ZHA_UNIQUE_ID.match(entry.unique_id):
                found.add(match["ieee"].lower())
            elif match := _ZHA_GROUP_UNIQUE_ID.search(entry.unique_id):
                found |= _zha_group_ieees(hass, int(match["gid"], 16))
            continue
        state = hass.states.get(entity_id)
        members = state.attributes.get("entity_id") if state else None
        if isinstance(members, list | tuple):
            found |= _ieees(hass, list(members), depth + 1)
    return found


_AL_SUFFIXES = ("_sleep_mode", "_adapt_color", "_adapt_brightness")


def adaptive_lighting_switches(hass: HomeAssistant, light_ids: list[str]) -> list[str]:
    """The Adaptive Lighting main switches that control any of these lights.

    Adaptive Lighting keeps one config entry per switch with its lights in the
    options. Each entry has four switches; the main one is the one whose unique
    id has no "_sleep_mode" / "_adapt_color" / "_adapt_brightness" suffix (its
    unique id is the configured name, which can differ from the entry title).
    Lights are compared by Zigbee device, so an entry that lists the room's ZHA
    group light (or a group helper) matches a room that lists the bulbs.
    """
    ours = _ieees(hass, light_ids)
    registry = er.async_get(hass)
    found: list[str] = []
    for entry in hass.config_entries.async_entries("adaptive_lighting"):
        conf = {**entry.data, **entry.options}
        lights = list(conf.get("lights") or [])
        if not lights or not (_ieees(hass, lights) & ours):
            continue
        for reg in er.async_entries_for_config_entry(registry, entry.entry_id):
            if reg.domain == "switch" and not reg.unique_id.endswith(_AL_SUFFIXES):
                found.append(reg.entity_id)
    return sorted(dict.fromkeys(found))


def resolve_bulbs(hass: HomeAssistant, entity_ids: list[str], room_format: str = "auto") -> list[Bulb]:
    """Map light entities to ZHA IEEE address + endpoint (+ model, color format)."""
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    bulbs: list[Bulb] = []
    for entity_id in expand_lights(hass, entity_ids):
        entry = registry.async_get(entity_id)
        if entry is None or entry.platform != "zha":
            raise BulbResolveError(f"{entity_id} is not a ZHA light")
        match = _ZHA_UNIQUE_ID.match(entry.unique_id)
        if match is None:
            raise BulbResolveError(f"Cannot read IEEE/endpoint from {entity_id} ({entry.unique_id})")
        device = devices.async_get(entry.device_id) if entry.device_id else None
        bulb = Bulb(
            entity_id,
            match["ieee"].lower(),
            int(match["ep"]),
            model=device.model if device else None,
            manufacturer=device.manufacturer if device else None,
        )
        bulbs.append(_with_format(bulb, room_format))
    if not bulbs:
        raise BulbResolveError("No ZHA lights selected")
    return bulbs


def _with_format(bulb: Bulb, room_format: str) -> Bulb:
    return Bulb(
        bulb.entity_id,
        bulb.ieee,
        bulb.endpoint,
        bulb.model,
        bulb.manufacturer,
        default_color_format(bulb, room_format),
    )


def unsupported_models(bulbs: list[Bulb]) -> list[str]:
    """'light.x (model)' for every bulb this integration was not built for."""
    return [f"{b.entity_id} ({b.model or 'unknown model'})" for b in bulbs if not b.supported]


def group_color_format(bulbs: list[Bulb]) -> str | None:
    """The one color format a multicast can use, or None if the room mixes formats."""
    formats = {b.color_format for b in bulbs}
    return formats.pop() if len(formats) == 1 else None


def rgb_to_zcl_xy(rgb: tuple[int, int, int], *, hue_gamut: bool = True) -> tuple[int, int]:
    """RGB -> CIE xy (clamped to the Hue gamut for Hue bulbs), as ZCL uint16 values."""
    x, y = color_util.color_RGB_to_xy(rgb[0], rgb[1], rgb[2], _GAMUT if hue_gamut else None)
    return (min(0xFEFF, round(x * 65536)), min(0xFEFF, round(y * 65536)))


def rgb_to_zcl_hs(rgb: tuple[int, int, int]) -> tuple[int, int]:
    """RGB -> ZCL CurrentHue / CurrentSaturation (0-254)."""
    h, s, _v = colorsys.rgb_to_hsv(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)
    return min(254, round(h * 254)), min(254, round(s * 254))


class ZhaOutput:
    """Turns engine commands into ZCL frames and reports each frame's round trip."""

    def __init__(self, hass: HomeAssistant, bulbs: list[Bulb], group_id: int | None) -> None:
        self._hass = hass
        self.bulbs = bulbs
        self.group_id = group_id
        self.group_format = group_color_format(bulbs) or COLOR_XY
        self._group_hue = all(b.is_hue for b in bulbs)
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

    def _color_frame(self, cmd: Command) -> tuple[int, dict[str, int]]:
        """ZCL command id and parameters for a color change, in the target's format."""
        assert cmd.rgb is not None
        if cmd.target == GROUP:
            fmt, hue_gamut = self.group_format, self._group_hue
        else:
            bulb = self.bulbs[cmd.target]
            fmt, hue_gamut = bulb.color_format, bulb.is_hue
        if fmt == COLOR_HS:
            hue, sat = rgb_to_zcl_hs(cmd.rgb)
            return MOVE_TO_HUE_SAT, {"hue": hue, "saturation": sat, "transition_time": cmd.transition_ds}
        x, y = rgb_to_zcl_xy(cmd.rgb, hue_gamut=hue_gamut)
        return MOVE_TO_COLOR, {"color_x": x, "color_y": y, "transition_time": cmd.transition_ds}

    async def _send_direct(self, cmd: Command) -> None:
        if cmd.level is not None:
            cluster = self._cluster(cmd.target, LEVEL_CLUSTER)
            result = await cluster.move_to_level(level=cmd.level, transition_time=cmd.transition_ds)
        else:
            command, params = self._color_frame(cmd)
            cluster = self._cluster(cmd.target, COLOR_CLUSTER)
            if command == MOVE_TO_HUE_SAT:
                result = await cluster.move_to_hue_and_saturation(**params)
            elif cmd.target != GROUP:
                # Call the stock ZCL command, not a quirk's override: the Third Reality
                # quirk would turn this xy frame back into hue/saturation.
                from zigpy.zcl import Cluster  # noqa: PLC0415

                result = await Cluster.command(cluster, MOVE_TO_COLOR, **params)
            else:
                result = await cluster.move_to_color(**params)
        status = getattr(result, "status", None)
        if status is not None and int(status) != 0:
            raise HomeAssistantError(f"Bulb answered with ZCL status {status}")

    async def _send_service(self, cmd: Command) -> None:
        if cmd.level is not None:
            cluster_id, command = LEVEL_CLUSTER, MOVE_TO_LEVEL
            params: dict[str, int] = {"level": cmd.level, "transition_time": cmd.transition_ds}
        else:
            cluster_id = COLOR_CLUSTER
            command, params = self._color_frame(cmd)
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
