"""Rooms: config flow, model warning, reconfigure, options, renames, migration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import zigpy.types as zt
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.music_light_sync import async_migrate_entry, rename_in_config
from custom_components.music_light_sync.const import (
    CONF_AREA,
    CONF_COLOR_FORMAT,
    CONF_HOST,
    CONF_IDENTITY,
    CONF_LAYOUT,
    CONF_LAYOUT_SOURCE,
    CONF_LIGHTS,
    CONF_MATCH_ROOMS,
    CONF_MAX_BRIGHTNESS,
    CONF_MIN_BRIGHTNESS,
    CONF_NAME,
    CONF_PAUSE_ENTITIES,
    CONF_PORT,
    CONF_SHOW_MODE,
    CONF_ZHA_GROUP_ID,
    DOMAIN,
)

CONNECTION = {CONF_HOST: "192.168.49.10", CONF_PORT: 8927}
SETUP = "custom_components.music_light_sync.async_setup_entry"
CAN_CONNECT = "custom_components.music_light_sync.config_flow._can_connect"


def _bulb(hass: HomeAssistant, object_id: str, ieee: str, model: str, maker: str, area: str | None = None) -> str:
    """A ZHA color light with a device (model) in an area."""
    owner = MockConfigEntry(domain="zha")
    owner.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("zha", ieee)},
        manufacturer=maker,
        model=model,
    )
    if area:
        dr.async_get(hass).async_update_device(device.id, area_id=area)
    entry = er.async_get(hass).async_get_or_create(
        "light", "zha", f"{ieee}-11", suggested_object_id=object_id, device_id=device.id
    )
    hass.states.async_set(entry.entity_id, "on", {"supported_color_modes": ["color_temp", "xy"]})
    return entry.entity_id


def _suggested(result: dict) -> dict:
    """The values a form shows pre-filled."""
    return {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if isinstance(key.description, dict) and "suggested_value" in key.description
    }


async def test_add_room_prefills_name_and_the_areas_color_lights(hass: HomeAssistant) -> None:
    dining = ar.async_get(hass).async_create("Dining Room")
    lights = [
        _bulb(hass, f"dining_{i}", f"00:00:00:00:00:00:00:0{i}", "3RCB01057Z", "Third Reality, Inc", dining.id)
        for i in range(1, 5)
    ]
    _bulb(hass, "kitchen_1", "00:17:88:01:0b:00:00:01", "LCB002", "Signify Netherlands B.V.")  # other room
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert CONF_HOST in result["data_schema"].schema  # first room: asks for Music Assistant
    assert CONF_LIGHTS not in result["data_schema"].schema  # lights come after the room is known
    with patch(CAN_CONNECT, return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_AREA: dining.id, **CONNECTION})
    assert result["step_id"] == "room"
    assert _suggested(result) == {CONF_NAME: "Music Sync - Dining Room", CONF_LIGHTS: sorted(lights)}
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_NAME: "Music Sync - Dining Room", CONF_LIGHTS: sorted(lights)}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Music Sync - Dining Room"
    assert result["data"][CONF_NAME] == "Music Sync - Dining Room"
    assert result["data"][CONF_LIGHTS] == sorted(lights)
    assert result["data"][CONF_AREA] == dining.id
    assert result["data"][CONF_HOST] == CONNECTION[CONF_HOST]
    assert len(result["data"][CONF_IDENTITY]) == 64
    # No floor plan dashboard in this test: positions start as manual.
    assert result["options"] == {CONF_SHOW_MODE: "mirrored", CONF_COLOR_FORMAT: "auto", CONF_LAYOUT_SOURCE: "manual"}


async def test_second_room_reuses_the_connection_and_warns_on_untested_bulbs(hass: HomeAssistant) -> None:
    MockConfigEntry(
        domain=DOMAIN, data={**CONNECTION, CONF_NAME: "Kitchen", CONF_LIGHTS: [], CONF_IDENTITY: "00" * 32}
    ).add_to_hass(hass)
    office = ar.async_get(hass).async_create("Office")
    odd = _bulb(hass, "lamp", "aa:aa:aa:aa:aa:aa:aa:aa", "LCT015", "Signify Netherlands B.V.", office.id)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert CONF_HOST not in result["data_schema"].schema
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_AREA: office.id})
    assert _suggested(result)[CONF_LIGHTS] == [odd]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_NAME: "Desk", CONF_LIGHTS: [odd]})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "unsupported"
    assert "LCT015" in result["description_placeholders"]["bulbs"]
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Desk"
    assert result["data"][CONF_LIGHTS] == [odd]
    assert result["data"][CONF_HOST] == CONNECTION[CONF_HOST]


async def test_add_room_errors(hass: HomeAssistant) -> None:
    empty = ar.async_get(hass).async_create("Garage")
    er.async_get(hass).async_get_or_create("light", "hue", "x", suggested_object_id="wifi")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch(CAN_CONNECT, return_value=False):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_AREA: empty.id, **CONNECTION})
    assert result["errors"] == {"base": "cannot_connect"}
    with patch(CAN_CONNECT, return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_AREA: empty.id, **CONNECTION})
    assert result["step_id"] == "room"
    assert _suggested(result)[CONF_LIGHTS] == []  # nothing in the garage
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_NAME: "G", CONF_LIGHTS: []})
    assert result["errors"] == {CONF_LIGHTS: "no_lights"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "G", CONF_LIGHTS: ["light.wifi"]}
    )
    assert result["errors"] == {CONF_LIGHTS: "not_zha"}


async def test_a_room_can_only_be_added_once(hass: HomeAssistant) -> None:
    kitchen = ar.async_get(hass).async_create("Kitchen")
    MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={**CONNECTION, CONF_NAME: "K", CONF_LIGHTS: [], CONF_IDENTITY: "00" * 32, CONF_AREA: kitchen.id},
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_AREA: kitchen.id})
    assert result["errors"] == {CONF_AREA: "area_taken"}


async def test_reconfigure_changes_lights_and_keeps_the_player(hass: HomeAssistant) -> None:
    kitchen = ar.async_get(hass).async_create("Kitchen")
    old = _bulb(hass, "k1", "00:17:88:01:0b:00:00:01", "LCB002", "Signify Netherlands B.V.", kitchen.id)
    new = _bulb(hass, "k2", "00:17:88:01:0b:00:00:02", "LCA005", "Signify Netherlands B.V.", kitchen.id)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Kitchen Lights",
        data={
            **CONNECTION,
            CONF_NAME: "Kitchen Lights",
            CONF_LIGHTS: [old],
            CONF_IDENTITY: "11" * 32,
            CONF_AREA: kitchen.id,
        },
        options={CONF_LAYOUT: "1, 2", CONF_ZHA_GROUP_ID: 3, CONF_SHOW_MODE: "house"},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_AREA: kitchen.id, CONF_NAME: "Kitchen Lights", CONF_LIGHTS: [old, new]}
        )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_LIGHTS] == [old, new]
    assert entry.data[CONF_IDENTITY] == "11" * 32  # same Music Assistant player
    assert CONF_LAYOUT not in entry.options and CONF_ZHA_GROUP_ID not in entry.options
    assert entry.options[CONF_SHOW_MODE] == "house"
    assert entry.options[CONF_LAYOUT_SOURCE] == "manual"


async def test_options_flow(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={**CONNECTION, CONF_NAME: "K", CONF_LIGHTS: ["light.a"], CONF_IDENTITY: "00" * 32},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    with patch(SETUP, return_value=True):
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
                CONF_SHOW_MODE: "independent",
                CONF_LAYOUT_SOURCE: "manual",
                CONF_COLOR_FORMAT: "xy",
            },
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ZHA_GROUP_ID] is None
    assert entry.options[CONF_MIN_BRIGHTNESS] == 30  # forced below max
    assert entry.options[CONF_SHOW_MODE] == "independent"
    assert entry.options[CONF_COLOR_FORMAT] == "xy"


async def test_options_reject_group_with_extra_members(hass: HomeAssistant) -> None:
    light = _bulb(hass, "a", "00:17:88:01:0b:00:00:01", "LCB002", "Signify Netherlands B.V.")
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, data={**CONNECTION, CONF_NAME: "K", CONF_LIGHTS: [light], CONF_IDENTITY: "00" * 32}
    )
    entry.add_to_hass(hass)

    def member(ieee: str) -> SimpleNamespace:
        return SimpleNamespace(device=SimpleNamespace(ieee=ieee), endpoint_id=11)

    groups = {  # keyed like real ZHA: zigpy Group, whose str() is "0x0002"
        zt.Group(2): SimpleNamespace(name="Kitchen", members=[member("00:17:88:01:0b:00:00:01")]),
        zt.Group(3): SimpleNamespace(
            name="Downstairs", members=[member("00:17:88:01:0b:00:00:01"), member("aa:bb:cc:dd:ee:ff:00:11")]
        ),
    }
    base = {
        "rate_max": 10,
        "group_min_interval": 2,
        CONF_MIN_BRIGHTNESS: 15,
        CONF_MAX_BRIGHTNESS: 100,
        "end_delay": 10,
        "ambient_period": 10,
        CONF_SHOW_MODE: "mirrored",
        CONF_LAYOUT_SOURCE: "manual",
        CONF_COLOR_FORMAT: "auto",
    }
    with (
        patch("homeassistant.components.zha.helpers.get_zha_gateway", return_value=SimpleNamespace(groups=groups)),
        patch(SETUP, return_value=True),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        values = [o["value"] for o in result["data_schema"].schema[CONF_ZHA_GROUP_ID].config["options"]]
        assert values == ["none", "2", "3"]
        result = await hass.config_entries.options.async_configure(result["flow_id"], {**base, CONF_ZHA_GROUP_ID: "3"})
        assert result["errors"] == {CONF_ZHA_GROUP_ID: "group_members"}
        result = await hass.config_entries.options.async_configure(result["flow_id"], {**base, CONF_ZHA_GROUP_ID: "2"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ZHA_GROUP_ID] == 2


def test_rename_in_config() -> None:
    data = {CONF_LIGHTS: ["light.old", "light.b"], CONF_NAME: "K"}
    options = {CONF_PAUSE_ENTITIES: ["automation.x"]}
    new_data, new_options = rename_in_config(data, options, "light.old", "light.new")
    assert new_data == {CONF_LIGHTS: ["light.new", "light.b"], CONF_NAME: "K"} and new_options is None
    new_data, new_options = rename_in_config(data, options, "automation.x", "automation.y")
    assert new_data is None
    assert new_options == {CONF_PAUSE_ENTITIES: ["automation.y"]}
    assert rename_in_config(data, options, "light.other", "light.x") == (None, None)


async def test_rooms_follow_entity_renames(hass: HomeAssistant) -> None:
    """A renamed bulb or paused automation is rewritten in the room's config (skill: Config-Entry blind spots)."""
    light = _bulb(hass, "k1", "00:17:88:01:0b:00:00:01", "LCB002", "Signify Netherlands B.V.")
    automation = er.async_get(hass).async_get_or_create("automation", "automation", "occ", suggested_object_id="occ")
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={**CONNECTION, CONF_NAME: "K", CONF_LIGHTS: [light], CONF_IDENTITY: "00" * 32},
        options={CONF_PAUSE_ENTITIES: [automation.entity_id]},
    )
    entry.add_to_hass(hass)
    from custom_components.music_light_sync import _rename_follower  # noqa: PLC0415

    unsub = hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _rename_follower(hass, entry))
    registry = er.async_get(hass)
    registry.async_update_entity(light, new_entity_id="light.kitchen_overhead_1")
    registry.async_update_entity(automation.entity_id, new_entity_id="automation.kitchen_occupancy")
    await hass.async_block_till_done()
    unsub()
    assert entry.data[CONF_LIGHTS] == ["light.kitchen_overhead_1"]
    assert entry.options[CONF_PAUSE_ENTITIES] == ["automation.kitchen_occupancy"]


