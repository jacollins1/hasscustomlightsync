"""ZCL frames against real zigpy cluster objects, and bulb resolution."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import zigpy.device
import zigpy.endpoint
import zigpy.types as t
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from zigpy.zcl import foundation

from custom_components.music_light_sync.engine import GROUP, Command
from custom_components.music_light_sync.zigbee import (
    Bulb,
    BulbResolveError,
    ZhaOutput,
    resolve_bulbs,
    rgb_to_zcl_xy,
)

IEEE = "00:17:88:01:0b:00:00:01"


def _zigpy_bulb() -> tuple[zigpy.device.Device, list[tuple[int, int, bytes]]]:
    app = MagicMock()
    device = zigpy.device.Device(app, t.EUI64.convert(IEEE), 0x1234)
    endpoint = device.add_endpoint(11)
    endpoint.profile_id = 260
    endpoint.add_input_cluster(0x0008)
    endpoint.add_input_cluster(0x0300)
    frames: list[tuple[int, int, bytes]] = []

    async def fake_request(self, cluster, sequence, data, command_id=0, **kwargs):
        frames.append((cluster, command_id, data))
        return foundation.GENERAL_COMMANDS[foundation.GeneralCommand.Default_Response].schema(
            command_id=command_id, status=foundation.Status.SUCCESS
        )

    patcher = patch.object(zigpy.endpoint.Endpoint, "request", fake_request)
    patcher.start()
    return device, frames, patcher


def _payload(cluster, data: bytes) -> bytes:
    _hdr, payload = foundation.ZCLHeader.deserialize(data)
    return payload


async def test_level_and_color_frames(hass: HomeAssistant) -> None:
    device, frames, patcher = _zigpy_bulb()
    try:
        zha_device = SimpleNamespace(async_get_cluster=lambda ep, cid: device.endpoints[ep].in_clusters[cid])
        gateway = SimpleNamespace(get_device=lambda ieee: zha_device if str(ieee) == IEEE else None)
        bulbs = [Bulb("light.b", IEEE, 11, "LCB002", "Signify Netherlands B.V.")]
        out = ZhaOutput(hass, bulbs, None)
        with patch("homeassistant.components.zha.helpers.get_zha_gateway", return_value=gateway):
            rtt = await out.async_send(Command(0, 0, 200, None, 5, 3))
            await out.async_send(Command(0, 0, None, (255, 0, 0), 3, 1))
        assert rtt >= 0
        (c1, cmd1, d1), (c2, cmd2, d2) = frames
        assert (c1, cmd1) == (0x0008, 0x00)  # Level Control / move_to_level
        assert _payload(c1, d1)[:3] == bytes([200, 5, 0])  # level, transition (uint16 LE)
        assert (c2, cmd2) == (0x0300, 0x07)  # Color Control / move_to_color
        x, y = rgb_to_zcl_xy((255, 0, 0))
        assert _payload(c2, d2)[:6] == x.to_bytes(2, "little") + y.to_bytes(2, "little") + (3).to_bytes(2, "little")
        assert out._direct  # noqa: SLF001 - stayed on the direct path
    finally:
        patcher.stop()


def test_red_is_clamped_to_hue_gamut() -> None:
    x, y = rgb_to_zcl_xy((255, 0, 0))
    assert abs(x / 65536 - 0.6915) < 0.01 and abs(y / 65536 - 0.3083) < 0.01


async def test_zha_not_ready_is_a_frame_failure_not_a_mode_switch(hass: HomeAssistant) -> None:
    out = ZhaOutput(hass, [Bulb("light.b", IEEE, 11, "LCB002", "Signify Netherlands B.V.")], None)
    with (
        patch("homeassistant.components.zha.helpers.get_zha_gateway", side_effect=ValueError("No gateway")),
        pytest.raises(Exception, match="ZHA is not ready"),
    ):
        await out.async_send(Command(0, 0, 120, None, 4, 3))
    assert out._direct


async def test_group_without_id_is_rejected(hass: HomeAssistant) -> None:
    out = ZhaOutput(hass, [], None)
    with pytest.raises(Exception, match="without a ZHA group"):
        await out.async_send(Command(0, GROUP, 100, None, 1, 1))


async def test_service_fallback_when_internals_missing(hass: HomeAssistant) -> None:
    calls = []

    async def handler(call):
        calls.append(call.data)

    hass.services.async_register("zha", "issue_zigbee_cluster_command", handler)
    hass.services.async_register("zha", "issue_zigbee_group_command", handler)
    bulbs = [Bulb("light.b", IEEE, 11, "LCB002", "Signify Netherlands B.V.")]
    out = ZhaOutput(hass, bulbs, 0x0002)
    # ZHA's internals changed shape (no get_device): fall back to the public services.
    with patch("homeassistant.components.zha.helpers.get_zha_gateway", return_value=object()):
        await out.async_send(Command(0, 0, 120, None, 4, 3))
        await out.async_send(Command(0, GROUP, None, (0, 0, 255), 2, 1))
    assert calls[0]["params"] == {"level": 120, "transition_time": 4}
    assert calls[0]["command_type"] == "server" and calls[0]["endpoint_id"] == 11
    assert calls[1]["group"] == 2 and calls[1]["command"] == 0x07 and len(calls[1]["args"]) == 3


async def test_resolve_expands_groups_and_rejects_non_zha(hass: HomeAssistant) -> None:
    registry = er.async_get(hass)
    a = registry.async_get_or_create("light", "zha", f"{IEEE}-11", suggested_object_id="a")
    b = registry.async_get_or_create("light", "zha", "00:17:88:01:0b:00:00:02-11", suggested_object_id="b")
    registry.async_get_or_create("light", "hue", "abc", suggested_object_id="wifi")
    hass.states.async_set("light.kitchen", "on", {"entity_id": [a.entity_id, b.entity_id]})
    bulbs = resolve_bulbs(hass, ["light.kitchen"])
    assert [(x.ieee, x.endpoint) for x in bulbs] == [(IEEE, 11), ("00:17:88:01:0b:00:00:02", 11)]
    with pytest.raises(BulbResolveError):
        resolve_bulbs(hass, ["light.wifi"])
    with pytest.raises(BulbResolveError):
        resolve_bulbs(hass, ["light.missing"])


@pytest.fixture(autouse=True)
def _no_sockets_needed() -> AsyncMock:
    return AsyncMock()


async def test_group_frame_through_real_zigpy_group(hass: HomeAssistant) -> None:
    import zigpy.group

    app = MagicMock()
    sent = []

    async def send_packet(packet):
        sent.append(packet)

    app.send_packet = send_packet
    app.get_sequence = MagicMock(return_value=7)
    groups = zigpy.group.Groups(app)
    group = zigpy.group.Group(0x0002, "Kitchen", groups)
    gateway = SimpleNamespace(get_group=lambda gid: SimpleNamespace(endpoint=group.endpoint) if gid == 2 else None)
    out = ZhaOutput(hass, [], 2)
    with patch("homeassistant.components.zha.helpers.get_zha_gateway", return_value=gateway):
        await out.async_send(Command(0, GROUP, None, (0, 0, 255), 3, 1))
    assert out._direct and len(sent) == 1
    packet = sent[0]
    assert packet.dst.address == 0x0002 and packet.cluster_id == 0x0300


async def test_third_reality_gets_native_hue_saturation_and_hue_gets_xy(hass: HomeAssistant) -> None:
    from custom_components.music_light_sync.zigbee import (  # noqa: PLC0415
        default_color_format,
        group_color_format,
        rgb_to_zcl_hs,
        unsupported_models,
    )

    hue = Bulb("light.k", IEEE, 11, "LCB002", "Signify Netherlands B.V.")
    tr = Bulb("light.d", IEEE, 1, "3RCB01057Z", "Third Reality, Inc")
    odd = Bulb("light.o", IEEE, 1, "LCT015", "Signify Netherlands B.V.")
    assert default_color_format(hue, "auto") == "xy"
    assert default_color_format(tr, "auto") == "hs"
    assert default_color_format(tr, "xy") == "xy" and default_color_format(hue, "hs") == "hs"
    assert unsupported_models([hue, tr, odd]) == ["light.o (LCT015)"]
    hs_tr = Bulb(*[getattr(tr, f) for f in ("entity_id", "ieee", "endpoint", "model", "manufacturer")], "hs")
    xy_hue = Bulb(*[getattr(hue, f) for f in ("entity_id", "ieee", "endpoint", "model", "manufacturer")], "xy")
    assert group_color_format([hs_tr, xy_hue]) is None  # a mixed room can't multicast colors
    assert rgb_to_zcl_hs((255, 0, 0)) == (0, 254)
    assert rgb_to_zcl_hs((0, 0, 255)) == (169, 254)


async def test_xy_frames_bypass_a_quirk_that_rewrites_them(hass: HomeAssistant) -> None:
    """Third Reality's native-HS quirk turns move_to_color into hue/sat; forced xy must reach the bulb as xy."""
    from zigpy.zcl.clusters.lighting import Color  # noqa: PLC0415

    device, frames, patcher = _zigpy_bulb()
    try:
        cluster = device.endpoints[11].in_clusters[0x0300]
        original = type(cluster).command

        async def quirk(self, command_id, *args, **kwargs):  # what the custom quirk does
            if command_id == Color.ServerCommandDefs.move_to_color.id:
                return await original(self, 0x06, hue=1, saturation=2, transition_time=0)
            return await original(self, command_id, *args, **kwargs)

        zha_device = SimpleNamespace(async_get_cluster=lambda ep, cid: device.endpoints[ep].in_clusters[cid])
        gateway = SimpleNamespace(get_device=lambda ieee: zha_device)
        with (
            patch.object(type(cluster), "command", quirk),
            patch("homeassistant.components.zha.helpers.get_zha_gateway", return_value=gateway),
        ):
            xy = ZhaOutput(hass, [Bulb("light.d", IEEE, 11, "3RCB01057Z", "Third Reality, Inc", "xy")], None)
            await xy.async_send(Command(0, 0, None, (255, 0, 0), 3, 1))
            hs = ZhaOutput(hass, [Bulb("light.d", IEEE, 11, "3RCB01057Z", "Third Reality, Inc", "hs")], None)
            await hs.async_send(Command(0, 0, None, (0, 0, 255), 3, 1))
        assert [cmd for _cluster, cmd, _data in frames] == [0x07, 0x06]
    finally:
        patcher.stop()


