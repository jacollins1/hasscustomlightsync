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

Each **Music Light Sync** entry is one room: its own bulbs, layout, settings, and its own player in Music Assistant ("Kitchen Lights", "Dining Room Lights"). Add rooms with **Add entry** on the integration page; each one shows up there with its device, **Music Sync - <Room>**, placed in the room's area.

Rooms work together:

- **Light sync players.** Music Assistant gets one **<speaker> - Light sync** player for every BluOS speaker (for example **Living Room - Light sync**): the speaker plus every light room that joined it. Play to that player and the speaker plays while the lights in those rooms run. The players follow their speakers: a new BluOS speaker gets one, a disabled speaker disables it, a removed speaker removes it. A speaker Music Assistant can't group with light players (in this house: Office and Outside) gets a player with just the speaker.
- **Match other rooms.** Rooms with this option on that play in the same group run one show: same section, color pair, rhythm, rate and look length, changing on the same beat, each mapped onto its own bulbs and layout. Rooms with it off run their own show even when grouped.
- **One Zigbee budget.** All rooms share one coordinator, so the frame budget is house-wide: two rooms playing get half each, not double.

- **Party (lighting designer)** (default): runs the room the way a lighting operator would, following the club lighting rules below.
  - **Song structure.** It reads the track's loudness bar by bar and follows the story: *warm-up* → *groove* → *build* → *drop* → *peak* → *breakdown* → *outro*.
    - Builds use one cool tone, dim and slow.
    - A drop is an instant white hit on every bulb, with the biggest jump around the color wheel into the peak colors.
    - The last 20 seconds of a track return slowly to amber and warm white.
  - **Groupings** come from your room's layout:
    - all bulbs together;
    - solos;
    - pairs (in the kitchen: left column, right column, top alone, i.e. 2 + 2 + 1);
    - 1 + 4 and 2 + 3 splits (left/right, top/bottom, diagonal);
    - 3-way splits like 2 + 2 + 1 or 1 + 1 + 3;
    - a center bulb against the rest.
  - **Rhythms:** unison, call and response, canon (groups take turns), hold and play (one group holds while the others hit), chase, comet, bounce, sparkle, wave, swell, spotlight, build-up (2, then 3, 4, 5 bulbs join) and impact.
  - **Look changes** happen every 1–4 bars, always on a bar line, never the same look twice in a row.
  - **Two-color rule:** one color pair per section (a single tone in a build); the pair changes between sections and leans toward the album art's hues. Moods:
    - moody: deep blue, purple, violet;
    - euphoric: cyan, magenta, green;
    - high energy: red, amber;
    - outro: amber, warm white.
  - **White** is reserved for drops.
- **Beat breathe:** the bulbs flash on beats 1 and 3, then fade before the next flash. Use it to calibrate the latency.
- **Bar color shift:** every bulb changes palette color on each bar. With a ZHA group this takes one multicast frame per bar.
- **Max reactive:** each bulb follows its own frequency band, bass on the first bulb.
- **Ambient palette:** album-art colors drift slowly. This works without beat analysis, and through the music player (see below).

### Lighting rules built in

| Rule | How it's enforced |
|---|---|
| Strobe 1–13 Hz, never 15–20 Hz | At most 10 hits per second in a room and 8 per bulb. In practice it stays at or below 6 per second up to 174 BPM. |
| Sync to BPM | Every hit lands on a beat or an eighth note. Builds move at half time or bar rate, drops and peaks on the beat or eighth. |
| Low ambient, high contrast | Floors of 2–8 % between hits (20 % in the outro); peaks up to 100 % in drops and peaks. |
| Smooth dimming | Fades are done by the bulb itself (ZCL transitions). Only drops and peaks at full intensity use instant rises. |
| Two colors per cue, white for impact | One pair per section, a single tone in builds, white only on drops. |
| Progressive color storytelling | Cool and dark in builds, warm and high-contrast at the drop, back to amber in the outro. |

### Volume and intensity

- **Following the volume.** The lights soften when you turn the music down. Volume is read from the *Music player* option and compared with the loudest setting used recently, not with a fixed number. A Node at 4 % into a loud amp counts as party volume; turning it down to half of that makes every rise take at least 0.3 s, with no flashes, and roughly halves the brightness swing. HA can't see the amplifier's own knob.
- **The Intensity control** (`number.<name>_intensity`, 10–100 %) caps the swing outright. Use about 40 % for dinner (gentle, no hard flashes below 60 %) and 100 % for a party.