async def test_migration_records_the_room_area(hass: HomeAssistant) -> None:
    kitchen = ar.async_get(hass).async_create("Kitchen")
    lights = [
        _bulb(hass, f"k{i}", f"00:17:88:01:0b:00:00:0{i}", "LCB002", "Signify Netherlands B.V.", kitchen.id)
        for i in (1, 2)
    ]
    hass.states.async_set("light.kitchen_group", "on", {"entity_id": lights})
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={**CONNECTION, CONF_NAME: "Kitchen Lights", CONF_LIGHTS: ["light.kitchen_group"], CONF_IDENTITY: "0" * 64},
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.version == 4
    assert entry.data[CONF_AREA] == kitchen.id
    assert entry.data[CONF_IDENTITY] == "0" * 64
    assert entry.options[CONF_SHOW_MODE] == "mirrored"


async def test_migration_to_show_modes_drops_the_music_player(hass: HomeAssistant) -> None:
    for match, mode in ((True, "mirrored"), (False, "independent")):
        entry = MockConfigEntry(
            domain=DOMAIN,
            version=2,
            data={**CONNECTION, CONF_NAME: "K", CONF_LIGHTS: [], CONF_IDENTITY: "00" * 32},
            options={
                CONF_MATCH_ROOMS: match,
                "fallback_player": "media_player.node_a7d8",
                "light_sync_speakers": ["90568298A7D8"],
                CONF_LAYOUT: "1, 2",
            },
        )
        entry.add_to_hass(hass)
        assert await async_migrate_entry(hass, entry)
        assert entry.version == 4
        assert entry.options == {CONF_SHOW_MODE: mode, CONF_LAYOUT: "1, 2"}  # layout source: decided at setup


