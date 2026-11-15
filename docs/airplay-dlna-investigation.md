# AirPlay and DLNA investigation

This record covers tests from October 11–12, 2026, plus an earlier sender comparison.
It preserves the recorded results and their limits. No tests were repeated for this document.
Firmware, deployment, source, and configuration observations apply to the dates recorded below.
They do not describe the current deployment or authorize new access.

## Privacy and timing

Real music titles, artists, and albums use neutral `Source` labels.
Those labels preserve relationships between observations. They are not literal captured ICY payloads.
Synthetic `ICY Test A` and `ICY Test B` labels remain unchanged.
Local workspace paths and obsolete working-copy inventories are omitted.

Wall-clock times are replaced with relative times where the event sequence matters.
`t+N s` means N seconds after the timing origin for that test.
T02 uses its first recorded metadata arrival. T06 uses its first recorded receiver connection.
T12, T13, and T14 use their recorded test starts.
The offsets retain the recorded event spacing. They are not acoustic latency measurements.

## Outcome and artifact status

The tested ICY variations did not update the receiver's reported metadata as required.
Progressive queued FLAC changed artwork in the captured test. Two queued transitions had no listener-reported pause.
The live queue did not meet acceptance requirements. It remains unsuitable for production integration on this evidence.
The historical probe at `tools/progressive_queue_probe.py` is no longer present in this repository.
The test findings remain useful without that implementation.

## Requirements and working constraints

- Carry AirPlay audio to a UPnP/DLNA receiver with changing title, artist, album and artwork.
- Avoid audio interruptions caused by metadata updates. Keep the web display updating independently.
- Prefer standard UPnP/DLNA control and receiver-independent behaviour. WiiM-specific playback commands are outside the preferred approach.
- Avoid additional lossy encoding. Evaluate AAC passthrough before adopting AAC re-encoding as a permanent solution.

