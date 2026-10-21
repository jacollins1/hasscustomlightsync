"""Runs a light show session: wires Sendspin features -> engine -> governor -> ZHA.

Session lifecycle (only while the Sync switch is on):

* Music Assistant starts streaming features for a group that contains our
  visualizer player -> record the bulbs' state and pause the configured
  automations (both persisted to disk first), switch the bulbs on with the
  palette, then run the effect loop.
* The stream ends -> wait ``end_delay`` seconds (pauses and track gaps
  shouldn't flicker the room), then restore the bulbs and resume automations.
* Optional fallback: if a configured media player (e.g. the Node playing
  native hi-res, not grouped with the lights) is playing without a Sendspin
  stream, run the ambient palette mode from its album art instead.

Hand-back safety: the pre-show light states and the list of paused entities
are written to ``.storage`` before anything is changed. Home Assistant stopping
triggers a hand-back, and anything still recorded at the next start (power
cut, crash) is handed back then.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, STATE_OFF, STATE_ON, STATE_PLAYING, STATE_UNAVAILABLE
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store

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
    DEFAULT_PORT,
    DEFAULT_RATE_MAX,
    DOMAIN,
    SENDSPIN_PATH,
    TICK_S,
    VERSION,
)
from .engine import Command, EffectEngine, EngineSettings, Mode
from .governor import Governor
from .palette import build_palette, palette_from_image
from .sendspin_link import LinkState, SendspinLink
from .zigbee import Bulb, ZhaOutput, resolve_bulbs

if TYPE_CHECKING:
    from aiosendspin.models.color import SessionUpdateColor
    from aiosendspin.models.visualizer import VisualizerFrame
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

FALLBACK_GRACE_S = 4.0  # let a real Sendspin stream win when both start together
LOOP_ERROR_LOG_EVERY_S = 30.0
HANDBACK_RETRY_S = 60.0
HANDBACK_MAX_RETRIES = 30  # half an hour of retries, then the record waits for the next show
_LIGHT_ATTRS = ("brightness", "color_mode", "color_temp_kelvin", "xy_color", "hs_color", "effect")


class Status(StrEnum):
    """Values of the status sensor."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    AWAITING_APPROVAL = "awaiting_approval"
    IDLE = "idle"
    SYNCING = "syncing"
    AMBIENT_FALLBACK = "ambient_fallback"


class Session(StrEnum):
    """Which feature source is driving the lights."""

    STREAM = "stream"
    FALLBACK = "fallback"


def _pct_to_level(pct: float) -> int:
    return max(1, min(254, round(pct * 254 / 100)))


def capture_light(state: State | None) -> dict[str, Any] | None:
    """The parts of a light's state needed to put it back."""
    if state is None or state.state not in (STATE_ON, STATE_OFF):
        return None
    captured: dict[str, Any] = {"state": state.state}
    for attr in _LIGHT_ATTRS:
        if (value := state.attributes.get(attr)) is not None:
            captured[attr] = list(value) if isinstance(value, tuple) else value
    return captured