async def test_new_room_starts_on_the_floor_plan_when_every_bulb_is_pinned(hass: HomeAssistant) -> None:
    from custom_components.music_light_sync.floorplan import Pin  # noqa: PLC0415

    MockConfigEntry(
        domain=DOMAIN, version=3, data={**CONNECTION, CONF_NAME: "K", CONF_LIGHTS: [], CONF_IDENTITY: "00" * 32}
    ).add_to_hass(hass)
    dining = ar.async_get(hass).async_create("Dining Room")
    lights = [
        _bulb(hass, f"dining_{i}", f"00:00:00:00:00:00:00:0{i}", "3RCB01057Z", "Third Reality, Inc", dining.id)
        for i in range(1, 5)
    ]
    devices = [er.async_get(hass).async_get(light).device_id for light in lights]
    pins = {d: Pin(25.0 + i, 30.0, dining.id) for i, d in enumerate(devices)}
    load = "custom_components.music_light_sync.config_flow.async_load_pins"
    for pinned, expected in ((pins, "floor_plan"), (dict(list(pins.items())[:3]), "manual")):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_AREA: dining.id})
        with patch(load, return_value=pinned), patch(SETUP, return_value=True):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_NAME: "Music Sync - Dining Room", CONF_LIGHTS: lights}
            )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["options"][CONF_LAYOUT_SOURCE] == expected
        await hass.config_entries.async_remove(result["result"].entry_id)


async def test_options_refuse_floor_plan_with_unpinned_bulbs(hass: HomeAssistant) -> None:
    light = _bulb(hass, "a", "00:17:88:01:0b:00:00:01", "LCB002", "Signify Netherlands B.V.")
    entry = MockConfigEntry(
        domain=DOMAIN, version=3, data={**CONNECTION, CONF_NAME: "K", CONF_LIGHTS: [light], CONF_IDENTITY: "00" * 32}
    )
    entry.add_to_hass(hass)
    values = {
        CONF_ZHA_GROUP_ID: "none",
        "rate_max": 10,
        "group_min_interval": 2,
        CONF_MIN_BRIGHTNESS: 15,
        CONF_MAX_BRIGHTNESS: 100,
        "end_delay": 10,
        "ambient_period": 10,
        CONF_SHOW_MODE: "house",
        CONF_LAYOUT_SOURCE: "floor_plan",
        CONF_COLOR_FORMAT: "auto",
    }
    with patch("custom_components.music_light_sync.config_flow.async_load_pins", return_value={}):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], values)
    assert result["errors"] == {CONF_LAYOUT_SOURCE: "not_pinned"}
    assert result["description_placeholders"]["unpinned"] == light
