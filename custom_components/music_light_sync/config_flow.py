"""Config and options flow for Music Light Sync."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .const import (
    COLOR_FORMAT_AUTO,
    CONF_AMBIENT_PERIOD,
    CONF_AREA,
    CONF_COLOR_FORMAT,
    CONF_END_DELAY,
    CONF_GROUP_MIN_INTERVAL,
    CONF_HOST,
    CONF_IDENTITY,
    CONF_LAYOUT,
    CONF_LAYOUT_SOURCE,
    CONF_LIGHTS,
    CONF_MAX_BRIGHTNESS,
    CONF_MIN_BRIGHTNESS,
    CONF_NAME,
    CONF_PAUSE_ADAPTIVE,
    CONF_PAUSE_ENTITIES,
    CONF_PORT,
    CONF_RATE_MAX,
    CONF_SHOW_MODE,
    CONF_SPEAKERS,
    CONF_ZHA_GROUP_ID,
    DEFAULT_AMBIENT_PERIOD,
    DEFAULT_END_DELAY,
    DEFAULT_GROUP_MIN_INTERVAL,
    DEFAULT_MAX_BRIGHTNESS,
    DEFAULT_MIN_BRIGHTNESS,
    DEFAULT_PAUSE_ADAPTIVE,
    DEFAULT_PORT,
    DEFAULT_RATE_MAX,
    DEFAULT_SHOW_MODE,
    DOMAIN,
    LAYOUT_FLOOR_PLAN,
    LAYOUT_MANUAL,
    SHOW_MODES,
)
from .floorplan import async_load_pins, room_positions
from .sendspin_link import client_id_for, new_identity_hex
from .zigbee import (
    COLOR_HS,
    COLOR_XY,
    SUPPORTED_MODELS,
    BulbResolveError,
    expand_lights,
    resolve_bulbs,
    unsupported_models,
)

_LOGGER = logging.getLogger(__name__)

NO_GROUP = "none"


def _music_assistant_host(hass: HomeAssistant) -> str:
    """Host of the configured Music Assistant server, if any."""
    for entry in hass.config_entries.async_entries("music_assistant"):
        if url := entry.data.get("url"):
            return urlparse(url).hostname or ""
    return ""


async def _can_connect(host: str, port: int) -> bool:
    try:
        async with asyncio.timeout(5):
            _reader, writer = await asyncio.open_connection(host, port)
    except OSError, TimeoutError:
        return False
    writer.close()
    return True


def _area_name(hass: HomeAssistant, area_id: str | None) -> str:
    area = ar.async_get(hass).async_get_area(area_id) if area_id else None
    return area.name if area else ""


def _lights_in_area(hass: HomeAssistant, area_id: str) -> list[str]:
    """ZHA color lights in an area, as a starting point for the room."""
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    found = []
    for entry in er.async_entries_for_area(entities, area_id) + [
        e
        for device in dr.async_entries_for_area(devices, area_id)
        for e in er.async_entries_for_device(entities, device.id)
        if e.area_id is None
    ]:
        if entry.domain != "light" or entry.platform != "zha" or entry.disabled_by:
            continue
        state = hass.states.get(entry.entity_id)
        modes = (state.attributes.get("supported_color_modes") or []) if state else []
        if {"xy", "hs", "rgb"} & set(modes):
            found.append(entry.entity_id)
    return sorted(dict.fromkeys(found))


async def async_layout_source(hass: HomeAssistant, light_ids: list[str]) -> str:
    """Floor plan when every bulb is pinned on it, else manual."""
    try:
        lights = expand_lights(hass, light_ids)
    except BulbResolveError:
        return LAYOUT_MANUAL
    positions, _missing = room_positions(hass, await async_load_pins(hass), lights)
    return LAYOUT_FLOOR_PLAN if positions is not None else LAYOUT_MANUAL


def default_room_name(area_name: str) -> str:
    """ "Dining Room" -> "Music Sync - Dining Room" (entry title and Music Assistant player)."""
    return f"Music Sync - {area_name}"


def _room_schema(defaults: dict[str, Any], *, with_area: bool) -> vol.Schema:
    """Name and lights (pre-filled), plus the area when reconfiguring."""
    fields: dict[Any, Any] = {}
    if with_area:
        fields[vol.Required(CONF_AREA, default=defaults.get(CONF_AREA) or vol.UNDEFINED)] = selector.AreaSelector()
    fields[vol.Required(CONF_NAME, description={"suggested_value": defaults.get(CONF_NAME)})] = str
    fields[vol.Required(CONF_LIGHTS, description={"suggested_value": defaults.get(CONF_LIGHTS)})] = (
        selector.EntitySelector(selector.EntitySelectorConfig(domain="light", multiple=True))
    )
    return vol.Schema(fields)


class MusicLightSyncConfigFlow(ConfigFlow, domain=DOMAIN):
    """One entry per room: its bulbs, its Music Assistant player, its show."""

    VERSION = 3

    def __init__(self) -> None:
        self._room: dict[str, Any] = {}
        self._warned: list[str] = []
        self._layout_source = LAYOUT_MANUAL

    def _connection(self) -> dict[str, Any] | None:
        """Music Assistant host/port of an existing room, so new rooms needn't ask."""
        for entry in self._async_current_entries(include_ignore=False):
            if CONF_HOST in entry.data:
                return {CONF_HOST: entry.data[CONF_HOST], CONF_PORT: entry.data.get(CONF_PORT, DEFAULT_PORT)}
        return None

    def _area_taken(self, area: str, entry_id: str | None = None) -> bool:
        return any(
            e.data.get(CONF_AREA) == area and e.entry_id != entry_id
            for e in self._async_current_entries(include_ignore=False)
        )

    def _check_room(self, area: str, user_input: dict[str, Any], errors: dict[str, str]) -> dict[str, Any] | None:
        """Validate the room's name and bulbs."""
        lights = list(user_input.get(CONF_LIGHTS) or [])
        name = (user_input.get(CONF_NAME) or "").strip() or default_room_name(_area_name(self.hass, area))
        if not lights:
            errors[CONF_LIGHTS] = "no_lights"
            return None
        try:
            bulbs = resolve_bulbs(self.hass, lights)
        except BulbResolveError as err:
            _LOGGER.info("Light validation failed: %s", err)
            errors[CONF_LIGHTS] = "not_zha"
            return None
        self._warned = unsupported_models(bulbs)
        return {CONF_AREA: area, CONF_NAME: name, CONF_LIGHTS: lights}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 1: the room (area), plus the Music Assistant server for the first room."""
        errors: dict[str, str] = {}
        connection = self._connection()
        if user_input is not None:
            area = user_input[CONF_AREA]
            chosen = connection
            if self._area_taken(area):
                errors[CONF_AREA] = "area_taken"
            elif chosen is None:
                chosen = {CONF_HOST: user_input[CONF_HOST].strip(), CONF_PORT: int(user_input[CONF_PORT])}
                if not await _can_connect(chosen[CONF_HOST], chosen[CONF_PORT]):
                    errors["base"] = "cannot_connect"
            if not errors and chosen is not None:
                self._room = {CONF_AREA: area, **chosen}
                return await self.async_step_room()

        defaults = user_input or {}
        fields: dict[Any, Any] = {
            vol.Required(CONF_AREA, default=defaults.get(CONF_AREA) or vol.UNDEFINED): selector.AreaSelector()
        }
        if connection is None:
            fields[vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, _music_assistant_host(self.hass)))] = str
            fields[vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT))] = vol.All(
                vol.Coerce(int), vol.Range(min=1, max=65535)
            )
        return self.async_show_form(step_id="user", data_schema=vol.Schema(fields), errors=errors)

    async def async_step_room(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 2: name and lights, pre-filled from the room."""
        area = self._room[CONF_AREA]
        errors: dict[str, str] = {}
        if user_input is not None:
            room = self._check_room(area, user_input, errors)
            if room is not None:
                self._room.update(room)
                if self._warned:
                    return await self.async_step_unsupported()
                return await self._async_create_room()
        area_name = _area_name(self.hass, area)
        defaults = user_input or {
            CONF_NAME: default_room_name(area_name),
            CONF_LIGHTS: _lights_in_area(self.hass, area),
        }
        return self.async_show_form(
            step_id="room",
            data_schema=_room_schema(defaults, with_area=False),
            errors=errors,
            description_placeholders={"room": area_name},
        )

    async def async_step_unsupported(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Warn about bulbs the integration wasn't built for; allow continuing."""
        if user_input is not None:
            if self.source == SOURCE_RECONFIGURE:
                return self._async_apply_reconfigure()
            return await self._async_create_room()
        return self.async_show_form(
            step_id="unsupported",
            data_schema=vol.Schema({}),
            description_placeholders={
                "bulbs": "\n".join(f"- {b}" for b in self._warned),
                "supported": ", ".join(SUPPORTED_MODELS),
            },
        )

    async def _async_create_room(self) -> ConfigFlowResult:
        identity = new_identity_hex()
        await self.async_set_unique_id(client_id_for(identity))
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=self._room[CONF_NAME],
            data={**self._room, CONF_IDENTITY: identity},
            options={
                CONF_SHOW_MODE: DEFAULT_SHOW_MODE,
                CONF_COLOR_FORMAT: COLOR_FORMAT_AUTO,
                CONF_LAYOUT_SOURCE: await async_layout_source(self.hass, self._room[CONF_LIGHTS]),
            },
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change a room's area, name or lights (keeps its Music Assistant player)."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            area = user_input[CONF_AREA]
            if self._area_taken(area, entry.entry_id):
                errors[CONF_AREA] = "area_taken"
            elif (room := self._check_room(area, user_input, errors)) is not None:
                self._room = room
                self._layout_source = await async_layout_source(self.hass, room[CONF_LIGHTS])
                if self._warned:
                    return await self.async_step_unsupported()
                return self._async_apply_reconfigure()
        defaults = user_input or {
            CONF_AREA: entry.data.get(CONF_AREA),
            CONF_NAME: entry.data.get(CONF_NAME),
            CONF_LIGHTS: entry.data.get(CONF_LIGHTS),
        }
        return self.async_show_form(
            step_id="reconfigure", data_schema=_room_schema(defaults, with_area=True), errors=errors
        )

    def _async_apply_reconfigure(self) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        options = dict(entry.options)
        if self._room[CONF_LIGHTS] != entry.data.get(CONF_LIGHTS):
            options.pop(CONF_LAYOUT, None)  # positions belonged to the old bulbs
            options.pop(CONF_ZHA_GROUP_ID, None)
            options[CONF_LAYOUT_SOURCE] = self._layout_source
        return self.async_update_reload_and_abort(
            entry, title=self._room[CONF_NAME], data_updates=self._room, options=options
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Options."""
        return MusicLightSyncOptionsFlow()


def _zha_groups(hass: HomeAssistant) -> list[selector.SelectOptionDict]:
    options = [selector.SelectOptionDict(value=NO_GROUP, label="None (unicast only)")]
    try:
        from homeassistant.components.zha.helpers import get_zha_gateway  # noqa: PLC0415

        groups = get_zha_gateway(hass).groups
    except Exception:  # noqa: BLE001 - ZHA not loaded: offer no groups
        return options
    for group_id, group in sorted(groups.items()):
        # ZHA keys groups by zigpy's Group type, whose str() is hex ("0x0003").
        gid = int(group_id)
        options.append(selector.SelectOptionDict(value=str(gid), label=f"{group.name} (0x{gid:04x})"))
    return options


def _group_mismatch(hass: HomeAssistant, group_id: int, light_ids: list[str]) -> str | None:
    """Explain why a ZHA group is unsafe to use, or None if it holds exactly our bulbs.

    A multicast reaches every member of the group, but only the configured
    bulbs are recorded and restored, so any extra member would be left in show
    colors and a missing one would not follow bar changes.
    """
    try:
        from homeassistant.components.zha.helpers import get_zha_gateway  # noqa: PLC0415

        group = get_zha_gateway(hass).groups.get(group_id)
    except Exception:  # noqa: BLE001 - ZHA not loaded: cannot check, allow it
        return None
    if group is None:
        return "missing"
    try:
        members = {(str(m.device.ieee).lower(), m.endpoint_id) for m in group.members}
    except Exception:  # noqa: BLE001 - group references a device ZHA no longer knows
        return "members"
    try:
        bulbs = {(b.ieee, b.endpoint) for b in resolve_bulbs(hass, light_ids)}
    except BulbResolveError:
        return None
    return None if members == bulbs else "members"


def _layout_error(hass: HomeAssistant, text: str | None, light_ids: list[str]) -> str | None:
    """Validate the bulb layout text against the number of configured bulbs."""
    if not text or not text.strip():
        return None
    from .controller import parse_layout  # noqa: PLC0415

    try:
        ids = [b.entity_id for b in resolve_bulbs(hass, light_ids)]
    except BulbResolveError:
        return None
    try:
        parse_layout(text, ids)
    except ValueError:
        return "layout_invalid"
    return None


class MusicLightSyncOptionsFlow(OptionsFlow):
    """Tuning and integration with the rest of the house."""

    def _speakers(self) -> dict[str, str]:
        """BluOS speakers in Music Assistant (id -> name), as last seen."""
        from .hub import async_get_hub  # noqa: PLC0415

        groups = async_get_hub(self.hass).groups
        return dict(sorted((groups.speakers if groups else {}).items(), key=lambda kv: kv[1]))

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Single options page."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"unpinned": ""}
        if user_input is not None:
            data = dict(user_input)
            group = data.pop(CONF_ZHA_GROUP_ID, NO_GROUP)
            data[CONF_ZHA_GROUP_ID] = None if group == NO_GROUP else int(str(group), 0)
            speakers = data.pop(CONF_SPEAKERS, None)
            known = set(self._speakers())
            # Every speaker ticked = "all", so speakers added later are joined too.
            data[CONF_SPEAKERS] = None if speakers is None or (known and set(speakers) >= known) else list(speakers)
            if data[CONF_MIN_BRIGHTNESS] >= data[CONF_MAX_BRIGHTNESS]:
                data[CONF_MIN_BRIGHTNESS] = max(1, data[CONF_MAX_BRIGHTNESS] - 10)
            layout_error = _layout_error(self.hass, data.get(CONF_LAYOUT), self.config_entry.data[CONF_LIGHTS])
            unpinned = await self._unpinned() if data.get(CONF_LAYOUT_SOURCE) == LAYOUT_FLOOR_PLAN else []
            if unpinned:
                errors[CONF_LAYOUT_SOURCE] = "not_pinned"
                placeholders["unpinned"] = ", ".join(unpinned)
            elif layout_error:
                errors[CONF_LAYOUT] = layout_error
            elif data[CONF_ZHA_GROUP_ID] is not None and (
                problem := _group_mismatch(self.hass, data[CONF_ZHA_GROUP_ID], self.config_entry.data[CONF_LIGHTS])
            ):
                errors[CONF_ZHA_GROUP_ID] = f"group_{problem}"
            else:
                return self.async_create_entry(data=data)

        opts = {**self.config_entry.options, **(user_input or {})}
        group = opts.get(CONF_ZHA_GROUP_ID)
        fields: dict[Any, Any] = {
            vol.Required(CONF_ZHA_GROUP_ID, default=NO_GROUP if group is None else str(group)): selector.SelectSelector(
                selector.SelectSelectorConfig(options=_zha_groups(self.hass), mode=selector.SelectSelectorMode.DROPDOWN)
            ),
            vol.Required(CONF_RATE_MAX, default=opts.get(CONF_RATE_MAX, DEFAULT_RATE_MAX)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=2, max=40, step=0.5, unit_of_measurement="frames/s")
            ),
            vol.Required(
                CONF_GROUP_MIN_INTERVAL, default=opts.get(CONF_GROUP_MIN_INTERVAL, DEFAULT_GROUP_MIN_INTERVAL)
            ): selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=10, step=0.5, unit_of_measurement="s")),
            vol.Required(
                CONF_MIN_BRIGHTNESS, default=opts.get(CONF_MIN_BRIGHTNESS, DEFAULT_MIN_BRIGHTNESS)
            ): selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=95, step=1, unit_of_measurement="%")),
            vol.Required(
                CONF_MAX_BRIGHTNESS, default=opts.get(CONF_MAX_BRIGHTNESS, DEFAULT_MAX_BRIGHTNESS)
            ): selector.NumberSelector(selector.NumberSelectorConfig(min=10, max=100, step=1, unit_of_measurement="%")),
            vol.Optional(CONF_PAUSE_ENTITIES, default=opts.get(CONF_PAUSE_ENTITIES, [])): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["automation", "switch", "input_boolean"], multiple=True)
            ),
            vol.Required(CONF_END_DELAY, default=opts.get(CONF_END_DELAY, DEFAULT_END_DELAY)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=120, step=1, unit_of_measurement="s")
            ),
            vol.Required(
                CONF_AMBIENT_PERIOD, default=opts.get(CONF_AMBIENT_PERIOD, DEFAULT_AMBIENT_PERIOD)
            ): selector.NumberSelector(selector.NumberSelectorConfig(min=3, max=120, step=1, unit_of_measurement="s")),
        }
        fields[vol.Required(CONF_PAUSE_ADAPTIVE, default=opts.get(CONF_PAUSE_ADAPTIVE, DEFAULT_PAUSE_ADAPTIVE))] = (
            selector.BooleanSelector()
        )
        fields[vol.Required(CONF_SHOW_MODE, default=opts.get(CONF_SHOW_MODE, DEFAULT_SHOW_MODE))] = (
            selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(SHOW_MODES), translation_key=CONF_SHOW_MODE, mode=selector.SelectSelectorMode.LIST
                )
            )
        )
        fields[vol.Required(CONF_COLOR_FORMAT, default=opts.get(CONF_COLOR_FORMAT, COLOR_FORMAT_AUTO))] = (
            selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[COLOR_FORMAT_AUTO, COLOR_HS, COLOR_XY],
                    translation_key=CONF_COLOR_FORMAT,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        )
        speakers = self._speakers()
        if speakers:
            chosen = opts.get(CONF_SPEAKERS)
            fields[vol.Optional(CONF_SPEAKERS, default=list(speakers) if chosen is None else list(chosen))] = (
                selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[selector.SelectOptionDict(value=k, label=v) for k, v in speakers.items()],
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                )
            )
        fields[vol.Required(CONF_LAYOUT_SOURCE, default=opts.get(CONF_LAYOUT_SOURCE, LAYOUT_MANUAL))] = (
            selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[LAYOUT_FLOOR_PLAN, LAYOUT_MANUAL],
                    translation_key=CONF_LAYOUT_SOURCE,
                    mode=selector.SelectSelectorMode.LIST,
                )
            )
        )
        layout = opts.get(CONF_LAYOUT)
        fields[vol.Optional(CONF_LAYOUT, description={"suggested_value": layout} if layout else None)] = (
            selector.TextSelector(selector.TextSelectorConfig(multiline=True))
        )
        return self.async_show_form(
            step_id="init", data_schema=vol.Schema(fields), errors=errors, description_placeholders=placeholders
        )

    async def _unpinned(self) -> list[str]:
        try:
            lights = expand_lights(self.hass, list(self.config_entry.data[CONF_LIGHTS]))
        except BulbResolveError:
            return []
        _positions, missing = room_positions(self.hass, await async_load_pins(self.hass), lights)
        return missing
