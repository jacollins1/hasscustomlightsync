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
        bulbs = [SimpleNamespace(entity_id="light.b", ieee=IEEE, endpoint=11)]
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
    out = ZhaOutput(hass, [SimpleNamespace(entity_id="light.b", ieee=IEEE, endpoint=11)], None)
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
    bulbs = [SimpleNamespace(entity_id="light.b", ieee=IEEE, endpoint=11)]
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
