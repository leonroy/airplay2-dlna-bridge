# airplay2-dlna-bridge

Stream **AirPlay 2** from your iPhone/Mac to any **UPnP/DLNA renderer** — with
FLAC audio, track title/artist/album, cover art, and hardware volume
control. Built for and tested on the **WiiM Ultra** (which has no AirPlay of
its own), but the renderer side is generic UPnP.

```
iPhone ──AirPlay 2──▶ shairport-sync ──PCM pipe──▶ bridge ──FLAC/HTTP──▶ renderer
                       (nqptp included)             ├─ hosts cover art (/art-N.jpg)
                                                    ├─ auto play/stop (UPnP AVTransport)
                                                    ├─ volume → UPnP RenderingControl
                                                    └─ track info → DIDL-Lite push
```

## Features

- **Lossless**: PCM from AirPlay is repackaged as FLAC (`-compression_level 0`),
  with no lossy transcoding in the bridge. This does not guarantee end-to-end
  lossless playback: the bridge cannot restore quality lost before receiving PCM.
- **Now playing on the renderer's display**: title / artist / album / cover art,
  refreshed on every track change (see *Display updates* below)
- **Volume**: the phone's volume slider drives the renderer's hardware volume
  via UPnP RenderingControl (with a configurable safety cap)
- **Auto start/stop**: selecting the AirPlay endpoint starts playback on the
  renderer; disconnecting stops it
- **Self-contained**: two containers, no host dependencies; the renderer's
  control endpoints are discovered automatically from its `description.xml`


## Quick start

Requirements: Docker with compose, host networking available (mDNS/PTP),
a UPnP/DLNA renderer on the same LAN, and Shairport Sync 5 or later.

```bash
git clone https://github.com/leonroy/airplay2-dlna-bridge
cd airplay2-dlna-bridge
cp .env.example .env      # edit HOST_IP and RENDERER_IP(WiiM)
docker compose pull
docker compose up -d
```

Pick "AirPlay 2 Bridge" from the AirPlay menu on your iPhone and play.

The bridge image is published to `ghcr.io/leonroy/airplay2-dlna-bridge` for
Linux amd64 and arm64. Set `BRIDGE_IMAGE_TAG` in `.env` to a release version
(without `v`) to pin it, or use `latest` to follow new releases. See
[RELEASING.md](RELEASING.md) for publishing and rollback details.

For local development, build the checked-out source with:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

To show
a different name in the AirPlay menu, edit `general.name` in
`shairport-sync.conf` and restart the `shairport-sync` container.

## Configuration

Everything is in `.env` (see `.env.example` for details): `DIDL_PUSH` toggles
display updates, `MAX_VOLUME` caps the hardware volume, `STREAM_PORT` moves the
stream port. The AirPlay name is set in `shairport-sync.conf` (`general.name`).

### PCM output format

Shairport's configuration selects its output, either fixed or automatic. Python
does not read that configuration: it reads the resulting `ssnc/odsc` description
(such as `48000/S24_3LE/2`) and derives the WAV header, FFmpeg input and buffers.
Keep `metadata.enabled = "yes"` and the shared metadata pipe configured.

The supplied configuration uses 16-bit/44.1 kHz stereo. For 24-bit/48 kHz, set
these values in the `pipe` block:

```conf
output_rate = 48000;
output_format = "S24_3LE";
output_channels = 2;
```

All ten Shairport integer formats are supported, including big-endian and
padded 24-bit samples. Normalization preserves sample values. Output must be
mono or stereo; `odsc` does not specify a wider channel layout. FLAC stores
8-bit input as 16-bit samples; full 32-bit FLAC requires FFmpeg's experimental
encoder support and a compatible renderer.

HTTP playback waits for `pbeg` and a valid `odsc`, returning 503 until ready.
Early audio is buffered up to 4 MiB; the bridge never guesses a missing format.
At session end, queued input is drained and late EOFs cannot close a newer session.
After restarting only the bridge, disconnect and reconnect AirPlay to obtain
a fresh description.

For automatic selection, use `output_rate = "auto"` and a format list such as
`output_format = ("S16_LE", "S24_3LE")`. New-session formats are accepted; a
different `odsc` within one session stops playback. The separate metadata pipe
provides no audio byte offset, and unannounced changes cannot be detected.
Keep fixed output until automatic transitions pass live playback tests.

To measure live FLAC encoder startup and verify exact sample round trips, run
`python3 -B tests/flac_startup.py --benchmark --roundtrip --repeats 3`. The helper
must run inside the bridge image being evaluated; host FFmpeg timings do not
establish the container's behavior. It compares default input probing with
experimental `-probesize 32 -analyzeduration 0`
options. Native ARM64 Alpine 3.24 with FFmpeg 8.1.2 emitted its first audio frame
in approximately 80–93 ms with either command; probing changes showed no material
gain, so the production command remains unchanged. Local FFmpeg 7.1 improved
from approximately 4.3–4.6 seconds to 80–93 ms, which is an older-runtime result.
These timings start just before the first PCM write and end at the first frame
sync after FLAC metadata; they do not measure complete decoding or audible
playback. Queued bridge audio can also change the result. The metadata debounce
and the renderer's stream restart on display updates remain separate sources of
latency. The exact deployment image digest recorded in the maintenance handoff
(`sha256:8d9c2c694d1fa3dc05b45921ac8da646e342ec75b7387302b888c8cbcb2359c2`)
also contains FFmpeg 8.1.2 and showed the same result in local ARM64 tests.
The AMD64 test server also runs FFmpeg 8.1.2; probe changes were omitted.

