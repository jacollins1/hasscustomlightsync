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
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.kitchen_lights_sync"}, blocking=True)
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