async def test_adaptive_lighting_found_through_a_zha_group_light(hass: HomeAssistant) -> None:
    """The house's AL entries list ZHA group lights, which have no member list in their state."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.music_light_sync.zigbee import adaptive_lighting_switches

    registry = er.async_get(hass)
    bulbs = [
        registry.async_get_or_create("light", "zha", f"c8:90:a8:1e:ac:71:00:0{i}-1", suggested_object_id=f"tr{i}")
        for i in range(1, 5)
    ]
    group_light = registry.async_get_or_create("light", "zha", "light_zha_group_0x0005", suggested_object_id="grp")
    hass.states.async_set(group_light.entity_id, "on", {})  # no "entity_id" attribute
    sconces = registry.async_get_or_create("light", "zwave_js", "sconce", suggested_object_id="sconces")

    al = MockConfigEntry(domain="adaptive_lighting", options={"lights": [sconces.entity_id, group_light.entity_id]})
    al.add_to_hass(hass)
    main = registry.async_get_or_create("switch", "adaptive_lighting", "Kitchen Island", config_entry=al)
    registry.async_get_or_create("switch", "adaptive_lighting", "Kitchen Island_sleep_mode", config_entry=al)
    other = MockConfigEntry(domain="adaptive_lighting", options={"lights": ["light.office"]})
    other.add_to_hass(hass)
    registry.async_get_or_create("switch", "adaptive_lighting", "Office", config_entry=other)

    members = [
        SimpleNamespace(device=SimpleNamespace(ieee=t.EUI64.convert(f"c8:90:a8:1e:ac:71:00:0{i}")), endpoint_id=1)
        for i in range(1, 5)
    ]
    gateway = SimpleNamespace(groups={5: SimpleNamespace(members=members)})
    with patch("homeassistant.components.zha.helpers.get_zha_gateway", return_value=gateway):
        assert adaptive_lighting_switches(hass, [b.entity_id for b in bulbs]) == [main.entity_id]
        assert adaptive_lighting_switches(hass, [bulbs[2].entity_id]) == [main.entity_id]  # one bulb is enough
    with patch("homeassistant.components.zha.helpers.get_zha_gateway", side_effect=KeyError):
        assert adaptive_lighting_switches(hass, [b.entity_id for b in bulbs]) == []  # ZHA not loaded


def test_color_record_from_bulb_attributes() -> None:
    from custom_components.music_light_sync.zigbee import color_record  # noqa: PLC0415

    assert color_record({8: 2, 7: 454, 3: 1, 4: 2}) == {"mode": "color_temp", "mireds": 454}
    assert color_record({8: 1, 7: 454, 3: 30000, 4: 20000}) == {"mode": "xy", "x": 30000, "y": 20000}
    assert color_record({8: 0, 0: 10, 1: 254}) == {"mode": "hs", "hue": 10, "saturation": 254}
    assert color_record({7: 454}) is None  # no color mode: nothing to trust


async def test_color_temperature_written_while_off_reaches_the_bulb(hass: HomeAssistant) -> None:
    """move_to_color_temp with ZCL 'execute if off', through the stock command (no quirk rewrite)."""
    device, frames, patcher = _zigpy_bulb()
    try:
        cluster = device.endpoints[11].in_clusters[0x0300]
        cluster._attr_cache.update({8: 2, 7: 370})  # noqa: SLF001 - what ZHA last saw
        zha_device = SimpleNamespace(async_get_cluster=lambda ep, cid: device.endpoints[ep].in_clusters[cid])
        gateway = SimpleNamespace(get_device=lambda ieee: zha_device)
        with patch("homeassistant.components.zha.helpers.get_zha_gateway", return_value=gateway):
            out = ZhaOutput(hass, [Bulb("light.k1", IEEE, 11, "LCB002", "Signify Netherlands B.V.")], None)
            with patch.object(type(cluster), "read_attributes", wraps=cluster.read_attributes) as read:
                read.side_effect = [TimeoutError(), ({8: 2, 7: 370}, {})]  # live read fails, cache answers
                assert await out.async_read_color(0) == {"mode": "color_temp", "mireds": 370}
            await out.async_set_color_while_off(0, {"mode": "color_temp", "mireds": 370})
        cluster_id, command, data = frames[-1]
        assert (cluster_id, command) == (0x0300, 0x0A)
        schema = cluster.server_commands[0x0A].schema
        args = schema.deserialize(_payload(cluster, data))[0]
        assert args.color_temp_mireds == 370 and args.transition_time == 0
        assert int(args.options_mask) == 1 and int(args.options_override) == 1  # execute if off
    finally:
        patcher.stop()