def restore_call(entity_id: str, captured: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Service + data that returns a light to a captured state."""
    if captured["state"] == STATE_OFF:
        return "turn_off", {"entity_id": entity_id, "transition": 1}
    data: dict[str, Any] = {"entity_id": entity_id, "transition": 1}
    if "brightness" in captured:
        data["brightness"] = captured["brightness"]
    mode = captured.get("color_mode")
    if mode == "color_temp" and "color_temp_kelvin" in captured:
        data["color_temp_kelvin"] = captured["color_temp_kelvin"]
    elif mode in ("xy", "hs", "rgb", "rgbw", "rgbww") and "xy_color" in captured:
        data["xy_color"] = captured["xy_color"]
    elif mode in ("xy", "hs", "rgb", "rgbw", "rgbww") and "hs_color" in captured:
        data["hs_color"] = captured["hs_color"]
    return "turn_on", data


class LightSyncController:
    """One per config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        opts = {**entry.data, **entry.options}
        self.name: str = entry.data[CONF_NAME]
        self.light_ids: list[str] = list(entry.data[CONF_LIGHTS])
        self.group_id: int | None = opts.get(CONF_ZHA_GROUP_ID) or None
        self.fallback_player: str | None = opts.get(CONF_FALLBACK_PLAYER) or None
        self.pause_entities: list[str] = list(opts.get(CONF_PAUSE_ENTITIES, []))
        self.end_delay_s = float(opts.get(CONF_END_DELAY, DEFAULT_END_DELAY))
        self.min_level = _pct_to_level(opts.get(CONF_MIN_BRIGHTNESS, DEFAULT_MIN_BRIGHTNESS))
        self.max_level = _pct_to_level(opts.get(CONF_MAX_BRIGHTNESS, DEFAULT_MAX_BRIGHTNESS))
        self._group_min_interval_s = float(opts.get(CONF_GROUP_MIN_INTERVAL, DEFAULT_GROUP_MIN_INTERVAL))
        self._ambient_period_s = float(opts.get(CONF_AMBIENT_PERIOD, DEFAULT_AMBIENT_PERIOD))
        self.governor = Governor(
            rate_max=float(opts.get(CONF_RATE_MAX, DEFAULT_RATE_MAX)),
            group_min_interval_s=self._group_min_interval_s,
        )

        # Restored by the entities on startup.
        self.armed = True
        self.mode = Mode.PARTY
        self.user_offset_ms = 0.0

        self.bulbs: list[Bulb] = []
        self.output: ZhaOutput | None = None
        self.engine: EffectEngine | None = None
        self.fallback_engine: EffectEngine | None = None

        port = int(entry.data.get(CONF_PORT, DEFAULT_PORT))
        self.link = SendspinLink(
            hass,
            url=f"ws://{entry.data[CONF_HOST]}:{port}{SENDSPIN_PATH}",
            name=self.name,
            identity_hex=entry.data[CONF_IDENTITY],
            store_path=hass.config.path(".storage", f"{DOMAIN}.{entry.entry_id}.sendspin.json"),
            listener=self,
            version=VERSION,
        )
        self._handback_store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.handback")

        self.session: Session | None = None
        self._stopping = False
        self._stream_active = False
        self._tick_task: asyncio.Task[None] | None = None
        self._send_tasks: set[asyncio.Task[None]] = set()
        self._end_cancel: Callable[[], None] | None = None
        self._fallback_cancel: Callable[[], None] | None = None
        self._captured: dict[str, dict[str, Any]] = {}
        self._paused: list[str] = []
        self._unsubs: list[Callable[[], None]] = []
        self._stop_unsub: Callable[[], None] | None = None
        self._retry_cancel: Callable[[], None] | None = None
        self._retries = 0
        self._update_listeners: list[Callable[[], None]] = []
        self._session_lock = asyncio.Lock()
        self._fallback_art: str | None = None
        self._last_loop_error_s = 0.0

    # ---------------------------------------------------------------- set-up

    def prepare(self) -> None:
        """Resolve bulbs and build the engines (raises BulbResolveError / BulbsNotReady)."""
        self.bulbs = resolve_bulbs(self.hass, self.light_ids)
        self.output = ZhaOutput(self.hass, self.bulbs, self.group_id)
        settings = EngineSettings(
            bulbs=len(self.bulbs),
            min_level=self.min_level,
            max_level=self.max_level,
            has_group=self.group_id is not None,
            group_min_interval_us=int(self._group_min_interval_s * 1e6),
            ambient_period_us=int(self._ambient_period_s * 1e6),
        )
        self.engine = EffectEngine(settings, self.mode)
        self.engine.budget_per_s = self.governor.rate
        self.fallback_engine = EffectEngine(settings, Mode.AMBIENT)

    async def async_connect(self) -> None:
        """Start listening (after the entities restored armed/mode/offset)."""
        leftover = await self._handback_store.async_load()
        if leftover:
            # A previous run was cut off mid-show (crash, power loss).
            self._captured = dict(leftover.get("lights", {}))
            self._paused = list(leftover.get("paused", []))

            @callback
            def _hand_back_leftover(_hass: HomeAssistant) -> None:
                self._schedule_retry(0)

            self.entry.async_on_unload(async_at_started(self.hass, _hand_back_leftover))

        if self.fallback_player:
            self._unsubs.append(
                async_track_state_change_event(self.hass, [self.fallback_player], self._on_fallback_player)
            )
        self._stop_unsub = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._async_on_hass_stop)
        await self.link.async_start()

    @callback
    def _schedule_retry(self, delay_s: float) -> None:
        """Retry an incomplete hand-back (bulb unavailable, ZHA still starting)."""
        if self._retry_cancel or self._stopping or self._retries >= HANDBACK_MAX_RETRIES:
            return

        @callback
        def _retry(_now: Any) -> None:
            self._retry_cancel = None
            self._retries += 1
            if not self._stopping:
                self.entry.async_create_task(self.hass, self._async_retry_hand_back(), "retry hand back")

        self._retry_cancel = async_call_later(self.hass, delay_s, _retry)

    async def _async_retry_hand_back(self) -> None:
        async with self._session_lock:
            # A running show already owns the record and hands it back when it ends.
            if self.session is None and not self._stopping and (self._captured or self._paused):
                await self._async_hand_back()

    async def _async_on_hass_stop(self, _event: Event) -> None:
        self._stop_unsub = None  # a fired once-listener must not be removed again
        await self.async_stop(hass_stopping=True)

    async def async_stop(self, *, hass_stopping: bool = False) -> None:
        """Disconnect and put the lights back."""
        if self._stopping:
            return
        self._stopping = True
        if self._stop_unsub is not None:
            self._stop_unsub()
            self._stop_unsub = None
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._cancel_timers()
        # Hand back first: on shutdown ZHA stops its radio on this same event.
        await self._async_end_session(force=True)
        await self.link.async_stop()
        if hass_stopping:
            _LOGGER.debug("%s: Home Assistant stopping, show handed back", self.name)

    # -------------------------------------------------------------- entities

    @callback
    def add_update_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register an entity refresh callback."""
        self._update_listeners.append(listener)

        def _remove() -> None:
            self._update_listeners.remove(listener)

        return _remove

    @callback
    def _notify(self) -> None:
        for listener in list(self._update_listeners):
            listener()

    @property
    def status(self) -> Status:
        """Overall state for the status sensor."""
        link = self.link.state
        if self.session is Session.FALLBACK:
            return Status.AMBIENT_FALLBACK
        if link is LinkState.DISCONNECTED:
            return Status.DISCONNECTED
        if link is LinkState.CONNECTING:
            return Status.CONNECTING
        if link is LinkState.AWAITING_APPROVAL:
            return Status.AWAITING_APPROVAL
        return Status.SYNCING if self.session is Session.STREAM else Status.IDLE

    def status_attributes(self) -> dict[str, Any]:
        """Diagnostics shown on the status sensor."""
        engine = self.fallback_engine if self.session is Session.FALLBACK else self.engine
        gov = self.governor
        return {
            "mode": self.mode.value,
            "bpm": engine.bpm if engine else None,
            "pulse_source": engine.beat_source if engine else None,
            "frames_per_second": gov.frames_per_s(time.monotonic()),
            "rate_limit": round(gov.rate, 1),
            "zigbee_rtt_ms": None if gov.rtt_s is None else round(gov.rtt_s * 1000),
            "lead_ms": round(self._lead_us() / 1000),
            "frames_sent": gov.stats.sent,
            "frames_dropped_late": gov.stats.dropped_stale,
            "frames_coalesced": gov.stats.coalesced,
            "frame_errors": gov.stats.errors,
            "palette": ["#{:02x}{:02x}{:02x}".format(*c) for c in engine.palette] if engine else [],
            "time_synced": self.link.time_synced,
            "sendspin_client_id": self.link.client_id,
            "last_error": self.link.last_error,
        }

    async def async_set_armed(self, armed: bool) -> None:
        """Sync switch."""
        self.armed = armed
        if not armed:
            self._cancel_timers()
            await self._async_end_session()
        elif self._stream_active:
            await self._async_begin(Session.STREAM)
        else:
            self._maybe_schedule_fallback()
        self._notify()

    def set_mode(self, mode: Mode) -> None:
        """Effect select."""
        self.mode = mode
        if self.engine:
            self.engine.set_mode(mode)
        self._notify()

    def set_user_offset(self, offset_ms: float) -> None:
        """Latency offset number: positive makes the lights fire earlier."""
        self.user_offset_ms = offset_ms
        self._notify()

    # ------------------------------------------------- Sendspin link callbacks

    @callback
    def on_link_state(self, state: LinkState) -> None:
        """Connection state changed."""
        if not self._stopping:
            self._notify()

    @callback
    def on_stream_start(self, tracks_downbeats: bool, types: tuple[str, ...]) -> None:
        """Features started flowing for our group."""
        if self._stopping or self.engine is None:
            return
        self.engine.tracks_downbeats = tracks_downbeats
        if not self._stream_active:
            self.engine.reset()
            self.governor.reset()
        self._stream_active = True
        _LOGGER.debug("Visualizer stream started: %s (downbeats=%s)", types, tracks_downbeats)
        self._cancel_end()
        if self.armed:
            self.entry.async_create_task(self.hass, self._async_begin(Session.STREAM), "start light show")

    @callback
    def on_stream_clear(self) -> None:
        """Seek or skip: everything queued is for audio that won't play."""
        if self.engine:
            self.engine.reset()
        self.governor.reset()

    @callback
    def on_stream_end(self) -> None:
        """Group stopped: restore after a grace period."""
        if self._stopping:
            return
        self._stream_active = False
        if self.engine:
            self.engine.reset()
        if self.session is Session.STREAM:
            self._schedule_end()
        self._maybe_schedule_fallback()

    @callback
    def on_frames(self, frames: list[VisualizerFrame]) -> None:
        """Route frames into the engine, translated to the local clock."""
        engine = self.engine
        # Only buffer while a show can run; before the clock filter converges
        # the library maps every timestamp to "now + 500 ms", which would
        # squash a burst of beats together.
        if engine is None or self._stopping or not self.armed or not self._stream_active:
            return
        if not self.link.time_synced:
            return
        for frame in frames:
            ts = self.link.to_play_us(frame.timestamp_us)
            if frame.is_downbeat is not None:
                engine.add_beat(ts, frame.is_downbeat)
            elif frame.spectrum is not None:
                engine.add_spectrum(ts, frame.spectrum)
            elif frame.loudness is not None:
                engine.add_loudness(ts, frame.loudness)
            elif frame.peak_strength is not None:
                engine.add_peak(ts, frame.peak_strength)

    @callback
    def on_color(self, color: SessionUpdateColor | None) -> None:
        """New album-art palette from Music Assistant."""
        if color is None or self.engine is None or self._stopping:
            return
        sources = [color.primary, color.accent, color.on_dark, color.background_dark, color.on_light]
        self.engine.set_palette(build_palette(sources))
        self._notify()

    # ------------------------------------------------------------- fallback

    @callback
    def _on_fallback_player(self, event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        if new is None or self._stopping:
            return
        art = new.attributes.get("entity_picture")
        if self.session is Session.FALLBACK and art != self._fallback_art:
            self.entry.async_create_task(self.hass, self._async_load_fallback_palette(), "album art palette")
        if new.state == STATE_PLAYING:
            if self.session is Session.FALLBACK:
                self._cancel_end()
            self._maybe_schedule_fallback()
        elif self.session is Session.FALLBACK:
            self._schedule_end()

    def _fallback_wanted(self) -> bool:
        if self._stopping or not (self.armed and self.fallback_player) or self._stream_active:
            return False
        state = self.hass.states.get(self.fallback_player)
        return state is not None and state.state == STATE_PLAYING

    @callback
    def _maybe_schedule_fallback(self) -> None:
        if self._fallback_cancel or not self._fallback_wanted() or self.session is not None:
            return

        @callback
        def _go(_now: Any) -> None:
            self._fallback_cancel = None
            if self._fallback_wanted() and self.session is None:
                self.entry.async_create_task(self.hass, self._async_begin(Session.FALLBACK), "start ambient")

        self._fallback_cancel = async_call_later(self.hass, FALLBACK_GRACE_S, _go)

    async def _async_load_fallback_palette(self) -> None:
        """Album art of the fallback player -> palette."""
        if self.fallback_engine is None or not self.fallback_player:
            return
        state = self.hass.states.get(self.fallback_player)
        self._fallback_art = state.attributes.get("entity_picture") if state else None
        image = await self._async_player_image(self.fallback_player)
        if image is None:
            return
        try:
            palette = await self.hass.async_add_executor_job(palette_from_image, image)
        except Exception:  # noqa: BLE001 - a bad image must not stop the show
            _LOGGER.debug("Could not extract palette from album art", exc_info=True)
            return
        self.fallback_engine.set_palette(palette)
        self._notify()

    async def _async_player_image(self, entity_id: str) -> bytes | None:
        from homeassistant.components.media_player import DATA_COMPONENT  # noqa: PLC0415

        component = self.hass.data.get(DATA_COMPONENT)
        entity = component.get_entity(entity_id) if component else None
        if entity is None:
            return None
        try:
            image, _content_type = await entity.async_get_media_image()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not fetch album art for %s", entity_id, exc_info=True)
            return None
        return image

    # -------------------------------------------------------------- sessions

    def _cancel_end(self) -> None:
        if self._end_cancel:
            self._end_cancel()
            self._end_cancel = None

    def _cancel_timers(self) -> None:
        self._cancel_end()
        if self._retry_cancel:
            self._retry_cancel()
            self._retry_cancel = None
        if self._fallback_cancel:
            self._fallback_cancel()
            self._fallback_cancel = None

    @callback
    def _schedule_end(self) -> None:
        if self._end_cancel or self._stopping:
            return

        @callback
        def _end(_now: Any) -> None:
            self._end_cancel = None
            if not self._stopping:
                self.entry.async_create_task(self.hass, self._async_after_end(), "end light show")

        self._end_cancel = async_call_later(self.hass, self.end_delay_s, _end)

    async def _async_after_end(self) -> None:
        if self._stopping or (self._stream_active and self.armed):
            return
        if self._fallback_wanted():
            await self._async_begin(Session.FALLBACK)
            return
        await self._async_end_session()

    async def _async_begin(self, kind: Session) -> None:
        if self._stopping:
            return
        async with self._session_lock:
            if self._stopping:
                return
            self._cancel_end()
            if self.session is kind:
                return
            first = self.session is None
            # Set before taking over, so an end that arrives meanwhile sees a session.
            self.session = kind
            if first:
                await self._async_take_over()
            if kind is Session.FALLBACK:
                await self._async_load_fallback_palette()
            engine = self.fallback_engine if kind is Session.FALLBACK else self.engine
            assert engine is not None
            await self._async_prime(engine.palette)
            if self._tick_task is None:
                self._tick_task = self.entry.async_create_background_task(
                    self.hass, self._tick_loop(), f"{DOMAIN} effect loop {self.name}"
                )
            _LOGGER.info("%s: light show started (%s)", self.name, kind.value)
        # The music may have stopped while we were taking over.
        still_wanted = (self._stream_active and self.armed) if kind is Session.STREAM else self._fallback_wanted()
        if not still_wanted:
            self._schedule_end()
        self._notify()

    async def _async_end_session(self, *, force: bool = False) -> None:
        async with self._session_lock:
            if self.session is None and not (force and (self._captured or self._paused)):
                return
            self.session = None
            if self._tick_task:
                self._tick_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._tick_task
                self._tick_task = None
            for task in list(self._send_tasks):
                task.cancel()
            self.governor.reset()
            await self._async_hand_back()
            _LOGGER.info("%s: light show ended, lights restored", self.name)
        if not self._stopping:
            self._notify()

    async def _async_take_over(self) -> None:
        """Record the bulbs, persist that record, then pause other drivers.

        A record left over from an interrupted show wins over the current
        state, because it holds what the room looked like before that show.
        """
        current = {
            bulb.entity_id: captured
            for bulb in self.bulbs
            if (captured := capture_light(self.hass.states.get(bulb.entity_id))) is not None
        }
        self._captured = {**current, **self._captured}
        leftover_paused = list(self._paused)  # already off since the interrupted show
        to_pause = [
            entity_id
            for entity_id in self.pause_entities
            if entity_id not in leftover_paused
            and (state := self.hass.states.get(entity_id)) is not None
            and state.state == STATE_ON
        ]
        # Persist the intent before touching anything, so a crash can be undone.
        self._paused = [*leftover_paused, *to_pause]
        await self._async_save_handback()
        paused: list[str] = []
        for entity_id in to_pause:
            domain = entity_id.split(".", 1)[0]
            data: dict[str, Any] = {"entity_id": entity_id}
            if domain == "automation":
                data["stop_actions"] = False
            try:
                await self.hass.services.async_call(domain, "turn_off", data, blocking=True)
            except Exception as err:  # noqa: BLE001 - one failure must not strand the rest
                _LOGGER.warning("Could not pause %s: %s", entity_id, err)
                continue
            paused.append(entity_id)
        self._paused = [*leftover_paused, *paused]
        await self._async_save_handback()

    async def _async_save_handback(self) -> None:
        if self._captured or self._paused:
            await self._handback_store.async_save({"lights": self._captured, "paused": self._paused})
        else:
            await self._handback_store.async_remove()

    async def _async_prime(self, palette: list[tuple[int, int, int]]) -> None:
        """Bulbs on, at full show brightness, one palette color each."""
        results = await asyncio.gather(
            *(
                self.hass.services.async_call(
                    "light",
                    "turn_on",
                    {
                        "entity_id": bulb.entity_id,
                        "rgb_color": list(palette[index % len(palette)]),
                        "brightness": self.max_level,
                        "transition": 0.5,
                    },
                    blocking=True,
                )
                for index, bulb in enumerate(self.bulbs)
            ),
            return_exceptions=True,
        )
        for bulb, result in zip(self.bulbs, results, strict=True):
            if isinstance(result, Exception):
                _LOGGER.warning("Could not switch on %s: %s", bulb.entity_id, result)

    async def _async_hand_back(self) -> None:
        """Restore the recorded light states and resume paused entities.

        Call only while holding the session lock. Each call is independent;
        whatever fails, or targets an entity that is unavailable right now
        (Home Assistant's service layer skips those without an error), stays
        recorded and is retried.
        """
        captured_all, paused_all = dict(self._captured), list(self._paused)
        failed_lights: dict[str, dict[str, Any]] = {}
        for entity_id, captured in captured_all.items():
            state = self.hass.states.get(entity_id)
            if state is None or state.state == STATE_UNAVAILABLE:
                failed_lights[entity_id] = captured
                continue
            service, data = restore_call(entity_id, captured)
            try:
                await self.hass.services.async_call("light", service, data, blocking=True)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Could not restore %s: %s", entity_id, err)
                failed_lights[entity_id] = captured
        failed_paused: list[str] = []
        for entity_id in paused_all:
            domain = entity_id.split(".", 1)[0]
            try:
                await self.hass.services.async_call(domain, "turn_on", {"entity_id": entity_id}, blocking=True)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Could not resume %s: %s", entity_id, err)
                failed_paused.append(entity_id)
        self._captured, self._paused = failed_lights, failed_paused
        await self._async_save_handback()
        if failed_lights or failed_paused:
            _LOGGER.info("%s: will retry handing back %s", self.name, [*failed_lights, *failed_paused])
            self._schedule_retry(HANDBACK_RETRY_S)
        else:
            self._retries = 0

    # ------------------------------------------------------------ effect loop

    def _lead_us(self) -> int:
        return max(0, int((self.governor.one_way_s + self.user_offset_ms / 1000) * 1e6))

    def _clock(self) -> tuple[EffectEngine, int, int] | None:
        """Engine plus 'now' and lead on that engine's clock; None if unavailable."""
        if self.session is Session.FALLBACK:
            if self.fallback_engine is None:
                return None
            return self.fallback_engine, time.monotonic_ns() // 1000, 0
        if self.engine is None or not self.link.available:
            return None  # link dropped; the end-of-stream grace timer is running
        return self.engine, self.link.now_us(), self._lead_us()

    async def _tick_loop(self) -> None:
        refresh_at = 0.0
        while True:
            now_s = time.monotonic()
            try:
                if (clock := self._clock()) is not None:
                    engine, now_us, lead_us = clock
                    engine.budget_per_s = self.governor.rate
                    self.governor.offer(engine.step(now_us, lead_us))
                    for cmd in self.governor.take(now_s, now_us + lead_us):
                        task = self.hass.async_create_task(self._async_send(cmd), eager_start=False)
                        self._send_tasks.add(task)
                        task.add_done_callback(self._send_tasks.discard)
            except Exception:
                if now_s - self._last_loop_error_s > LOOP_ERROR_LOG_EVERY_S:
                    self._last_loop_error_s = now_s
                    _LOGGER.exception("Effect loop error (further errors suppressed for 30 s)")
            if now_s >= refresh_at:
                refresh_at = now_s + 5
                self._notify()
            await asyncio.sleep(TICK_S)

    async def _async_send(self, cmd: Command) -> None:
        assert self.output is not None
        try:
            rtt = await self.output.async_send(cmd)
        except asyncio.CancelledError:
            self.governor.on_result(time.monotonic(), None, True)
            raise
        except Exception as err:  # noqa: BLE001 - count it, back off, keep going
            self.governor.on_result(time.monotonic(), None, False)
            _LOGGER.debug("Zigbee frame to %s failed: %s", cmd.target, err)
            if self.governor.stats.errors in (10, 100, 1000):
                _LOGGER.warning("%s: %d Zigbee frames failed so far", self.name, self.governor.stats.errors)
            return
        self.governor.on_result(time.monotonic(), rtt, True)