### Diagnosing format metadata

Logs correlate `sdsc` (incoming format), `odsc` (pipe output), session boundaries,
pipe closures and HTTP/encoder activity using UTC timestamps and local session
IDs. They report description timing, byte counts and rejection reasons. IDs
reset on bridge restart. Follow both containers when reproducing a problem:

```bash
docker compose logs --timestamps --follow bridge shairport-sync
```

While audio waits for `odsc`, buffer status is logged at most every five seconds.

Playback timing records use `timing event=... session=... revision=...`. They
report `playback_ready` (the metadata quiet period has elapsed),
`command_start`/`command_end` (action, attempt, SOAP result and duration),
`flac_connected`, the first `flac_pcm` feed and the first `flac_frame` sync after
complete FLAC metadata. The FLAC events report milliseconds from the HTTP
request; `flac_frame` also reports time from the first PCM feed. They occur once
per connection, without titles, artwork or PCM payloads. Command success means
a valid SOAP response, not audible playback. Frame detection precedes writing
to the socket and does not measure renderer buffering or audible sound.
Playback command revisions identify metadata snapshots; Stop, volume and resume
use their own command revisions. A FLAC connection records the metadata
revision observed when the request arrived, which can differ from the URI's
revision if metadata changed meanwhile. Session IDs reset on bridge restart.

### Initial playback timing

Initial playback uses a fixed **0.5-second** metadata quiet period. The bridge
still requires `pbeg` and a valid `odsc`, and uses the shorter interval only
when a nonempty title is available. There is no configuration toggle.
Title-less startup retains the ordinary two-second fallback. Each changed
title/artwork event restarts the quiet period; identical updates do not.

All later display updates retain the two-second debounce, and `DIDL_PUSH=0`
still sends one initial URI with no display refreshes. Artwork arriving after
the initial quiet period can require another URI and stream restart with
`DIDL_PUSH=1`; metadata arriving during an in-flight URI cancels the stale Play
and schedules the latest snapshot. A shorter interval can therefore increase
restarts for senders with late metadata. FFmpeg flags, PCM samples and stream
backlog are unchanged.

Live AMD64 tests compared three fresh starts with the previous two-second
interval and five with the half-second interval. Median `odsc` acceptance to
first FLAC frame improved from **2.391 to 0.871 seconds**; median `pbeg` to first
frame improved from **3.285 to 1.694 seconds**. All trials used S32/48 kHz stereo,
but tracks differed between the baseline and faster trials. No command failures
or artwork-only extra initial resets were observed. Four faster trials used one
URI and one FLAC connection; the fifth's two additional refreshes followed
actual title changes and retained the two-second mid-play debounce.

These are small-sample results from one sender/renderer setup, and frame sync
is measured before socket writing, not at audible output. When testing another
setup, repeat cold playback, sender takeover and late artwork; also check track
changes, pause/resume and seeking. Record sender-button and audible-start times
externally, then correlate session logs from `pbeg`/`odsc` through
`playback_ready`, each URI/Play command and `flac_frame`. Count URI sends and
reconnects alongside delay.


## Display updates and the track-change gap

At least on WiiM/LinkPlay firmware, the on-device display renders **only**
UPnP DIDL-Lite metadata — ICY/Shoutcast in-stream metadata is ignored in every
mode (verified against the WiiM HTTP API). The only way to refresh the display
is re-sending `SetAVTransportURI`, and the renderer restarts its stream
connection on every such push. The bridge therefore debounces metadata into a
single push per track change, which costs a **~2–4 s audio gap when the track
changes**. If you prefer fully gapless audio with a static display, set
`DIDL_PUSH=0`.

Expect ~4–6 s of end-to-end latency (AirPlay 2 clock + the renderer's own HTTP
prebuffer). This is inherent to the double-buffered chain; volume and
play/pause react much faster.

## Multiroom

**Do not group this bridge with real AirPlay 2 speakers** — the renderer's HTTP
prebuffer is seconds long, varies per connection, and drifts (its DAC clock is
not disciplined to the AirPlay PTP timeline), so the rooms will be seconds
apart with no way to correct it. This is a protocol-level limitation of any
re-streaming bridge.

Multiroom among LinkPlay devices works fine the other way around: group your
other WiiM/LinkPlay speakers behind the renderer in the WiiM Home app (firmware
handles tight sync between them), and AirPlay to the bridge as a single
endpoint.

## Credits

- [shairport-sync](https://github.com/mikebrady/shairport-sync) does all the
  AirPlay 2 heavy lifting (nqptp is bundled in its Docker image)
- [AirConnect](https://github.com/philippe44/AirConnect) — the classic-AirPlay
  equivalent and the inspiration for the UPnP control approach

## License

MIT
