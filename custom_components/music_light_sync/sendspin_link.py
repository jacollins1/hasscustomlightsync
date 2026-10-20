"""Connection to Music Assistant's Sendspin server as a visualizer + color client.

Music Assistant shows this client as a "visualizer" player. Grouping it with
a playing speaker makes Music Assistant stream audio features for that
group's music: beats, onsets, loudness and spectrum, each timestamped to
when it will be heard, plus a color palette taken from the album art.

No audio is ever sent to Home Assistant.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from aiosendspin.client import SendspinClient
from aiosendspin.models.core import DeviceInfo
from aiosendspin.models.types import GoodbyeReason, Roles
from aiosendspin.models.visualizer import (
    ClientHelloVisualizerSpectrum,
    ClientHelloVisualizerSupport,
    VisualizerStatePayload,
)
from aiosendspin.noise.keys import Identity
from aiosendspin.noise.trust_store import FileClientPairingStore
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import SPECTRUM_BINS, SPECTRUM_F_MAX, SPECTRUM_F_MIN, VISUALIZER_RATE

if TYPE_CHECKING:
    from aiosendspin.models.color import SessionUpdateColor
    from aiosendspin.models.core import ServerStatePayload, StreamStartMessage
    from aiosendspin.models.visualizer import VisualizerFrame
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

RECONNECT_MIN_S = 5.0
RECONNECT_MAX_S = 60.0
STATUS_POLL_S = 2.0


class LinkState(StrEnum):
    """Connection state shown on the status sensor."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    AWAITING_APPROVAL = "awaiting_approval"
    CONNECTED = "connected"


class LinkListener(Protocol):
    """What the controller implements to receive Sendspin events."""

    def on_link_state(self, state: LinkState) -> None:
        """Connection state changed."""

    def on_stream_start(self, tracks_downbeats: bool, types: tuple[str, ...]) -> None:
        """A visualizer stream started (or its configuration changed)."""

    def on_stream_clear(self) -> None:
        """Buffered features are void (seek, skip)."""

    def on_stream_end(self) -> None:
        """The group stopped playing."""

    def on_frames(self, frames: list[VisualizerFrame]) -> None:
        """Visualizer frames arrived (timestamps on the server clock)."""

    def on_color(self, color: SessionUpdateColor | None) -> None:
        """The palette for the current audio changed."""


def new_identity_hex() -> str:
    """Create the private key that gives this client a stable Sendspin client id."""
    return Identity.generate().private_bytes.hex()


def client_id_for(identity_hex: str) -> str:
    """Sendspin client id (and Music Assistant player id) for a stored identity."""
    return Identity.from_private_bytes(bytes.fromhex(identity_hex)).peer_id


