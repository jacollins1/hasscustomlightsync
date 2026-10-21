"""Config and options flow for Music Light Sync."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .const import (
    CONF_AMBIENT_PERIOD,
    CONF_END_DELAY,
    CONF_FALLBACK_PLAYER,
    CONF_GROUP_MIN_INTERVAL,
    CONF_HOST,
    CONF_IDENTITY,
    CONF_LIGHTS,
    CONF_MAX_BRIGHTNESS,
    CONF_MIN_BRIGHTNESS,
    CONF_NAME,
    CONF_PAUSE_ENTITIES,
    CONF_PORT,
    CONF_RATE_MAX,
    CONF_ZHA_GROUP_ID,
    DEFAULT_AMBIENT_PERIOD,
    DEFAULT_END_DELAY,
    DEFAULT_GROUP_MIN_INTERVAL,
    DEFAULT_MAX_BRIGHTNESS,
    DEFAULT_MIN_BRIGHTNESS,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_RATE_MAX,
    DOMAIN,
)
from .sendspin_link import client_id_for, new_identity_hex
from .zigbee import BulbResolveError, resolve_bulbs

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


class MusicLightSyncConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create one light show (one set of bulbs) per entry."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the Music Assistant server and the bulbs."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input[CONF_PORT])
            try:
                bulbs = resolve_bulbs(self.hass, user_input[CONF_LIGHTS])
            except BulbResolveError as err:
                _LOGGER.info("Light validation failed: %s", err)
                errors[CONF_LIGHTS] = "not_zha"
            else:
                if not await _can_connect(host, port):
                    errors["base"] = "cannot_connect"
            if not errors:
                identity = new_identity_hex()
                await self.async_set_unique_id(client_id_for(identity))
                _LOGGER.debug("Configuring %d bulbs: %s", len(bulbs), bulbs)
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_NAME: user_input[CONF_NAME],
                        CONF_LIGHTS: user_input[CONF_LIGHTS],
                        CONF_IDENTITY: identity,
                    },
                )

        defaults = user_input or {}
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME)): str,
                vol.Required(CONF_LIGHTS, default=defaults.get(CONF_LIGHTS, [])): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="light", multiple=True)
                ),
                vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, _music_assistant_host(self.hass))): str,
                vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=65535)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

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


class MusicLightSyncOptionsFlow(OptionsFlow):
    """Tuning and integration with the rest of the house."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Single options page."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = dict(user_input)
            group = data.pop(CONF_ZHA_GROUP_ID, NO_GROUP)
            data[CONF_ZHA_GROUP_ID] = None if group == NO_GROUP else int(str(group), 0)
            if data[CONF_MIN_BRIGHTNESS] >= data[CONF_MAX_BRIGHTNESS]:
                data[CONF_MIN_BRIGHTNESS] = max(1, data[CONF_MAX_BRIGHTNESS] - 10)
            if data[CONF_ZHA_GROUP_ID] is not None and (
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
        player = opts.get(CONF_FALLBACK_PLAYER)
        fields[vol.Optional(CONF_FALLBACK_PLAYER, description={"suggested_value": player} if player else None)] = (
            selector.EntitySelector(selector.EntitySelectorConfig(domain="media_player"))
        )
        return self.async_show_form(step_id="init", data_schema=vol.Schema(fields), errors=errors)