Timing jitter is about 50–150 ms. Zigbee allows only 10–20 frames per second before the rest of the house suffers, so this cannot match a Hue Sync Box's 25 Hz strobing.

## Requirements

- Home Assistant 2026.10 or later with ZHA, and the Music Assistant integration (used for the Light sync players).
- Music Assistant 2.10.7 or later. It provides the Sendspin visualizer and color roles, and beat analysis through Smart Fades.
- Color bulbs on ZHA. Built and tested for:

  | Bulb | Home Assistant model | Color sent as |
  |---|---|---|
  | Philips Hue White and Color Ambiance BR30 (R578096-4) | LCB002 | CIE xy |
  | Philips Hue White and Color Ambiance A19 (562785) | LCA005 | CIE xy |
  | Philips Hue White and Color Ambiance A19 (590851) | 9290022266 | CIE xy |
  | Third Reality color bulb | 3RCB01057Z | hue/saturation |

  Any other ZHA light can be added after a warning.

## Install (HACS)

1. In HACS, open the three-dot menu, choose **Custom repositories**, add this repository's URL, and set the category to **Integration**.
2. Install **Music Light Sync** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Music Light Sync** (later rooms: **Add entry** on the integration's page):
   - **Room**: the area, e.g. Dining Room. Leave **Lights** empty to take every ZHA color light in it, or pick them (a light group helper is expanded in member order).
   - **Name**: leave empty for `<Room> Lights`. This is the room's player in Music Assistant.
   - **Host** / **Port** (first room only): pre-filled from your Music Assistant integration, `8927`.
4. To change a room's area, name or lights later: the room's **⋮ → Reconfigure**. It keeps its Music Assistant player.

## One-time setup in Music Assistant

1. **Approve each room's player.** Go to **Settings → Players** and open the room's player (e.g. **Kitchen Lights**). Light players are hidden by default, so you may need to show hidden players first. Approve it with the one-click allow option rather than pairing. Until you do, the integration's Status sensor reads *Waiting for approval in Music Assistant*. Allowing it exposes nothing on the Home Assistant side, because the client only has the visualizer and color roles: no microphone and no audio output.
2. **Check the Node can join Sendspin groups.** Open the **Living Room** (Node) player and confirm AirPlay is listed among its protocols. Music Assistant uses AirPlay 2 to keep the Node in sync with Sendspin members.

## Daily use: two paths

| Path | How | Audio to the Node | Lights |
|---|---|---|---|
| **Synced** | Play to **Living Room - Light sync** (or group **Living Room** with any room's light player by hand) | AirPlay 2, capped at 24-bit/48 kHz | All effects, beat-accurate |
| **Native hi-res** | Play to **Living Room** alone | Native BluOS, full Tidal resolution | Optional: the Ambient palette from album art, via the fallback player |

The **Sync** switch arms the integration. While it is on and Music Assistant streams to the group:

1. The bulbs' current state and the list of paused entities are written to `.storage`.
2. The entities you chose to pause are turned off, and the bulbs switch on in palette colors.
3. When the music has been stopped for longer than *Restore after* (default 10 s), the bulbs return to exactly their pre-show state and the paused entities turn back on.

If Home Assistant stops, the integration hands back first. If it crashes or loses power mid-show, the hand-back runs at the next start. An unavailable bulb stays recorded and is retried every minute.

## Options (Configure)

| Option | Recommendation |
|---|---|
| Bulb layout | Where each bulb is in the room: one `x, y` line per bulb, in the order of the *Lights* list, 0–100 across and down as seen from above. Party mode builds its pairs, splits and chase paths from it. Without a layout the bulbs are treated as a row. The kitchen is below. |
| Music player | The room's Music Assistant player, e.g. `media_player.node_a7d8`. Party mode reads the volume, the time left in the track (for the outro) and track changes from it; the native hi-res path uses it for the ambient palette. |
| ZHA group | Recommended. In ZHA, create a group with exactly this room's bulbs and pick it here. Whole-room changes (a drop's white flash) then take one multicast frame instead of one per bulb, which keeps drops on time. The option is refused if the group has other members. |
| Light sync speakers | Which speakers' **- Light sync** players this room joins. All ticked (the default) also joins speakers added later. |
| Match other rooms | On (default): run one show with other matching rooms in the same group. |
| Color format | Automatic: each bulb gets its native format (Hue: CIE xy; Third Reality: hue/saturation, the same thing its *Native hue/saturation color* quirk switch does). Nothing on the bulbs is switched during a show, so there is nothing to put back. If Hue and Third Reality bulbs don't match side by side, force **Hue/saturation for every bulb** or **CIE xy for every bulb**. A ZHA group mixing formats sends colors bulb by bulb. |
| Zigbee frame budget | 20 frames/s for the whole house (the default). Rooms playing at the same time share it; the lowest value set in any room applies. The governor backs off by itself if the mesh gets congested. |
| Min. group spacing | 2 s. Multicast frames fill the coordinator's broadcast table, so don't go lower. |
| Min/Max brightness | 15 % / 100 % |
| Pause Adaptive Lighting during a show | On (default). Every Adaptive Lighting switch that controls this room's lights (found from Adaptive Lighting's own light lists, groups included) is turned off when the show starts and back on afterwards, if it was on. A switch shared by two rooms stays off until the last of them stops. The status sensor's `paused_during_show` attribute lists what will be paused. |
| Pause during a show | Other automations that would fight the show, e.g. the room's occupancy and illuminance automations. |
| Restore after | 10 s |

