"""End to end: a real Sendspin server (same library MA 2.10.7 uses) -> HA -> ZHA frames.

Only the radio is faked: ZhaOutput.async_send records frames instead of
transmitting them. Everything else is real: the Sendspin handshake, unpaired
approval, clock sync, the visualizer stream, the engine, the governor, the
session take-over (persisted light record, paused automation) and the hand-back.
"""

from __future__ import annotations

import asyncio
import math
import struct
import time
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from aiosendspin.models.visualizer import BeatTiming
from aiosendspin.noise.keys import Identity
from aiosendspin.noise.trust_store import InMemoryServerPairingStore
from aiosendspin.server.audio import AudioFormat
from aiosendspin.server.roles.color.state import Color
from aiosendspin.server.server import SendspinServer
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.music_light_sync.const import (
    CONF_END_DELAY,
    CONF_HOST,
    CONF_IDENTITY,
    CONF_LIGHTS,
    CONF_NAME,
    CONF_PAUSE_ENTITIES,
    CONF_PORT,
    CONF_RATE_MAX,
    DOMAIN,
)
from custom_components.music_light_sync.engine import PRIO_BEAT, Command
from custom_components.music_light_sync.sendspin_link import client_id_for, new_identity_hex
from custom_components.music_light_sync.zigbee import ZhaOutput

RATE = 48_000
FMT = AudioFormat(sample_rate=RATE, bit_depth=16, channels=2)
BEAT_US = 500_000


def kick_pcm(seconds: float) -> bytes:
    out = bytearray()
    for n in range(int(RATE * seconds)):
        phase = (n / RATE) % (BEAT_US / 1e6)
        value = math.exp(-phase * 30) * math.sin(2 * math.pi * 60 * phase) * 0.9
        out += struct.pack("<h", int(value * 30000)) * 2
    return bytes(out)


@pytest.fixture
async def sendspin_server(socket_enabled: None) -> AsyncIterator[tuple[SendspinServer, int]]:
    server = SendspinServer(
        loop=asyncio.get_running_loop(),
        identity=Identity.generate(),
        server_name="test-ma",
        pairing_store=InMemoryServerPairingStore(),
    )
    app = web.Application()
    app.router.add_get(SendspinServer.API_PATH, server.on_client_connect)
    test_server = TestServer(app, host="127.0.0.1")
    await test_server.start_server()
    yield server, test_server.port
    await test_server.close()
    await server.close()


async def _until(predicate, timeout: float = 10.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.05)


def _add_zha_lights(hass: HomeAssistant, count: int) -> list[str]:
    registry = er.async_get(hass)
    ids = []
    for i in range(count):
        entry = registry.async_get_or_create(
            "light", "zha", f"00:17:88:01:0b:00:00:0{i}-11", suggested_object_id=f"hue_bulb_{i}"
        )
        hass.states.async_set(
            entry.entity_id,
            "on",
            {
                "brightness": 180,
                "color_mode": "color_temp",
                "color_temp_kelvin": 3000,
                "supported_color_modes": ["color_temp", "xy"],
            },
        )
        ids.append(entry.entity_id)
    return ids


# The aiohttp test *server* (standing in for Music Assistant) keeps a websocket
# heartbeat timer until its fixture closes; that timer is not ours.
pytestmark = pytest.mark.parametrize("expected_lingering_timers", [True])


