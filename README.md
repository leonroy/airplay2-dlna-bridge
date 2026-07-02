# airplay2-dlna-bridge

Stream **AirPlay 2** from your iPhone/Mac to any **UPnP/DLNA renderer** — with
lossless audio, track title/artist/album, cover art, and hardware volume
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
  no lossy transcoding
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
a UPnP/DLNA renderer on the same LAN.

```bash
git clone https://github.com/shinn-y/airplay2-dlna-bridge
cd airplay2-dlna-bridge
cp .env.example .env      # edit HOST_IP and RENDERER_IP(WiiM)
docker compose up -d --build
```

Pick "AirPlay 2 Bridge" from the AirPlay menu on your iPhone and play.

To show
a different name in the AirPlay menu, edit `general.name` in
`shairport-sync.conf` and restart the `shairport-sync` container.

## Configuration

Everything is in `.env` (see `.env.example` for details): `DIDL_PUSH` toggles
display updates, `MAX_VOLUME` caps the hardware volume, `STREAM_PORT` moves the
stream port. The AirPlay name is set in `shairport-sync.conf` (`general.name`).


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

## Credits

- [shairport-sync](https://github.com/mikebrady/shairport-sync) does all the
  AirPlay 2 heavy lifting (nqptp is bundled in its Docker image)
- [AirConnect](https://github.com/philippe44/AirConnect) — the classic-AirPlay
  equivalent and the inspiration for the UPnP control approach

## License

MIT