Kitchen layout (1 top center, 2 left upper, 3 right middle, 4 left lower, 5 right bottom; assumes the *Lights* list is in that order):

```
43, 13
22, 36
62, 48
23, 65
63, 81
```

## Entities

- `switch.<name>_sync`: arms or disarms the integration. Turning it off ends a running show and restores the lights.
- `select.<name>_effect`: picks the effect.
- `number.<name>_latency_offset`: positive values fire the lights earlier.
- `number.<name>_intensity`: how hard the effects hit, 10–100 %.
- `sensor.<name>_status`: the connection and show state. Its attributes show `bpm`, `pulse_source` (beats, onsets or none), `frames_per_second`, `rate_limit`, `zigbee_rtt_ms`, `lead_ms`, frame counters and the current palette.

### Calibrating

Pick **Beat breathe** and play something with a clear kick drum. Watch whether the brightness peaks land on the kick. Adjust **Latency offset** in steps of 20–30 ms: positive if the lights are late, negative if early. The integration already compensates the measured Zigbee delay, so this offset only covers the speaker side, mostly AirPlay buffering.

## Zigbee notes

- Party mode plans each beat 250 ms ahead. When a look changes, the new colors go out up to half a beat before the first hit, so colors and hits don't compete for airtime at the drop. At equal priority a color is sent before a brightness change, so a bulb never flashes up in its old color.
- Late frames are dropped, never played late: a hit that can't reach the bulb within 150 ms of its beat is skipped.
- A bulb that fails 4 frames in a row (typically switched off at the wall) is rested for 60 s instead of being retried on every beat; the status sensor lists it under `resting_bulbs`. Smart bulbs behind a wall switch need power all the time: put Inovelli switches in *smart bulb mode*.

- Every command is one ZCL frame, either `Level Control / move_to_level` or `Color Control / move_to_color`, with the bulb doing the fade. Frames go directly to ZHA's zigpy cluster objects, so the event bus is not flooded with `call_service` events. If ZHA's internals change, it falls back to the `zha.issue_zigbee_*_command` services.
- At most 6 frames are in flight at once, out of zigpy's 8 request slots, so other devices are slowed during a show but never starved.
- The bulbs report their new brightness and color back to ZHA, so the light entities update during a show, several times a second per bulb. That's history-database writes, nothing more. If you want them out of the history, add the bulbs to `recorder: exclude: entities:` in `configuration.yaml`. That part of the configuration can only be edited in YAML, so make it a managed edit: keep a copy of the file, run **Check configuration** in the **YAML** tab of **Tools**, then restart (recorder settings are not reloadable). Ask Claude to make the edit through Home Assistant's config API (it backs the file up and validates it first) rather than hand-editing.

## Limitations

- Beat tracking comes from Music Assistant's analysis. The first play of a track may not be analyzed yet; the integration then falls back to onsets, and to ambient if there are none.
- ZCL transitions use 0.1 s steps.
- The native hi-res path gets album art only. There is no beat data, because the lights are not part of that stream.

## Development

```
pip install homeassistant pytest-homeassistant-custom-component "aiosendspin[server]==10.2.0" zha==2.3.1 pillow
pytest
```

`tests/test_choreo.py` plays a simulated club track (intro, verse, build, drop, breakdown, build, drop, outro) at 90–174 BPM through the real engine. It checks the lighting rules: song structure, the two-color rule, white only on drops, strobe limits, on-grid hits, the frame budget, and gentle output at low volume and low intensity.

The integration test starts a real Sendspin server from the same `aiosendspin` version Music Assistant 2.10.7 uses. It approves the client, streams audio with a beat schedule, and checks that level ramps start on the beats, that the frame budget holds, and that the pre-show state is restored.