async def test_full_show(hass: HomeAssistant, hass_storage: dict, sendspin_server: tuple[SendspinServer, int]) -> None:
    server, port = sendspin_server
    await async_setup_component(hass, "homeassistant", {})
    lights = _add_zha_lights(hass, 3)
    hass.states.async_set("light.kitchen_group", "on", {"entity_id": lights})  # a helper group
    hass.states.async_set("automation.kitchen_occupancy", "on")
    turn_on_calls = async_mock_service(hass, "light", "turn_on")
    auto_off = async_mock_service(hass, "automation", "turn_off")
    auto_on = async_mock_service(hass, "automation", "turn_on")

    identity = new_identity_hex()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: port,
            CONF_NAME: "Kitchen Lights",
            CONF_LIGHTS: ["light.kitchen_group"],
            CONF_IDENTITY: identity,
        },
        options={CONF_END_DELAY: 1, CONF_RATE_MAX: 12, CONF_PAUSE_ENTITIES: ["automation.kitchen_occupancy"]},
    )
    entry.add_to_hass(hass)

    sent: list[tuple[float, Command]] = []

    async def fake_send(self: ZhaOutput, cmd: Command) -> float:
        sent.append((time.monotonic(), cmd))
        await asyncio.sleep(0.03)
        return 0.06

    status = "sensor.kitchen_lights_status"
    with patch.object(ZhaOutput, "async_send", fake_send):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        controller = entry.runtime_data
        assert [b.entity_id for b in controller.bulbs] == lights
        assert controller.bulbs[0].ieee == "00:17:88:01:0b:00:00:00" and controller.bulbs[0].endpoint == 11
        assert hass.states.get("number.kitchen_lights_intensity").state == "100"
        await hass.services.async_call(
            "number", "set_value", {"entity_id": "number.kitchen_lights_intensity", "value": 50}, blocking=True
        )
        assert controller.intensity == 0.5
        await hass.services.async_call(
            "number", "set_value", {"entity_id": "number.kitchen_lights_intensity", "value": 100}, blocking=True
        )

        # Connected, but Music Assistant hasn't approved the new player yet.
        # This test checks beat timing precisely, so use the deterministic effect.
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": "select.kitchen_lights_effect", "option": "beat_breathe"},
            blocking=True,
        )
        client_id = client_id_for(identity)
        await _until(lambda: hass.states.get(status).state == "awaiting_approval")
        await server.trust_unpaired(client_id)  # the one-click "Allow" in MA
        await _until(lambda: hass.states.get(status).state == "idle")

        # Music starts in the group: features + beats + album-art colors.
        group = server.get_client(client_id).group
        stream = group.start_stream()
        group.group_role("color").set_color(Color(primary=(220, 30, 60), accent=(30, 200, 220)))
        pcm = kick_pcm(6)
        step = RATE // 10 * 4
        first_start = None
        beats: list[int] = []
        for offset in range(0, len(pcm), step):
            stream.prepare_audio(pcm[offset : offset + step], FMT)
            start = await stream.commit_audio()
            if first_start is None:
                first_start = start
                beats = [first_start + k * BEAT_US for k in range(12)]
                group.group_role("visualizer").append_beat_schedule(
                    [BeatTiming(ts, k % 4 == 0) for k, ts in enumerate(beats)]
                )
            await stream.sleep_to_limit_buffer(1_500_000)

        await _until(lambda: hass.states.get(status).state == "syncing")
        await asyncio.sleep(4)

        # Take-over happened: pre-show state recorded on disk, automation paused, bulbs primed.
        stored = hass_storage[f"{DOMAIN}.{entry.entry_id}.handback"]["data"]
        assert stored["paused"] == ["automation.kitchen_occupancy"]
        assert stored["lights"][lights[0]] == {
            "state": "on",
            "brightness": 180,
            "color_mode": "color_temp",
            "color_temp_kelvin": 3000,
        }
        assert [c.data["entity_id"] for c in auto_off] == ["automation.kitchen_occupancy"]
        primed = [c for c in turn_on_calls if "rgb_color" in c.data]
        assert {c.data["entity_id"] for c in primed} == set(lights)

        # Frames: level ramps on beats, all within the Zigbee budget.
        levels = [c for _, c in sent if c.level is not None and c.priority == PRIO_BEAT]
        assert levels, "no beat frames were sent"
        link = controller.link
        local_beats = [link.to_play_us(b) for b in beats]
        for cmd in levels:
            nearest = min(abs(cmd.effect_us - b) for b in local_beats)
            assert nearest < 5_000, "level ramps must start on beats"
        span = sent[-1][0] - sent[0][0]
        assert len(sent) / max(span, 1) <= 12 + 6  # budget + initial burst
        # Each frame left ~lead before its beat (send time vs. the beat on the client clock).
        attrs = hass.states.get(status).attributes
        assert attrs["pulse_source"] == "beats"
        assert attrs["bpm"] == 120.0
        assert attrs["palette"][0] != "#ff4020"  # album-art palette, not the fallback

        # Music stops: after end_delay the lights and the automation are handed back.
        for light in lights:  # what ZHA would report after the show's frames
            hass.states.async_set(
                light,
                "on",
                {
                    "brightness": 254,
                    "color_mode": "xy",
                    "xy_color": [0.6, 0.3],
                    "supported_color_modes": ["color_temp", "xy"],
                },
            )
        turn_on_calls.clear()
        group.stop_stream()
        await _until(lambda: hass.states.get(status).state == "idle", timeout=10)
        assert [c.data["entity_id"] for c in auto_on] == ["automation.kitchen_occupancy"]
        restored = {c.data["entity_id"]: c.data for c in turn_on_calls}
        assert set(restored) == set(lights)
        assert all(d.get("color_temp_kelvin") == 3000 and d.get("brightness") == 180 for d in restored.values())
        assert f"{DOMAIN}.{entry.entry_id}.handback" not in hass_storage  # record cleared

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        await _until(lambda: (c := server.get_client(client_id)) is None or not c.is_connected)


