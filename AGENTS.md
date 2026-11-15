# Bridge development

This project carries AirPlay audio through Shairport Sync to a UPnP/DLNA receiver.
The bridge serves FLAC audio, controls playback and volume, and provides a web display.
Keep shared instructions here. Keep `CLAUDE.md` as the single-line import `@AGENTS.md`.

## Read first

- Read [README.md](README.md) for current behavior, configuration, and test commands.
- Read [RELEASING.md](RELEASING.md) before changing release or image-publishing behavior.
- Read the [investigation record](docs/airplay-dlna-investigation.md) before proposing metadata, queue, encoding, latency, or continuity experiments.

## Requirements

- Carry audio with title, artist, album, and artwork updates. Keep the web display independent of receiver display updates.
- Prefer standard UPnP/DLNA control. Do not introduce proprietary receiver playback commands without an explicit scope change.
- Avoid additional lossy encoding. FLAC preserves decoded PCM (uncompressed sample audio), but it cannot restore quality lost in the source codec.
- Evaluate AAC passthrough before adopting AAC re-encoding as a permanent solution. Passthrough does not itself solve artwork updates.
- Treat receiver compatibility as tested behavior. Do not claim universal DLNA support from one receiver's results.

## Experiment constraints

- Identify a changed variable before repeating a completed experiment.
- Do not repeat failed ICY combinations without a changed variable. Do not propose removing `Play` after `SetAVTransportURI` as a new fix.
- Do not repeat finite-resource capability tests merely to reconfirm them. Address a distinct live-segmentation or continuity question.
- Keep the live rolling queue experimental. The recorded live tests did not meet acceptance requirements.
- Distinguish observer reports, measured results, source-author claims, and hypotheses.
- Do not equate PLAYING state, artwork requests, or intact connections with audible continuity or visible artwork updates.

## Volume during startup and testing

- Never set audio volume to 100% during startup or testing.
- Keep test playback strictly below 30% receiver volume. On integer controls from 0 to 100, use 29 or lower.
- Before test playback, make sure that the actual receiver volume and effective bridge cap are below 30%. Use `MAX_VOLUME=29` or lower in authorized test configuration. Do not rely on the default cap of 100.
- Apply the test limit during startup, resume, reconnect, and restoration. Do not restore a saved volume above the limit.
- If the actual receiver volume or effective cap cannot be established, do not start test playback.

## Live-test procedure

1. Obtain explicit permission before changing receiver playback, services, containers, configuration, or files. Obtain separate permission before SSH access.
2. Before controlling a receiver, make sure that the temporary encoder and HTTP server pass local checks.
3. Decode encoder output. For lossless tests, compare recovered PCM with the input and preserve 32-bit samples explicitly.
4. Make sure that served bytes and HTTP Range responses match the intended protocol behavior.
5. Preserve the effective deployment configuration. Do not substitute documentation-only example addresses during container recreation.
6. Prepare restoration and cleanup before starting a probe. Preserve replacement playback if another source takes receiver ownership.
7. After a probe, make sure that playback is restored and temporary processes and listening ports are gone.
8. Record the date, firmware, source revision/hash, changed variable, initial metadata, and observed payload. Record title and artwork results separately.
9. Record playback state, connection continuity, and observer reports separately. State unknowns and distinguish sampled lag from acoustic latency.

## Privacy and permissions

- Call private hosts the "test server" and use generic names for home-network devices in reports and public artifacts.
- Do not publish private hostnames, addresses, usernames, filesystem paths, credentials, or raw personal listening history.
- Use neutral source labels when exact music metadata is irrelevant. State when payload text is anonymized.
- Never merge a pull request or enable auto-merge without explicit approval of that specific pull request.
- Approval to implement, commit, push, publish, or open a pull request does not authorize merging.
- Keep new worktrees under `.worktrees/<task>/` inside this repository. Make sure that `/.worktrees/` remains in the root `.gitignore`.
