"""Hand-back safety: races, failures, Home Assistant stopping, leftovers after a crash."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.music_light_sync.const import (
    CONF_END_DELAY,
    CONF_HOST,
    CONF_IDENTITY,
    CONF_LIGHTS,
    CONF_NAME,
    CONF_PAUSE_ENTITIES,
    CONF_PORT,
    DOMAIN,
)
from custom_components.music_light_sync.controller import Session
from custom_components.music_light_sync.sendspin_link import SendspinLink, new_identity_hex


def _lights(hass: HomeAssistant, count: int = 2) -> list[str]:
    registry = er.async_get(hass)
    ids = []
    for i in range(count):
        entry = registry.async_get_or_create(
            "light", "zha", f"00:17:88:01:0b:00:00:1{i}-11", suggested_object_id=f"b{i}"
        )
        hass.states.async_set(
            entry.entity_id, "on", {"brightness": 100, "color_mode": "color_temp", "color_temp_kelvin": 2700}
        )
        ids.append(entry.entity_id)
    return ids


def _entry(hass: HomeAssistant, lights: list[str], pause: list[str]) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: 1,
            CONF_NAME: "Kitchen Lights",
            CONF_LIGHTS: lights,
            CONF_IDENTITY: new_identity_hex(),
        },
        options={CONF_END_DELAY: 0, CONF_PAUSE_ENTITIES: pause},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture(autouse=True)
def _no_network():
    with (
        patch.object(SendspinLink, "async_start", AsyncMock()),
        patch.object(SendspinLink, "async_stop", AsyncMock()),
    ):
        yield


async def _settle(hass: HomeAssistant) -> None:
    for _ in range(5):
        await hass.async_block_till_done()
        await asyncio.sleep(0.05)


async def test_stream_end_during_take_over_still_ends_show(hass: HomeAssistant) -> None:
    lights = _lights(hass)
    hass.states.async_set("automation.a", "on")
    gate = asyncio.Event()
    resumed: list[str] = []

    async def slow_off(call: ServiceCall) -> None:
        await gate.wait()

    async def on(call: ServiceCall) -> None:
        resumed.append(call.data["entity_id"])

    hass.services.async_register("automation", "turn_off", slow_off)
    hass.services.async_register("automation", "turn_on", on)
    async_mock_service(hass, "light", "turn_on")
    entry = _entry(hass, lights, ["automation.a"])
    assert await hass.config_entries.async_setup(entry.entry_id)
    controller = entry.runtime_data

    controller.on_stream_start(True, ("beat",))
    await asyncio.sleep(0.05)  # take-over is now blocked inside automation.turn_off
    controller.on_stream_end()  # play then stop, quickly
    gate.set()
    await _settle(hass)

    assert controller.session is None
    assert resumed == ["automation.a"]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_failed_pause_and_resume_are_isolated_and_remembered(hass: HomeAssistant, hass_storage: dict) -> None:
    lights = _lights(hass)
    for name in ("automation.ok", "automation.broken"):
        hass.states.async_set(name, "on")
    off_calls: list[str] = []
    on_calls: list[str] = []

    async def off(call: ServiceCall) -> None:
        if call.data["entity_id"] == "automation.broken":
            raise HomeAssistantError("offline")
        off_calls.append(call.data["entity_id"])

    async def on(call: ServiceCall) -> None:
        on_calls.append(call.data["entity_id"])
        if call.data["entity_id"] == "automation.ok" and len(on_calls) == 1:
            raise HomeAssistantError("busy")

    hass.services.async_register("automation", "turn_off", off)
    hass.services.async_register("automation", "turn_on", on)
    async_mock_service(hass, "light", "turn_on")
    entry = _entry(hass, lights, ["automation.broken", "automation.ok"])
    assert await hass.config_entries.async_setup(entry.entry_id)
    controller = entry.runtime_data

    controller.on_stream_start(True, ("beat",))
    await _settle(hass)
    assert controller.session is Session.STREAM
    assert off_calls == ["automation.ok"]  # the broken one did not block the rest
    assert controller._paused == ["automation.ok"]  # noqa: SLF001

    controller.on_stream_end()
    await _settle(hass)
    assert controller.session is None
    # Resuming failed once: it stays on disk for the next hand-back.
    key = f"{DOMAIN}.{entry.entry_id}.handback"
    assert hass_storage[key]["data"]["paused"] == ["automation.ok"]
    assert await hass.config_entries.async_unload(entry.entry_id)  # unload retries the hand-back
    assert on_calls == ["automation.ok", "automation.ok"]
    assert key not in hass_storage


async def test_home_assistant_stop_hands_back(hass: HomeAssistant) -> None:
    lights = _lights(hass)
    hass.states.async_set("automation.a", "on")
    async_mock_service(hass, "automation", "turn_off")
    resumed = async_mock_service(hass, "automation", "turn_on")
    light_on = async_mock_service(hass, "light", "turn_on")
    entry = _entry(hass, lights, ["automation.a"])
    assert await hass.config_entries.async_setup(entry.entry_id)
    controller = entry.runtime_data
    controller.on_stream_start(True, ("beat",))
    await _settle(hass)
    assert controller.session is Session.STREAM
    for light in lights:  # show colors
        hass.states.async_set(light, "on", {"brightness": 254, "color_mode": "xy", "xy_color": [0.6, 0.3]})
    light_on.clear()

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await _settle(hass)
    assert [c.data["entity_id"] for c in resumed] == ["automation.a"]
    assert {c.data["entity_id"]: c.data["color_temp_kelvin"] for c in light_on} == dict.fromkeys(lights, 2700)
    assert controller.session is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_leftover_from_crash_is_handed_back_at_start(hass: HomeAssistant, hass_storage: dict) -> None:
    lights = _lights(hass)
    resumed = async_mock_service(hass, "automation", "turn_on")
    light_off = async_mock_service(hass, "light", "turn_off")
    entry = _entry(hass, lights, [])
    key = f"{DOMAIN}.{entry.entry_id}.handback"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"lights": {lights[0]: {"state": "off"}}, "paused": ["automation.a"]},
    }

    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass)
    assert [c.data["entity_id"] for c in resumed] == ["automation.a"]
    assert [c.data["entity_id"] for c in light_off] == [lights[0]]
    assert key not in hass_storage
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_frames_ignored_while_disarmed(hass: HomeAssistant) -> None:
    lights = _lights(hass)
    async_mock_service(hass, "light", "turn_on")
    entry = _entry(hass, lights, [])
    assert await hass.config_entries.async_setup(entry.entry_id)
    controller = entry.runtime_data
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.music_sync_kitchen_lights_sync"}, blocking=True
    )
    controller.on_stream_start(True, ("beat",))
    with patch.object(SendspinLink, "time_synced", True), patch.object(SendspinLink, "to_play_us", lambda s, ts: ts):
        from aiosendspin.models.visualizer import VisualizerFrame

        controller.on_frames([VisualizerFrame(timestamp_us=i, loudness=100) for i in range(50)])
    assert len(controller.engine._loudness) == 0  # noqa: SLF001
    assert controller.session is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_group_helper_not_ready_retries(hass: HomeAssistant) -> None:
    er.async_get(hass).async_get_or_create("light", "group", "kitchen", suggested_object_id="kitchen_group")
    entry = _entry(hass, ["light.kitchen_group"], [])
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_unknown_light_fails_setup_once_running(hass: HomeAssistant) -> None:
    entry = _entry(hass, ["light.deleted_long_ago"], [])
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_unavailable_bulb_stays_recorded_and_is_retried(hass: HomeAssistant, hass_storage: dict) -> None:
    lights = _lights(hass)
    restored = async_mock_service(hass, "light", "turn_on")
    entry = _entry(hass, lights, [])
    assert await hass.config_entries.async_setup(entry.entry_id)
    controller = entry.runtime_data
    controller.on_stream_start(True, ("beat",))
    await _settle(hass)
    hass.states.async_set(lights[1], "unavailable")  # wall switch flipped off mid-show
    restored.clear()
    controller.on_stream_end()
    await _settle(hass)
    assert [c.data["entity_id"] for c in restored] == [lights[0]]
    key = f"{DOMAIN}.{entry.entry_id}.handback"
    assert list(hass_storage[key]["data"]["lights"]) == [lights[1]]
    # The bulb comes back; the retry restores it and clears the record.
    hass.states.async_set(lights[1], "on", {"brightness": 254, "color_mode": "xy", "xy_color": [0.2, 0.2]})
    await controller._async_retry_hand_back()  # noqa: SLF001 - what the 60 s timer runs
    assert [c.data["entity_id"] for c in restored][-1] == lights[1]
    assert key not in hass_storage
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_group_helper_restored_unavailable_at_boot_retries(hass: HomeAssistant) -> None:
    """Seen live: at boot the group helper exists as 'unavailable' with no member list."""
    er.async_get(hass).async_get_or_create("light", "group", "kitchen2", suggested_object_id="kitchen_overhead")
    hass.states.async_set("light.kitchen_overhead", "unavailable", {"restored": True})
    entry = _entry(hass, ["light.kitchen_overhead"], [])
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_rooms_in_one_group_share_a_show_and_the_zigbee_budget(hass: HomeAssistant) -> None:
    from custom_components.music_light_sync.const import CONF_MATCH_ROOMS  # noqa: PLC0415

    kitchen = _entry(hass, _lights(hass, 2), [])
    registry = er.async_get(hass)
    dining_lights = []
    for i in range(2):
        e = registry.async_get_or_create("light", "zha", f"00:17:88:01:0b:00:00:2{i}-11", suggested_object_id=f"d{i}")
        hass.states.async_set(e.entity_id, "on", {"brightness": 100})
        dining_lights.append(e.entity_id)
    dining = _entry(hass, dining_lights, [])
    loner_lights = []
    for i in range(2):
        e = registry.async_get_or_create("light", "zha", f"00:17:88:01:0b:00:00:3{i}-11", suggested_object_id=f"o{i}")
        hass.states.async_set(e.entity_id, "on", {"brightness": 100})
        loner_lights.append(e.entity_id)
    loner = _entry(hass, loner_lights, [])
    hass.config_entries.async_update_entry(loner, options={**loner.options, CONF_MATCH_ROOMS: False})
    assert await hass.config_entries.async_setup(kitchen.entry_id)  # sets up every room of the domain
    await hass.async_block_till_done()
    a, b, c = kitchen.runtime_data, dining.runtime_data, loner.runtime_data
    assert a.governor.airtime is b.governor.airtime is c.governor.airtime  # one coordinator, one budget
    for controller in (a, b, c):
        controller.on_group("living-room-light-sync")
    assert a.engine.choreo.director is b.engine.choreo.director  # matching rooms: one show
    assert c.engine.choreo.director is not a.engine.choreo.director  # this room plays on its own
    assert a.status_attributes()["matched_rooms"] == ["Kitchen Lights"]  # (both test rooms share a name)
    b.on_group("dining-alone")  # dining room ungrouped: back to its own show
    assert a.engine.choreo.director is not b.engine.choreo.director
    for entry in (kitchen, dining, loner):
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert not a.hub.directors


def _recorder(hass: HomeAssistant, domain: str, service: str, effect=None) -> list[str]:
    """Record the entity ids a service is called for (target or data)."""
    calls: list[str] = []

    async def _handler(call: ServiceCall) -> None:
        ids = call.data.get("entity_id")
        calls.extend([ids] if isinstance(ids, str) else list(ids or []))
        if effect:
            effect(call)

    hass.services.async_register(domain, service, _handler)
    return calls


def _adaptive_lighting(hass: HomeAssistant, name: str, lights: list[str]) -> str:
    """An Adaptive Lighting entry (lights in its options) with its four switches."""
    # Title and name differ, as in the house ("Office" entry, switch named "Adaptive Home").
    al = MockConfigEntry(
        domain="adaptive_lighting", title=f"{name} entry", data={"name": name}, options={"lights": lights}
    )
    al.add_to_hass(hass)
    registry = er.async_get(hass)
    main = registry.async_get_or_create(
        "switch", "adaptive_lighting", name, config_entry=al, suggested_object_id=f"{name.lower()}_adaptive_lighting"
    )
    for suffix in ("sleep_mode", "adapt_color", "adapt_brightness"):
        registry.async_get_or_create("switch", "adaptive_lighting", f"{name}_{suffix}", config_entry=al)
    hass.states.async_set(main.entity_id, "on")
    return main.entity_id


async def test_adaptive_lighting_paused_for_the_show_and_restored_after(hass: HomeAssistant) -> None:
    lights = _lights(hass)
    hass.states.async_set("light.kitchen_group", "on", {"entity_id": lights})
    al_switch = _adaptive_lighting(hass, "Kitchen", ["light.kitchen_group"])  # controls the room through a group
    _adaptive_lighting(hass, "Office", ["light.office"])  # unrelated room: untouched
    async_mock_service(hass, "light", "turn_on")
    entry = _entry(hass, lights, [])
    assert await hass.config_entries.async_setup(entry.entry_id)
    controller = entry.runtime_data
    # After setup: loading our switch platform registers the real switch services.
    off, on = _recorder(hass, "switch", "turn_off"), _recorder(hass, "switch", "turn_on")
    assert controller.pause_targets() == [al_switch]  # found through the group, not the Office one
    # (In tests the switch has no loaded entity, so the service layer would drop it from a
    # target; drive the pause/resume through a plain state instead.)
    al_switch = "switch.kitchen_al"
    hass.states.async_set(al_switch, "on")
    patcher = patch(
        "custom_components.music_light_sync.controller.adaptive_lighting_switches", return_value=[al_switch]
    )
    patcher.start()
    controller.on_stream_start(True, ("beat",))
    await _settle(hass)
    assert off == [al_switch]
    controller.on_stream_end()
    await _settle(hass)
    assert controller.session is None
    assert on == [al_switch]
    assert await hass.config_entries.async_unload(entry.entry_id)
    patcher.stop()


async def test_shared_adaptive_lighting_switch_waits_for_the_last_room(hass: HomeAssistant) -> None:
    registry = er.async_get(hass)
    kitchen_lights = _lights(hass)
    table = []
    for i in range(2):
        e = registry.async_get_or_create("light", "zha", f"00:17:88:01:0b:00:00:4{i}-1", suggested_object_id=f"t{i}")
        hass.states.async_set(e.entity_id, "on", {"brightness": 100})
        table.append(e.entity_id)
    assert len(_adaptive_lighting(hass, "Kitchen", [*kitchen_lights, *table]).split(".")) == 2  # one switch, two rooms
    al_switch = "switch.kitchen_al"
    hass.states.async_set(al_switch, "on")
    async_mock_service(hass, "light", "turn_on")
    kitchen, kitchen_table = _entry(hass, kitchen_lights, []), _entry(hass, table, [])
    patcher = patch(
        "custom_components.music_light_sync.controller.adaptive_lighting_switches", return_value=[al_switch]
    )
    patcher.start()
    assert await hass.config_entries.async_setup(kitchen.entry_id)
    await hass.async_block_till_done()
    on = _recorder(hass, "switch", "turn_on")
    _recorder(hass, "switch", "turn_off", lambda call: hass.states.async_set(al_switch, "off"))
    a, b = kitchen.runtime_data, kitchen_table.runtime_data
    a.on_stream_start(True, ("beat",))
    await _settle(hass)
    b.on_stream_start(True, ("beat",))  # joins while the switch is already off
    await _settle(hass)
    a.on_stream_end()
    await _settle(hass)
    assert on == []  # the table is still playing
    b.on_stream_end()
    await _settle(hass)
    assert on == [al_switch]
    for entry in (kitchen, kitchen_table):
        assert await hass.config_entries.async_unload(entry.entry_id)
    patcher.stop()
