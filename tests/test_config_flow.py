"""Config and options flow."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.music_light_sync.const import (
    CONF_FALLBACK_PLAYER,
    CONF_HOST,
    CONF_IDENTITY,
    CONF_LIGHTS,
    CONF_MAX_BRIGHTNESS,
    CONF_MIN_BRIGHTNESS,
    CONF_NAME,
    CONF_PORT,
    CONF_ZHA_GROUP_ID,
    DOMAIN,
)

USER_INPUT = {CONF_NAME: "Kitchen Lights", CONF_HOST: "192.168.49.10", CONF_PORT: 8927, CONF_LIGHTS: ["light.a"]}


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    er.async_get(hass).async_get_or_create("light", "zha", "00:17:88:01:0b:00:00:01-11", suggested_object_id="a")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    with (
        patch("custom_components.music_light_sync.config_flow._can_connect", return_value=True),
        patch("custom_components.music_light_sync.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Kitchen Lights"
    assert len(result["data"][CONF_IDENTITY]) == 64
    assert result["data"][CONF_LIGHTS] == ["light.a"]


async def test_user_flow_errors(hass: HomeAssistant) -> None:
    er.async_get(hass).async_get_or_create("light", "hue", "x", suggested_object_id="a")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch("custom_components.music_light_sync.config_flow._can_connect", return_value=False):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {CONF_LIGHTS: "not_zha"}


async def test_cannot_connect(hass: HomeAssistant) -> None:
    er.async_get(hass).async_get_or_create("light", "zha", "00:17:88:01:0b:00:00:01-11", suggested_object_id="a")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch("custom_components.music_light_sync.config_flow._can_connect", return_value=False):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_options_flow(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={**USER_INPUT, CONF_IDENTITY: "00" * 32})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    with patch("custom_components.music_light_sync.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_ZHA_GROUP_ID: "none",
                "rate_max": 10,
                "group_min_interval": 2,
                CONF_MIN_BRIGHTNESS: 50,
                CONF_MAX_BRIGHTNESS: 40,
                "end_delay": 10,
                "ambient_period": 10,
                CONF_FALLBACK_PLAYER: "media_player.node",
            },
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ZHA_GROUP_ID] is None
    assert entry.options[CONF_MIN_BRIGHTNESS] == 30  # forced below max
    assert entry.options[CONF_FALLBACK_PLAYER] == "media_player.node"


async def test_options_reject_group_with_extra_members(hass: HomeAssistant) -> None:
    from types import SimpleNamespace

    er.async_get(hass).async_get_or_create("light", "zha", "00:17:88:01:0b:00:00:01-11", suggested_object_id="a")
    entry = MockConfigEntry(domain=DOMAIN, data={**USER_INPUT, CONF_IDENTITY: "00" * 32})
    entry.add_to_hass(hass)

    def member(ieee: str) -> SimpleNamespace:
        return SimpleNamespace(device=SimpleNamespace(ieee=ieee), endpoint_id=11)

    import zigpy.types as zt

    groups = {  # keyed like real ZHA: zigpy Group, whose str() is "0x0002"
        zt.Group(2): SimpleNamespace(name="Kitchen", members=[member("00:17:88:01:0b:00:00:01")]),
        zt.Group(3): SimpleNamespace(
            name="Downstairs", members=[member("00:17:88:01:0b:00:00:01"), member("aa:bb:cc:dd:ee:ff:00:11")]
        ),
    }
    gateway = SimpleNamespace(groups=groups)
    base = {
        "rate_max": 10,
        "group_min_interval": 2,
        CONF_MIN_BRIGHTNESS: 15,
        CONF_MAX_BRIGHTNESS: 100,
        "end_delay": 10,
        "ambient_period": 10,
    }
    with (
        patch("homeassistant.components.zha.helpers.get_zha_gateway", return_value=gateway),
        patch("custom_components.music_light_sync.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        values = [o["value"] for o in result["data_schema"].schema[CONF_ZHA_GROUP_ID].config["options"]]
        assert values == ["none", "2", "3"]
        result = await hass.config_entries.options.async_configure(result["flow_id"], {**base, CONF_ZHA_GROUP_ID: "3"})
        assert result["errors"] == {CONF_ZHA_GROUP_ID: "group_members"}
        result = await hass.config_entries.options.async_configure(result["flow_id"], {**base, CONF_ZHA_GROUP_ID: "2"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ZHA_GROUP_ID] == 2
