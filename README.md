# Music Light Sync

Home Assistant integration that syncs **ZHA color bulbs** to whatever **Music Assistant** is playing. It needs no Hue bridge, no sync box, and no Wi-Fi bulbs.

```
Tidal ─► Music Assistant ──(audio, AirPlay 2)──► Bluesound Node ─► amp ─► speakers
              │
              │  Sendspin visualizer + color roles (no audio):
              │  beats/downbeats, onsets, loudness, spectrum, album-art palette,
              │  each timestamped to when it will be HEARD, sent 0.3–1.5 s early
              ▼
     Music Light Sync (this) ─► effect engine ─► Zigbee governor ─► ZHA ─► Hue bulbs
```

Music Assistant already analyzes the audio it decodes. This integration connects to Music Assistant's Sendspin server (port 8927) as a **visualizer** player and receives that analysis. Every beat arrives ahead of the audio, so each Zigbee frame is sent early by the measured mesh delay and the change lands on the beat. Home Assistant never receives any audio.

## What to expect

- **Party (light jockey)** (default): runs the lights the way a person at a lighting desk would. The bulbs work in groups that share color, brightness and rhythm, and the look changes every 1–4 bars, always on a bar line and never the same look twice in a row. Looks include:
  - **Unison:** all 5 hit together.
  - **Call and response:** 2 bulbs answer 3.
  - **Hold and pairs:** 1 bulb holds a color while 2 pairs trade beats.
  - **Chase:** a hit runs around the room.
  - **Build:** 2, then 3, 4 and 5 bulbs join in.
  - **Swell, spotlight and sparkle.**

  The music's energy picks the look: calm looks for quiet passages, chases and sparkles for loud ones, and a sudden jump in volume (a drop) hits all 5 at once. Each group gets colors far apart on the color wheel. They come from the album art plus its complementary and triadic hues, and they drift around the wheel during a song.
- **Beat breathe:** the bulbs flash to full brightness on beats 1 and 3, then fade down before the next flash.
- **Bar color shift**: every bulb changes palette color on each bar. With a ZHA group this takes one multicast frame per bar.
- **Max reactive**: each bulb follows its own frequency band, bass on the first bulb. Colors rotate on the beat.
- **Ambient palette**: album-art colors drift slowly. This works without beat analysis, and through the fallback player (see below).

Timing jitter is about 50–150 ms. Zigbee only allows about 10–15 frames per second before the rest of the house suffers, so this cannot match a Hue Sync Box's 25 Hz strobing. Fades are smooth because the bulbs interpolate each transition themselves.

## Requirements

- Home Assistant 2026.10 or later with ZHA.
- Music Assistant 2.10.7 or later. It provides the Sendspin visualizer and color roles, and beat analysis through Smart Fades.
- Color bulbs on ZHA. Tested against the zigpy 2.3 ZCL definitions for `move_to_level` and `move_to_color`.

## Install (HACS)

1. In HACS, open the three-dot menu, choose **Custom repositories**, add this repository's URL, and set the category to **Integration**.
2. Install **Music Light Sync** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Music Light Sync**:
   - **Name**: `Kitchen Lights`. This is the player name shown in Music Assistant.
   - **Lights**: `light.kitchen_overhead_light_bulbs`. Group helpers are expanded, and every member must be a ZHA light.
   - **Host**: pre-filled from your Music Assistant integration. **Port**: `8927`.

## One-time setup in Music Assistant

1. **Approve the player.** Go to **Settings → Players** and open **Kitchen Lights**. Light players are hidden by default, so you may need to show hidden players first. Approve it with the one-click allow option rather than pairing. Until you do, the integration's Status sensor reads *Waiting for approval in Music Assistant*. Allowing it exposes nothing on the Home Assistant side, because the client only has the visualizer and color roles: no microphone and no audio output.
2. **Check the Node can join Sendspin groups.** Open the **Living Room** (Node) player and confirm AirPlay is listed among its protocols. Music Assistant uses AirPlay 2 to keep the Node in sync with Sendspin members.