See `AGENTS.md` for [privacy and permissions](../AGENTS.md#privacy-and-permissions), [experiment constraints](../AGENTS.md#experiment-constraints), and the [live-test procedure](../AGENTS.md#live-test-procedure), including SSH permission.

## Verified versions and deployment

Observed on 2026-10-12, using read-only receiver queries and container inspection:

| Component | Observed version or state |
|---|---|
| Receiver hardware | WiiM Amp Ultra; device-description model number `V01-Sep 17 2026` |
| Receiver firmware | `Linkplay.5.2.829663`; release field `20260917`; build `release` |
| Bridge image | `airplay2-bridge:pr41-ghcr` |
| Bridge image ID | `sha256:0a573f129c7112b0d0aeefc1b007f09639173a5f8dbcc8688d4f163b81e74a43` |
| Bridge image revision label | `02efbaf24430ad3deab66c14bac778388d73cf5a` |
| Actual deployed `/app/server.py` SHA-256 | `ec4aaec81194b7ed40b217f18c70d838276b4496d3dd15f18fed21659a5b3213`; matches inspected local source |
| Local bridge checkout | `08485ea885b2aecda3b3bd5b976bdc805741f532` |
| Shairport image | `ghcr.io/leonroy/shairport-sync:latest` |
| Shairport image ID | `sha256:16fcefbaca051035b1a9c503fd324d36c705b9686fe5c681fdb2c0fa98bc0525` |
| Shairport image revision label | `f82d92e6b0ce47b7393f444c3a8cf87a332fd49c` |
| Shairport runtime version prefix | `f82d92e-dirty-AirPlay2`; do not treat the revision label as proof of a clean source build |
| Local upstream Shairport checkout | `b45f15b32882409a7d39b01293d43da3d5519416` |
| Selected test bridge | AirPlay 2, port 8000, `DIDL_PUSH=0` |
| Separate test endpoint | AirPlay 1, port 8001; do not confuse it with the selected AirPlay 2 bridge |
| Tested audio path | Buffered AirPlay 2 AAC, 48 kHz; PCM output 32-bit stereo; bridge HTTP FLAC, 48 kHz/32-bit stereo |

The firmware above was read on October 12. Firmware for earlier tests was not recorded at the time. Do not retrospectively assign the current firmware to those tests. Sender OS/app versions and the WiiM Home app version were not captured.

The image revision label and actual source hash are separate facts. Preserve both when identifying a reproduction.

## Completed tests and observations

| ID / date | Experiment | Observed behaviour | Evaluation |
|---|---|---|---|
| T01 / earlier session | iOS Music versus macOS Apple Music | Observer reported metadata/artwork working from iOS but missing from macOS Apple Music. | Separate sender-dependent issue. No verified cause or permanent resolution recorded. |
| T02 / Oct 11 | Normal playback with `DIDL_PUSH=1` | Title/art changes triggered `SetAVTransportURI` followed by `Play`. During “Source track 01”, metadata arrived at t+0 s, the push occurred around t+2 s, and the receiver disconnected at t+2.857 s and reconnected at t+3.893 s. Another track showed similar behaviour. No missing/late audio blocks were reported in the inspected status. | Metadata-driven stream restarts explain those observed interruptions. Late artwork can cause another metadata update. |
| T03 / Oct 11 | Original deployment with `DIDL_PUSH=0` | Bridge metadata/artwork changed from Source artist 01 to Source artist 02. Receiver artwork stayed on the previous album. | Already tested. Disabling repeated DIDL pushes alone did not refresh artwork. |
| T04 / Oct 11 | Same working URL, changed DIDL metadata, no subsequent `Play` | Selected receiver initially played Source artist 03. A metadata-only `SetAVTransportURI` changed it to captured Source artist 01 metadata/artwork. SOAP succeeded; receiver entered `STOPPED` within about 28 ms and stayed stopped during ten polls over five seconds. Source audio continued; receiver connection closed. Observer confirmed changed cover and stopped audio. | Removing `Play` does not prevent the stop. Restoring the prior metadata with the same command also left playback stopped. |
| T05 / Oct 11 | ICY negotiation and payload inspection | Receiver stream requests logged `icy=True`. A separate short inspection connection decoded a live block containing the current artist/title and artwork URL, matching bridge status. The inspection connection closed afterwards. | ICY was already being served. Client opt-in and valid payload do not prove that the receiver uses them on its display. |
| T06 / Oct 12 | Correctly configured selected bridge, `DIDL_PUSH=0`, DLNA startup plus FLAC/ICY | At t+0 s the receiver connected with `icy=True`. Bridge/web metadata advanced from “Source track 02” to “Source track 03”, “Source track 04” and later tracks. Receiver artwork stayed old. Standard `GetMediaInfo` retained “Source track 02”. A later read-only `getPlayerStatus` also retained “Source track 02” while the bridge reported “Source track 05”. | FLAC/ICY with initial DIDL metadata present did not update this receiver's display/reported track. This is a repeat failure with valid configuration. |
| T07 / Oct 12 | Audio continuity during T06 | Receiver audio connection remained open across the observed track changes. No further `SetAVTransportURI` or `Play` commands occurred after initial startup. AirPlay status reported zero missing/late blocks. Listener also reported an audio skip/glitch. | The reported glitch was not explained by a DIDL-driven restart. Its cause is unresolved; do not claim all audio issues are solved. |
| T08 / Oct 12 | Empty DIDL on the same FLAC/ICY URL, followed by standard UPnP Play | Commands succeeded; receiver returned PLAYING/OK with empty CurrentURIMetaData. It disconnected once and reconnected with icy=True. A fresh inspected ICY block carried Source artist 04 - Source track 06 and its artwork URL, but receiver getPlayerStatus reported the stream URL as title and Unknown artist/album. | Failed for artwork: observer confirmed the receiver still showed the earlier Source track 02 cover. On subsequent inspection the bridge played Source artist 05 / Source track 07 / Source album 02, while the receiver still reported a generic stream title and Unknown artist/album. Clearing DIDL did not resolve title or artwork updates in this tested path. This was a temporary receiver-state test, not a permanent bridge setting. |
| T09 / Oct 12 | Independent memory-only FLAC relay, empty DIDL, ICY interval 8192 instead of 131072 | Receiver requested ICY and stayed PLAYING/OK for 24 seconds. Relay emitted real title/artwork changes from Source track 07 to Source track 08. Receiver still reported its stream URL with Unknown artist/album. | Shorter metadata interval did not activate reported title updates. This was an independent relay comparison, not an installed Icecast reference server. |
| T10 / Oct 12 | Same FLAC relay and 8192-byte interval, controlled ASCII ICY Test A → ICY Test B with two artwork URLs | Both metadata blocks were emitted over one connection; receiver stayed PLAYING/OK for 36 seconds but reported only the stream URL. No receiver artwork GET was observed in bridge logs during the relay probes. | Simplifying title text and injecting artwork changes did not produce reported metadata updates. Physical screen was not separately confirmed for this probe; lack of artwork requests is supporting evidence, not proof that an image was never cached. |
| T11 / Oct 12 | Corrected AAC/ICY relay, 48 kHz stereo, temporary AAC 256 kb/s, interval 8192, empty DIDL | Relay framing roundtrip and live payload parsing passed. Encoded audio decoded successfully in FFmpeg. Receiver requested ICY, stayed PLAYING/OK for 36 seconds, and received ASCII Test A/B plus artwork URLs. Reported metadata stayed URL/Unknown. | No reported ICY title updates with AAC in this DLNA path. No new receiver artwork requests observed. Diagnostic lossy encoding only; permanent bridge output remains FLAC. |
| T12 / Oct 12 | Corrected MP3/ICY relay, 48 kHz stereo, temporary MP3 320 kb/s, otherwise same test as T11 | Live framing and decoder checks passed. Receiver requested ICY, stayed PLAYING/OK for 36 seconds, and received Test A/B on one connection. Reported metadata remained URL/Unknown; standard GetPositionInfo returned empty TrackMetaData. | Changing to MP3 did not activate reported metadata updates. Direct FLAC playback was restored and verified PLAYING/OK at t+40 s. |
| T13 / Oct 12 | Two finite eight-second FLAC resources with separate DIDL metadata, standard SetNextAVTransportURI before Play | Memory-only captured PCM was split into contiguous segments, encoded as 48 kHz/32-bit stereo FLAC, given known sample counts, and decoded back byte-for-byte. HTTP Range preflight passed. Receiver fetched A at t+1.497 s and prefetched B at t+2.681 s. Reported title/album changed to B around t+10 s; CurrentURI advanced to B. Every one-second poll across the A/B boundary remained PLAYING/OK. Receiver fetched distinct artwork URLs for A and B successfully. It stopped normally after B ended. Direct FLAC was restored and verified PLAYING/OK. | Positive standard-DLNA capability result: automatic queued resource, title/album and artwork-fetch transitions work on this firmware. No acoustic measurement or physical-screen confirmation was performed, so fully gapless sound and visible artwork refresh remain unproven. Artificial segment boundaries were used, not actual live AirPlay track boundaries. |
| T14 / Oct 12 | Rolling standard-DLNA queue: three finite eight-second FLAC resources; replenish next resource while the second is PLAYING | Memory-only contiguous PCM segments again passed byte-for-byte FLAC roundtrips and HTTP Range preflight. Receiver prefetched B. On B becoming current at t+10.896 s, SetNextAVTransportURI queued C at t+10.924 s; transport immediately remained PLAYING/OK. Receiver fetched C at t+11.051 s and advanced reported title/album and CurrentURI to C around t+18.645 s. C artwork was fetched successfully at t+18.182 s. One-second polling showed no STOPPED state across A/B or B/C transitions. It stopped normally after the final resource ended. | Positive rolling-queue capability result on firmware 5.2.829663. Standard next-resource control supports replenishment during playback and dynamic metadata/artwork requests. It is not yet a production live segmenter, a proof of audible gaplessness, or physical-screen confirmation. Direct FLAC and current real track metadata were restored; DIDL_PUSH remains 0. Temporary port closed, no temporary relay processes remained, and only the normal bridge FLAC encoder remained. |
| D02 / Oct 12 | First temporary AAC/MP3 relay implementation | An escaping error emitted literal backslash-x00 bytes instead of one zero byte for unchanged metadata. Receiver stopped these invalid streams. Only this identified temporary process was interrupted; its finally block restored direct FLAC playback. | Invalid run, excluded from codec conclusions. Corrected relay passed syntax, byte-framing, live payload and decoder checks before T11/T12. |
| D03 / Oct 12 | First finite-resource prototype preflight | Captured PCM and FLAC roundtrips passed, but an escaped Range regex failed the local HTTP Range check. The prototype stopped before sending any playback-changing command. | Invalid preflight, excluded from receiver conclusions. Corrected Range parser was verified for bounded, open and suffix requests before retrying. |
| D01 / Oct 11–12 | Deployment error when disabling DIDL | Container recreation loaded documentation-only example receiver/stream addresses from the Compose environment file. Control requests failed and no receiver audio client connected. The two previously verified working addresses were restored in the test override, and receiver queries succeeded again. | Exclude this invalid interval from ICY conclusions. Future recreations must preserve the effective configuration, including any shell-supplied overrides, rather than trusting Compose labels alone. A fresh AirPlay session was needed after bridge recreation to supply audio-format metadata. |

No repeated DIDL test demonstrated uninterrupted artwork refresh. The suspected playback-state bug was reported to WiiM.

## Code and protocol findings

- Bridge receiver control uses standard UPnP AVTransport and RenderingControl SOAP calls, not proprietary WiiM playback commands. Existing discovery assumptions do not prove universal DLNA compatibility.
- DIDL-Lite is standard UPnP metadata. ICY is an additional HTTP radio metadata mechanism, not a DLNA requirement.
- `DIDL_PUSH=0` suppresses subsequent DIDL updates but still sends initial DIDL metadata. Empty DIDL was tested in T08 and failed: artwork remained stale after an album change, while the reported title remained generic.
- ICY is enabled per connection only when the client sends `Icy-MetaData: 1`. Other clients receive plain FLAC.
- The inspected ICY interval was 131072 audio bytes. Metadata contains `StreamTitle` (artist/title) and `StreamUrl` (artwork URL), but no separate album field.
- Upstream author `syy` introduced ICY, including artwork URLs, in initial commit `1d20c3da35822383ac0a145303fd449494c00a6d` on July 3, 2026. Fork commit `35f68e0bdc6f9e8ae525ef59f480b277df7a3963` improved escaping, size limits and update handling.
- The tested path was AAC → decoded PCM → FLAC. FLAC adds no further lossy encoding and cannot restore quality lost in AAC. Re-encoding that PCM to AAC adds another lossy generation.
- In the inspected upstream Shairport source, `ap2_buffered_audio_processor.c` decrypts buffered AAC and adds ADTS framing before decoding. `player.c` sends packets to the decoder. The pipe output operates on PCM. A new pre-decoder output path is plausible but unimplemented.
- AAC passthrough must account for timing, pause/flush, decoder priming, backpressure and codec changes, including ALAC. It does not itself solve artwork display.
- The AVTransport:1 specification, section 2.4.1.3, describes temporary `TRANSITIONING` followed by `PLAYING` during resource loading. Resource-fetch failure can cause `STOPPED`. T04 supports a suspected playback-state compliance bug, but internal fetch failure was not conclusively excluded.
- Fixing the unexpected stop does not establish that metadata updates can be fully gapless. Ignoring ICY artwork is not itself a DLNA standards violation.

## Options considered before the later queue tests

These were proposals, not authorization to change services or code. The later T15–T18 results qualify the queue proposal.

1. Empty initial DIDL, same FLAC/ICY and standard DLNA control (T08 completed, failed). Empty metadata was accepted and playback continued, but reported title remained generic and the observer confirmed stale artwork after an album change. Do not repeat without a new variable.
2. ICY framing comparisons completed (T09/T10). Shorter interval and controlled ASCII/artwork changes failed to update reported metadata. Icecast was not installed and no actual Icecast reference comparison was performed. Do not equate the independent Python relay with a known reference implementation.
3. AAC/MP3 ICY capability tests completed (T11/T12). Both valid temporary codecs played but failed to update reported metadata in this DLNA path. AAC passthrough remains an efficiency/fidelity development option, not an evidence-based artwork fix.
4. Finite-resource standard next-track queuing: positive T13/T14 capability results. Receiver prefetched and advanced resources, reported title/album and artwork requests. T14 also queued the following resource while the current resource was PLAYING, without a sampled stop at either boundary. Remaining work is a live rolling segmenter, metadata/audio alignment, buffering/latency, pause/flush, source/format changes and audible-continuity/physical-screen verification. Fixed-duration chunks can avoid requiring exact song boundaries. This requires SetNextAVTransportURI support and appropriate receiver behaviour; it is not universal DLNA compatibility.
5. Explicit compromises. Send DIDL only when artwork changes to reduce interruption frequency, or retain continuous playback with artwork in the web display. Neither meets the complete requirement.

Proprietary WiiM network-stream startup remains untested and outside the preferred approach. Do not describe it as already tested or automatically equivalent to the app's Open Network Stream feature.

See the [experiment constraints](../AGENTS.md#experiment-constraints) before proposing further tests based on these results.

## Sources and future evidence

- [UPnP AVTransport:1 specification](https://upnp.org/specs/av/UPnP-av-AVTransport-v1-Service.pdf)
- [Upstream bridge display/gap findings](https://github.com/shinn-y/airplay2-dlna-bridge#display-updates-and-the-track-change-gap). The author's “ICY ignored in every mode” claim is historical, not proof about every current firmware/path.
- [AirConnect documentation](https://github.com/philippe44/AirConnect#readme). It documents ICY metadata with AAC/MP3 on compatible receivers; it does not prove WiiM artwork support.
- [upmpdcli radio documentation](https://www.lesbonscomptes.com/upmpdcli/pages/radios.html). External UPnP renderer metadata support is conditional; its richer OpenHome artwork path is not evidence of universal DLNA artwork updates.
- [ICY negotiation documentation](https://cast.readme.io/docs/icy)
- [WiiM Amp Ultra firmware release notes](https://faq.wiimhome.com/en/support/solutions/articles/72000645823-wiim-amp-ultra-firmware-release-notes). ICY support announced for TuneIn/iHeartRadio does not establish arbitrary DLNA stream support.
- [WiiM HTTP API](https://www.wiimhome.com/pdf/HTTP%20API%20for%20WiiM%20Products.pdf). A documented URL-play command is not a standalone current-artwork setter.

For future tests and evidence records, follow the [live-test procedure](../AGENTS.md#live-test-procedure), [volume limits](../AGENTS.md#volume-during-startup-and-testing), and [privacy rules](../AGENTS.md#privacy-and-permissions) in `AGENTS.md`.

## T15 / Oct 12: progressive finite-resource local preflight

- Added an isolated memory-only probe at `tools/progressive_queue_probe.py`; production bridge code and configuration were not changed by this work.
- Changed variable from T13/T14: FLAC resources are served before encoding finishes, without HTTP Content-Length while growing. Encoder input is released at audio-clock speed. This tests progressive delivery rather than requiring complete eight-second files before playback.
- Local two-second 48 kHz/32-bit stereo checks passed: first FLAC audio frame around 0.13–0.14 seconds after input began; progressive HTTP bytes matched encoder output; decoded PCM matched input exactly. HEAD, simultaneous requests, reconnect from byte zero, completed bounded/open/suffix ranges and unsatisfiable ranges passed.
- Growing resources ignore Range requests with a full HTTP 200 response. Completed resources support exact lengths and HTTP 206 ranges. Unknown FLAC MD5 is zero; known sample count is supplied in STREAMINFO.
- This is an encoder/HTTP preflight only. No receiver firmware query, SSH, receiver playback change, acoustic continuity measurement or physical artwork confirmation occurred in this local run. It does not prove receiver compatibility or end-to-end latency.
- The prepared receiver probe captures 24 seconds while direct playback continues, then progressively serves three eight-second resources with one-second initial buffering and distinct test artwork. It uses standard next-resource queueing and a restoration path guarded by source session identity.
- Explicit SSH and temporary playback-test permission was subsequently granted for the selected test server. T16 establishes progressive receiver compatibility; the live two-second queue remains a separate test.

## T16 / Oct 12: progressive eight-second receiver queue

- Receiver firmware reverified as `Linkplay.5.2.829663`, release `20260917`, project WiiM_Amp_Ultra. Bridge `DIDL_PUSH=0` unchanged. Source was Source artist 06 / Source track 09 as reported by the observer.
- Changed variable from T13/T14: resources were encoded and served progressively at simulated PCM-clock speed, with one second of source buffering, unknown compressed HTTP length while growing, and known eight-second FLAC sample counts. This remains a captured fixture, not a live bridge.
- Corrected run: Play issued about 1.05 seconds after simulated source start. Receiver first fetched A while growing around source time 2.15 seconds and reached sampled PLAYING around 2.76 seconds. B and C were fetched while growing around source times 8.19 and 16.19 seconds.
- Standard next-resource transitions updated reported titles and artwork. B became current around source time 11.14 seconds and C around 18.94 seconds, estimated URI transition lag 3.14 and 2.94 seconds. These observations do not measure acoustic end-to-end latency. No sampled STOPPED state occurred at A/B or B/C boundaries.
- Observer visually confirmed red, green and blue images. Listener heard an initial pause entering red, no pause entering green, no pause entering blue, and another pause when switching back to direct playback. This supports audible continuity for these two queued transitions; it does not prove sample-level gaplessness or long-term stability.
- C stopped normally after its final audio. Direct FLAC with current real metadata was restored and verified PLAYING/OK. Temporary encoder threads exited. Later inspection found the temporary port closed; source had subsequently become idle.
- Result: progressive finite FLAC resources and dynamic artwork work in this tested standard-UPnP path without waiting for complete eight-second files. Sampled lag was about three seconds, under the five-second rejection threshold but above the two-second target. The next proposal at that stage was the live two-second queue. T18 records its later rejection. Do not repeat this capability test unchanged.

## D04 / Oct 12: invalid immediate-startup classification

- First progressive receiver run sampled STOPPED about 28 milliseconds after Play, before any audio GET, and prematurely aborted. It restored direct playback successfully.
- Exclude this run from receiver compatibility conclusions. Corrected probe allows bounded startup before rejecting STOPPED; T16 then demonstrated progressive playback and queued transitions.

## T17 / Oct 12: live-ingestion preflight

- Added an opt-in live two-second queue to the isolated probe. Production bridge source, defaults, containers and settings remain unchanged by this work.
- Local and selected-server FFmpeg preflights passed: actual WAV reader handled arbitrary byte fragments, two adjacent two-second FLAC resources decoded to the exact concatenated input PCM with no missing/repeated/reordered samples, first frames appeared around 0.08–0.11 seconds, expected source EOF was detected, and temporary processes were cleaned up.
- Prepared live mode reads shared PCM through the diagnostic WAV endpoint, snapshots track metadata per resource, bounds retained resources/bytes, and reads fresh bridge logs for pause/resume/flush. It rejects source/format replacement, queue stalls and excessive sampled lag and restores direct playback on exit while preserving replacement receiver ownership.
- These are ingestion/encoder preflight results only. Live tests were subsequently attempted; see D05 and T18. Ten-minute stability, live continuity and pause/resume acceptance remain unverified.

## D05 / Oct 12: first live probe input-buffer limitation

- Firmware remained Linkplay.5.2.829663; DIDL_PUSH=0. First live two-second queue crossed 53 resource boundaries over about 110 seconds, with sampled source lag around 3.1–3.3 seconds and real metadata changes.
- The probe then rejected bursty PCM because its encoder-input queue limited message count rather than byte count. This is a harness limitation, not evidence of receiver incompatibility. Direct playback was restored and verified PLAYING/OK.
- Replaced that queue with a byte-bounded buffer permitting one complete two-second resource. Added and passed local and selected-server exact-PCM full-resource-burst preflights before retrying. Do not repeat the old packet-count configuration.

## T18 / Oct 12: corrected live probe stopped after observer report

- Changed variable: byte-bounded encoder input after D05. Production bridge and settings unchanged. Firmware Linkplay.5.2.829663; DIDL_PUSH=0.
- Corrected run crossed at least 74 two-second boundaries over roughly 151 seconds. Sampled URI/source lag remained around 3.2–3.4 seconds, with four retained resources at sampled health checks. Reported metadata included Source artist 07 / Source track 10 / Source album 03 and later Source artist 08 / Source track 11. This is protocol evidence, not physical artwork or acoustic verification.
- Observer reported repeated pause/resume, a skip from Source artist 09 to Source artist 07, artwork delayed roughly 30 seconds, and a brief silence when artwork finally changed. This does not meet live acceptance requirements.
- Log timestamps place the reported pause sequence after the first run restored direct playback and before the corrected retry began. Therefore those pauses were not exercised inside the corrected queue. The first failure/restoration and retry startup are possible sources of the silence; exact causal attribution and artwork-delay cause remain unknown. Do not label this a confirmed WiiM queue bug.
- Stopped the corrected probe after the negative observer report. Subsequent independent inspection verified direct FLAC URI, PLAYING/OK, temporary port closed, no probe/log-follow process, and only the normal bridge encoder remaining.
- Ten-minute stability, live pause/flush handling, physical artwork timing and acoustic continuity remain unproven. Three-second sampled lag exceeds the two-second target, though below the five-second rejection threshold; it is not an acoustic latency measurement. Do not integrate into production or repeat unchanged. Any further experiment needs a distinct diagnostic variable and clean observation timing that excludes switching between direct and probe playback.