class SendspinLink:
    """Keeps one Sendspin client connected, reconnecting with backoff."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        url: str,
        name: str,
        identity_hex: str,
        store_path: str | Path,
        listener: LinkListener,
        version: str,
    ) -> None:
        self._hass = hass
        self._url = url
        self._name = name
        self._identity = Identity.from_private_bytes(bytes.fromhex(identity_hex))
        self._store_path = store_path
        self._listener = listener
        self._version = version
        self._client: SendspinClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._state = LinkState.DISCONNECTED
        self._unsubs: list[Callable[[], None]] = []
        self.last_error: str | None = None

    @property
    def client_id(self) -> str:
        """This client's Sendspin id."""
        return self._identity.peer_id

    @property
    def state(self) -> LinkState:
        """Current connection state."""
        return self._state

    def now_us(self) -> int:
        """Client clock now, in microseconds."""
        assert self._client is not None
        return self._client.now_us()

    def to_play_us(self, server_ts_us: int) -> int:
        """Translate a server timestamp to the client clock (when it is audible)."""
        assert self._client is not None
        return self._client.compute_play_time(server_ts_us)

    @property
    def time_synced(self) -> bool:
        """Whether the clock filter has converged."""
        return self._client is not None and self._client.is_time_synchronized()

    @property
    def available(self) -> bool:
        """Connected, approved, and the client clock usable."""
        return self._client is not None and self._state is LinkState.CONNECTED

    async def async_start(self) -> None:
        """Start the background connection loop."""
        self._task = self._hass.async_create_background_task(self._run(), f"music_light_sync sendspin {self._name}")

    async def async_stop(self) -> None:
        """Disconnect and stop reconnecting."""
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._close_client(GoodbyeReason.SHUTDOWN)
        self._set_state(LinkState.DISCONNECTED)

    def _set_state(self, state: LinkState) -> None:
        if state != self._state:
            self._state = state
            self._listener.on_link_state(state)

    async def _close_client(self, reason: GoodbyeReason) -> None:
        # Leave CONNECTED before the client goes away, so nothing asks it for the time.
        if self._state in (LinkState.CONNECTED, LinkState.AWAITING_APPROVAL):
            self._set_state(LinkState.DISCONNECTED)
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._client is not None:
            client, self._client = self._client, None
            try:
                await client.disconnect(reason)
            except Exception:  # noqa: BLE001 - best effort on teardown
                _LOGGER.debug("Error while disconnecting Sendspin client", exc_info=True)

    async def _build_client(self) -> SendspinClient:
        store = await FileClientPairingStore.open(self._store_path)
        client = SendspinClient(
            self._identity,
            self._name,
            [Roles.VISUALIZER, Roles.COLOR],
            pairing_store=store,
            device_info=DeviceInfo(
                product_name="Music Light Sync",
                manufacturer="Home Assistant",
                software_version=self._version,
            ),
            visualizer_support=ClientHelloVisualizerSupport(buffer_capacity=262_144),
            visualizer_state=VisualizerStatePayload(
                types=["beat", "peak", "loudness", "spectrum"],
                rate_max=VISUALIZER_RATE,
                spectrum=ClientHelloVisualizerSpectrum(
                    n_disp_bins=SPECTRUM_BINS,
                    scale="mel",
                    f_min=SPECTRUM_F_MIN,
                    f_max=SPECTRUM_F_MAX,
                ),
            ),
            session=async_get_clientsession(self._hass),
        )
        # Lets Music Assistant approve us with one click instead of a pairing
        # code. Only the visualizer and color roles exist, so nothing of this
        # host (no microphone, no audio output) is exposed by allowing it.
        await client.set_unpaired_access(enabled=True)
        return client

    def _wire(self, client: SendspinClient, closed: asyncio.Event) -> None:
        listener = self._listener

        def _start(message: StreamStartMessage) -> None:
            vis = message.payload.visualizer
            if vis is None:
                return
            listener.on_stream_start(bool(getattr(vis, "tracks_downbeats", False)), tuple(vis.types))

        def _end(roles: list[str] | None) -> None:
            if roles is None or "visualizer" in roles:
                listener.on_stream_end()

        def _clear(roles: list[str] | None) -> None:
            if roles is None or "visualizer" in roles:
                listener.on_stream_clear()

        def _color(payload: ServerStatePayload | None) -> None:
            color = getattr(payload, "color", None) if payload is not None else None
            listener.on_color(color if hasattr(color, "primary") else None)

        self._unsubs = [
            client.add_stream_start_listener(_start),
            client.add_stream_end_listener(_end),
            client.add_stream_clear_listener(_clear),
            client.add_visualizer_listener(listener.on_frames),
            client.add_color_listener(_color),
            client.add_disconnect_listener(closed.set),
        ]

    def _roles_active(self) -> bool:
        """Whether Music Assistant has approved us (our roles are active)."""
        connection = getattr(self._client, "_admitted_connection", None)
        active = getattr(connection, "_active_roles", None)
        if active is None:  # library internals changed: assume approved
            return True
        return bool(active)

    async def _run(self) -> None:
        delay = RECONNECT_MIN_S
        while True:
            closed = asyncio.Event()
            self._set_state(LinkState.CONNECTING)
            try:
                self._client = await self._build_client()
                self._wire(self._client, closed)
                await self._client.connect(self._url)
                self.last_error = None
                delay = RECONNECT_MIN_S
                while not closed.is_set():
                    self._set_state(LinkState.CONNECTED if self._roles_active() else LinkState.AWAITING_APPROVAL)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(closed.wait(), STATUS_POLL_S)
                _LOGGER.info("Sendspin connection to %s closed", self._url)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - reconnect on anything
                self.last_error = str(err) or type(err).__name__
                _LOGGER.warning("Sendspin connection to %s failed: %s", self._url, self.last_error)
            self._listener.on_stream_end()
            await self._close_client(GoodbyeReason.RESTART)
            self._set_state(LinkState.DISCONNECTED)
            await asyncio.sleep(delay)
            delay = min(RECONNECT_MAX_S, delay * 2)