## Daily use: two paths

| Path | How | Audio to the Node | Lights |
|---|---|---|---|
| **Synced** | In Music Assistant, group **Living Room** with **Kitchen Lights** | AirPlay 2, capped at 24-bit/48 kHz | All four effects, beat-accurate |
| **Native hi-res** | Play to **Living Room** alone | Native BluOS, full Tidal resolution | Optional: the Ambient palette from album art, via the fallback player |

The **Sync** switch arms the integration. While it is on and Music Assistant streams to the group:

1. The bulbs' current state and the list of paused entities are written to `.storage`.
2. The entities you chose to pause are turned off, and the bulbs switch on in palette colors.
3. When the music has been stopped for longer than *Restore after* (default 10 s), the bulbs return to exactly their pre-show state and the paused entities turn back on.

If Home Assistant stops, the integration hands back first. If it crashes or loses power mid-show, the hand-back runs at the next start. An unavailable bulb stays recorded and is retried every minute.

## Options (Configure)

| Option | Recommendation for this house |
|---|---|
| ZHA group | Optional. In ZHA, create a group with exactly the 5 kitchen bulbs and pick it here. Bar changes then hit all bulbs in one frame. The option is refused if the group has extra members. |
| Zigbee frame budget | 20 frames/s (the default). This trades a little Zigbee responsiveness during a show for livelier lights. The governor still backs off by itself if the mesh gets congested. |
| Min. group spacing | 2 s. Multicast frames fill the coordinator's broadcast table, so don't go lower. |
| Min/Max brightness | 15 % / 100 % |
| Pause during a show | The kitchen occupancy/illuminance automations, and the *Adaptive Lighting: Kitchen Overhead* switch if you turn it back on. |
| Restore after | 10 s |
| Fallback player | `media_player.node_a7d8`. Runs the ambient palette from album art when the Node plays without the lights grouped. |

## Entities

- `switch.<name>_sync`: arms or disarms the integration. Turning it off ends a running show and restores the lights.
- `select.<name>_effect`: picks one of the four effects.
- `number.<name>_latency_offset`: positive values fire the lights earlier.
- `sensor.<name>_status`: the connection and show state. Its attributes show `bpm`, `pulse_source` (beats, onsets or none), `frames_per_second`, `rate_limit`, `zigbee_rtt_ms`, `lead_ms`, frame counters and the current palette.

### Calibrating

Pick **Beat breathe** and play something with a clear kick drum. Watch whether the brightness peaks land on the kick. Adjust **Latency offset** in steps of 20–30 ms: positive if the lights are late, negative if early. The integration already compensates the measured Zigbee delay, so this offset only covers the speaker side, mostly AirPlay buffering.

## Zigbee notes

- Every command is one ZCL frame, either `Level Control / move_to_level` or `Color Control / move_to_color`, with the bulb doing the fade. Frames go directly to ZHA's zigpy cluster objects, so the event bus is not flooded with `call_service` events. If ZHA's internals change, it falls back to the `zha.issue_zigbee_*_command` services.
- At most 6 frames are in flight at once, out of zigpy's 8 request slots, so other devices are slowed during a show but never starved.
- The bulbs report their new brightness and color back to ZHA, so the light entities update during a show. To keep those state changes out of the history database, exclude the 5 bulb entities from `recorder`.

## Limitations

- Beat tracking comes from Music Assistant's analysis. The first play of a track may not be analyzed yet; the integration then falls back to onsets, and to ambient if there are none.
- ZCL transitions use 0.1 s steps.
- The native hi-res path gets album art only. There is no beat data, because the lights are not part of that stream.

## Development

```
pip install homeassistant pytest-homeassistant-custom-component "aiosendspin[server]==10.2.0" zha==2.3.1 pillow
pytest
```

The integration test starts a real Sendspin server from the same `aiosendspin` version Music Assistant 2.10.7 uses. It approves the client, streams audio with a beat schedule, and checks that level ramps start on the beats, that the frame budget holds, and that the pre-show state is restored.
