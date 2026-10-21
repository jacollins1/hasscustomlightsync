"""Constants for Music Light Sync."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "music_light_sync"
VERSION: Final = "0.3.0"

# Config entry data
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_NAME: Final = "name"
CONF_LIGHTS: Final = "lights"
CONF_IDENTITY: Final = "identity"

# Options
CONF_ZHA_GROUP_ID: Final = "zha_group_id"
CONF_RATE_MAX: Final = "rate_max"
CONF_GROUP_MIN_INTERVAL: Final = "group_min_interval"
CONF_MIN_BRIGHTNESS: Final = "min_brightness"
CONF_MAX_BRIGHTNESS: Final = "max_brightness"
CONF_FALLBACK_PLAYER: Final = "fallback_player"
CONF_PAUSE_ENTITIES: Final = "pause_entities"
CONF_END_DELAY: Final = "end_delay"
CONF_AMBIENT_PERIOD: Final = "ambient_period"
CONF_LAYOUT: Final = "layout"

DEFAULT_PORT: Final = 8927
DEFAULT_NAME: Final = "Music Lights"
DEFAULT_RATE_MAX: Final = 20.0
DEFAULT_GROUP_MIN_INTERVAL: Final = 2.0
DEFAULT_MIN_BRIGHTNESS: Final = 15  # percent
DEFAULT_MAX_BRIGHTNESS: Final = 100  # percent
DEFAULT_END_DELAY: Final = 10  # seconds of silence before restoring the lights
DEFAULT_AMBIENT_PERIOD: Final = 10  # seconds for a full palette walk

SENDSPIN_PATH: Final = "/sendspin"

# Visualizer request. 10 mel bins over 30 Hz-12 kHz are plenty to give each of
# a handful of bulbs its own band; 15 frames/s is already 3x what the Zigbee
# budget lets a single bulb show.
SPECTRUM_BINS: Final = 10
SPECTRUM_F_MIN: Final = 30
SPECTRUM_F_MAX: Final = 12000
VISUALIZER_RATE: Final = 15

TICK_S: Final = 0.02
USER_OFFSET_RANGE_MS: Final = 2000

# Hue color bulbs use gamut C.
HUE_GAMUT_C: Final = ((0.6915, 0.3083), (0.17, 0.7), (0.1532, 0.0475))