async def test_disarm_ends_show_immediately(hass: HomeAssistant, sendspin_server) -> None:
    server, port = sendspin_server
    await async_setup_component(hass, "homeassistant", {})
    lights = _add_zha_lights(hass, 2)
    async_mock_service(hass, "light", "turn_on")
    identity = new_identity_hex()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: port,
            CONF_NAME: "Kitchen Lights",
            CONF_LIGHTS: lights,
            CONF_IDENTITY: identity,
        },
    )
    entry.add_to_hass(hass)

    async def fake_send(self: ZhaOutput, cmd: Command) -> float:
        return 0.05

    with patch.object(ZhaOutput, "async_send", fake_send):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        client_id = client_id_for(identity)
        await _until(lambda: server.get_client(client_id) is not None and server.get_client(client_id).is_connected)
        await server.trust_unpaired(client_id)
        await _until(lambda: hass.states.get("sensor.kitchen_lights_status").state == "idle")
        group = server.get_client(client_id).group
        stream = group.start_stream()
        pcm = kick_pcm(2)
        for offset in range(0, len(pcm), RATE // 5 * 4):
            stream.prepare_audio(pcm[offset : offset + RATE // 5 * 4], FMT)
            await stream.commit_audio()
        await _until(lambda: hass.states.get("sensor.kitchen_lights_status").state == "syncing")

        await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.kitchen_lights_sync"}, blocking=True)
        assert hass.states.get("sensor.kitchen_lights_status").state == "idle"
        assert entry.runtime_data.session is None

        group.stop_stream()
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        await _until(lambda: (c := server.get_client(client_id)) is None or not c.is_connected)


async def test_bulb_that_stops_answering_is_rested(hass: HomeAssistant, sendspin_server) -> None:
    """A bulb switched off at the wall must not be retried on every beat."""
    server, port = sendspin_server
    await async_setup_component(hass, "homeassistant", {})
    lights = _add_zha_lights(hass, 3)
    async_mock_service(hass, "light", "turn_on")
    identity = new_identity_hex()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: port,
            CONF_NAME: "Kitchen Lights",
            CONF_LIGHTS: lights,
            CONF_IDENTITY: identity,
        },
        options={CONF_END_DELAY: 1},
    )
    entry.add_to_hass(hass)
    attempts: list[int] = []

    async def fake_send(self: ZhaOutput, cmd: Command) -> float:
        attempts.append(cmd.target)
        if cmd.target == 1:
            raise TimeoutError("device did not respond")
        return 0.05

    with patch.object(ZhaOutput, "async_send", fake_send):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": "select.kitchen_lights_effect", "option": "beat_breathe"},
            blocking=True,
        )
        client_id = client_id_for(identity)
        await _until(lambda: server.get_client(client_id) is not None and server.get_client(client_id).is_connected)
        await server.trust_unpaired(client_id)
        await _until(lambda: hass.states.get("sensor.kitchen_lights_status").state == "idle")
        group = server.get_client(client_id).group
        stream = group.start_stream()
        pcm = kick_pcm(6)
        first_start = None
        for offset in range(0, len(pcm), RATE // 10 * 4):
            stream.prepare_audio(pcm[offset : offset + RATE // 10 * 4], FMT)
            start = await stream.commit_audio()
            if first_start is None:
                first_start = start
                group.group_role("visualizer").append_beat_schedule(
                    [BeatTiming(first_start + k * BEAT_US, k % 4 == 0) for k in range(12)]
                )
            await stream.sleep_to_limit_buffer(1_500_000)
        await _until(
            lambda: (
                "light.hue_bulb_1"
                in hass.states.get("sensor.kitchen_lights_status").attributes.get("resting_bulbs", [])
            )
        )
        failed_before = attempts.count(1)
        await asyncio.sleep(2)
        assert attempts.count(1) == failed_before  # rested: no more frames to it
        assert attempts.count(0) > failed_before  # the others keep playing

        group.stop_stream()
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        await _until(lambda: (c := server.get_client(client_id)) is None or not c.is_connected)
