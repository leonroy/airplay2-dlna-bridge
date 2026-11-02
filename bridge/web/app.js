"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const demoParam = new URLSearchParams(location.search).get("demo");
  const demo = demoParam !== null;
  let source = null, snapshot = null, lastStatus = 0, panelView = null, offline = false;
  let retryTimer = null, retryDelay = 1000;
  let lastSequence = 0, instance = null, lines = [], logBytes = 0;
  let logRenderPending = false, logQuery = "", logIssuesOnly = false;
  let following = true, unreadLogs = 0, pendingLogCount = 0;
  let logConnection = demo ? "Demo" : "Connecting…";
  const encoder = new TextEncoder();
  const labels = { idle: "Ready for AirPlay", receiving: "Receiving AirPlay", waiting: "Waiting for audio format", error: "Audio session error" };
  const SPEAKER_STATES = {
    PLAYING: { label: "Playing on ", active: true }, PAUSED_PLAYBACK: { label: "Paused on ", held: true },
    STOPPED: { label: "Stopped on ", held: true }, NO_MEDIA_PRESENT: { label: "Nothing playing on ", held: true },
    TRANSITIONING: { label: "Starting on " }
  };
  const playbackLabels = { PLAYING: "Playing", PAUSED_PLAYBACK: "Paused", STOPPED: "Stopped", TRANSITIONING: "Starting", NO_MEDIA_PRESENT: "No media" };
  // Status fields such as failed=False are ordinary lines, so only real failures count.
  const ISSUE = /\b(error|errors|rejected|timed out|timeout|exceeded|lost)\b|\bfailed\b(?!=False)/i;
  const SOURCE = /^(ALAC|AAC|PCM)\/([0-9]{4,6})\/([A-Z0-9_]+)\/([1-8]|5\.1|7\.1)$/;
  const calm = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
  const esc = (text) => text.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const timing = (name, fallback) => CSS.supports("transition-timing-function", "linear(0, 1)") ? getComputedStyle(document.documentElement).getPropertyValue(name).trim() : fallback;
  const OUT = "cubic-bezier(.2,.8,.2,1)", ACCEL = "cubic-bezier(.5,0,.8,.4)";
  const SPRING = timing("--spring", OUT), POP = timing("--pop", OUT);

  // One wrapper so every animation respects reduced motion.
  const play = (el, frames, options) => (calm() || !el) ? null : el.animate(frames, { easing: SPRING, fill: "backwards", ...options });
  const rise = (el, delay = 0, distance = 14) => play(el, [{ opacity: 0, translate: `0 ${distance}px`, filter: "blur(8px)" }, { opacity: 1, translate: "0 0", filter: "blur(0)" }], { duration: 760, delay });

  const duration = (value) => value == null ? "—" : `${Math.floor(value / 60)}m ${Math.floor(value % 60)}s`;
  const bytes = (value) => value >= 1048576 ? `${(value / 1048576).toFixed(1)} MiB` : `${(value / 1024).toFixed(1)} KiB`;
  const channelLabel = (channels) => channels === "1" ? "Mono" : channels === "2" ? "Stereo" :
    ["5.1", "7.1"].includes(channels) ? channels : `${channels} channels`;
  const receivedFormat = (description) => {
    const match = typeof description === "string" && description.match(/^(ALAC|AAC|PCM)\/([0-9]{4,6})\/[A-Z0-9_]+\/([1-8]|5\.1|7\.1)$/);
    return match ? `${match[1]} · ${channelLabel(match[3])} · ${Number(match[2]) / 1000} kHz` : "Unknown";
  };
  const outputFormat = (stream) => stream ?
    `${stream.codec} · ${channelLabel(String(stream.channels))} · ${stream.rate / 1000} kHz · ${stream.bits}-bit` : "Unknown";
  const inputBitrate = (audio) => ["AAC", "ALAC"].includes(audio.codec) &&
    Number.isFinite(audio.input_bitrate_bps) && audio.input_bitrate_bps > 0
      ? `${Math.round(audio.input_bitrate_bps / 1000)} kbps (measured average)` : "";
  // Only local artwork routes are accepted, even if a malformed status supplies a URL.
  const safeArt = (art) => /^\/art-[0-9a-f]{64}\.jpg$/.test(art) || art === "/demo.svg" ? art : "";

  /* ---------- Bitrate history: this page load only; playback boundaries only break the line ---------- */
  const bitrateHistory = [];
  let historyGap = true, historyIdentity = null, drawnAt = 0, drawnY = 0;
  const CHART_H = 56, WINDOW_MS = 60000;
  function recordBitrate(data) {
    const now = Date.now(), audio = data.audio;
    const identity = JSON.stringify([data.instance, audio.session, audio.source_format]);
    if (identity !== historyIdentity) historyGap = true;
    historyIdentity = identity;
    if (audio.state === "receiving" && inputBitrate(audio)) {
      const previous = bitrateHistory.at(-1);
      bitrateHistory.push({at: now, value: audio.input_bitrate_bps / 1000,
        gap: historyGap || (previous && now - previous.at > 5000)});
      if (bitrateHistory.length > 30) bitrateHistory.shift();
      historyGap = false;
    } else historyGap = true;
    drawHistory();
  }
  // Catmull-Rom through every reading of a run: smooth, but it still passes through the data.
  function smooth(points) {
    const fixed = (n) => n.toFixed(2);
    let d = `M${fixed(points[0][0])},${fixed(points[0][1])}`;
    if (points.length === 1) return `${d} L${fixed(points[0][0])},${fixed(points[0][1])}`;
    for (let i = 0; i < points.length - 1; i++) {
      const p0 = points[Math.max(i - 1, 0)], p1 = points[i], p2 = points[i + 1], p3 = points[Math.min(i + 2, points.length - 1)];
      const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6], c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
      d += ` C${fixed(c1[0])},${fixed(c1[1])} ${fixed(c2[0])},${fixed(c2[1])} ${fixed(p2[0])},${fixed(p2[1])}`;
    }
    return d;
  }
  function drawHistory() {
    const latest = bitrateHistory.at(-1), chart = $("bitrate-chart"), svg = $("bitrate-history");
    const shown = !!latest && snapshot?.audio.state !== "idle";
    chart.hidden = !shown;
    $("bitrate-stat").hidden = !shown;
    $("audio-stats").hidden = !shown && $("tempo-stat").hidden;
    svg.dataset.count = bitrateHistory.length;
    if (!latest) { drawnAt = 0; return; }
    const first = bitrateHistory[0], age = Math.max(0, (Date.now() - latest.at) / 1000);
    const values = bitrateHistory.map((point) => point.value);
    const low = Math.min(...values), high = Math.max(...values);
    // The window is a fixed 60 seconds anchored at the newest reading; x follows the reading's time.
    const inWindow = bitrateHistory.filter((point) => latest.at - point.at <= WINDOW_MS);
    const near = inWindow.map((point) => point.value), lowest = Math.min(...near), highest = Math.max(...near);
    // At least 10% of the reading is always in view, so small drift doesn't look dramatic.
    const span = Math.max(highest - lowest, (highest + lowest) / 2 * 0.1, 1), floor = (highest + lowest - span) / 2;
    const y = (value) => 6 + (1 - (value - floor) / span) * (CHART_H - 12);
    const runs = [];
    inWindow.forEach((point, index) => {
      if (!index || point.gap) runs.push([]);
      runs.at(-1).push([100 - (latest.at - point.at) / WINDOW_MS * 100, y(point.value)]);
    });
    $("bitrate-line").setAttribute("d", runs.map(smooth).join(" "));
    const top = `${(y(latest.value) / CHART_H * 100).toFixed(1)}%`;
    if (latest.at !== drawnAt) {
      const slide = (latest.at - drawnAt) / WINDOW_MS * 100;
      if (shown && drawnAt && !latest.gap && !calm()) {
        $("bitrate-glide").animate([{ transform: `translateX(${slide}px)` }, { transform: "translateX(0)" }], { duration: 450, easing: OUT });
        $("trace-dot").animate([{ top: `${(drawnY / CHART_H * 100).toFixed(1)}%` }, { top }], { duration: 450, easing: OUT });
      }
      drawnAt = latest.at; drawnY = y(latest.value);
      roll($("bitrate-now"), String(Math.round(latest.value)));
    }
    $("trace-dot").style.setProperty("--y", top);
    svg.dataset.stale = String(age > 5);
    $("bitrate-foot").textContent = `Min ${Math.round(low)} kbps · Max ${Math.round(high)} kbps · History ${duration((latest.at - first.at) / 1000)} · Latest ${Math.floor(age)}s ago${age > 5 ? " (old)" : ""}`;
  }
  setInterval(drawHistory, 1000);

  // Characters that changed roll into place. The ones that stayed never move.
  function roll(el, text) {
    const before = el.textContent;
    if (before === text) return;
    if (!before || calm()) { el.textContent = text; return; }
    const was = [...before], now = [...text];
    el.setAttribute("aria-label", text);
    el.innerHTML = now.map((c) => `<span class="char" aria-hidden="true">${esc(c)}</span>`).join("");
    let changed = 0;
    [...el.children].forEach((span, i) => {
      if (was[i] === now[i]) return;
      span.animate([{ translate: "0 0.3em", opacity: 0 }, { translate: "0 0", opacity: 1 }], { duration: 300, delay: changed++ * 18, easing: OUT, fill: "backwards" });
    });
  }
  function crossText(el, text) {
    if (el.textContent === text) return;
    const first = !el.textContent;
    el.textContent = text;
    if (!first) play(el, [{ opacity: 0, translate: "0 6px", filter: "blur(4px)" }, { opacity: 1, translate: "0 0", filter: "blur(0)" }], { duration: 520 });
  }

  /* ---------- Details ---------- */
  const observed = (field) => field.at ? `${Math.max(0, Math.floor(Date.now() / 1000 - field.at))} seconds ago` : "Not yet observed";
  const fieldValue = (field, volume = false) => {
    if (field.error === "unavailable") return "Unavailable";
    if (field.error) return "Connection problem";
    if (field.value == null) return "Checking…";
    const text = volume ? `${field.value}%` : playbackLabels[field.value] || "Unknown";
    return field.stale ? `${text} (stale)` : text;
  };

  function details(data) {
    const groups = [
      ["Bridge", [
        ["Bridge", demo ? "Demo" : "Online"], ["Uptime", duration(data.uptime_seconds)],
        ["Audio connections", data.connections],
        ["Last command", data.command ? `${data.command.name} · ${data.command.accepted ? "Accepted" : "Failed"}` : "None"]]],
      ["AirPlay session", [
        ["AirPlay input", labels[data.audio.state] || "Unknown"], ["Session", data.audio.session ?? "None"],
        ["Session age", duration(data.audio.age_seconds)],
        ["AirPlay version", data.audio.airplay_version ? `AirPlay ${data.audio.airplay_version}` : "Unknown"],
        ["Missing audio blocks (session)", data.audio.receiver_stats?.missing_audio_blocks ?? "Unavailable"],
        ["Too-late audio blocks (session)", data.audio.receiver_stats?.too_late_audio_blocks ?? "Unavailable"],
        ["Retry requests (session)", data.audio.receiver_stats?.retry_requests ?? "Unavailable"]]],
      ["Audio", [
        ["Output stream", outputFormat(data.audio.output_stream)],
        ["Received from AirPlay", receivedFormat(data.audio.source_format)],
        ...(["AAC", "ALAC"].includes(data.audio.codec) ? [["Input bitrate (measured average)", inputBitrate(data.audio) || "Unavailable"]] : []),
        ["Buffered audio", bytes(data.audio.buffered_bytes)], ["Waiting for format", bytes(data.audio.pending_bytes)],
        ["Received audio", bytes(data.audio.raw_bytes)], ["Discarded audio", bytes(data.audio.discarded_bytes)]]],
      ["Speaker", [
        ["Recipient playback", data.recipient.configured ? fieldValue(data.recipient.playback) : "Not configured"],
        ["Playback observation", observed(data.recipient.playback)],
        ["Recipient volume", data.recipient.configured ? fieldValue(data.recipient.volume, true) : "Not configured"],
        ["Volume observation", observed(data.recipient.volume)]]]
    ];
    $("details").replaceChildren(...groups.map(([name, rows]) => {
      const group = document.createElement("div"), heading = document.createElement("h3"), list = document.createElement("dl");
      heading.textContent = name;
      list.append(...rows.map(([label, value]) => {
        const row = document.createElement("div"); row.className = "detail-row";
        const term = document.createElement("dt"); term.textContent = label;
        const definition = document.createElement("dd"); definition.textContent = String(value);
        row.append(term, definition); return row;
      }));
      group.append(heading, list); return group;
    }));
  }

  /* ---------- What the sender delivered. The tag never describes the FLAC the bridge creates. ---------- */
  const BARS = `<svg width="14" height="12" viewBox="0 0 14 12" fill="currentColor" aria-hidden="true"><rect data-eq x="0" y="4" width="2" height="4" rx="1"/><rect data-eq x="4" y="1" width="2" height="10" rx="1"/><rect data-eq x="8" y="0" width="2" height="12" rx="1"/><rect data-eq x="12" y="3" width="2" height="6" rx="1"/></svg>`;
  const SURROUND = `<svg width="16" height="12" viewBox="0 0 16 12" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><circle cx="8" cy="6" r="1.4" fill="currentColor" stroke="none"/><path d="M4.6 3a4.5 4.5 0 0 0 0 6M11.4 3a4.5 4.5 0 0 1 0 6M1.8 1a8 8 0 0 0 0 10M14.2 1a8 8 0 0 1 0 10"/></svg>`;
  function audioInfo(data, target) {
    const audio = data.audio, input = typeof audio.source_format === "string" && audio.source_format.match(SOURCE);
    const kbps = inputBitrate(audio) ? Math.round(audio.input_bitrate_bps / 1000) : 0;
    const output = audio.output_stream ? ["FLAC", `${channelLabel(String(audio.output_stream.channels))} · ${audio.output_stream.rate / 1000} kHz · ${audio.output_stream.bits}-bit`] : ["Unknown", ""];
    if (!input) return { tags: "", spec: "Unknown format", title: "Unknown format", copy: "The sender hasn’t reported the format of this audio.", received: ["Unknown", ""], output };
    const [, codec, hz, sample, channels] = input, rate = Number(hz) / 1000;
    const bits = codec !== "AAC" && sample.match(/^S(16|24|32)(?:_|$)/)?.[1];
    const multi = !["1", "2"].includes(channels), kHz = `${rate} kHz`;
    let tag, title, copy, spec;
    if (codec === "ALAC") {
      const high = Number(bits) >= 24 && Number(hz) > 48000;
      tag = high ? `<span class="tag hires">${BARS}Hi-Res Lossless</span>` : `<span class="tag lossless">${BARS}Lossless</span>`;
      title = high ? "Hi-Res Lossless" : "Lossless";
      copy = `The sender is delivering ${high ? "high-resolution" : "full-quality"} audio, and it stays lossless all the way to ${target}.`;
    } else if (codec === "AAC") {
      tag = `<span class="tag plain">AAC</span>`; title = "AAC";
      copy = `The sender compressed this audio before sending it. The bridge passes it on as FLAC, so nothing further is lost on the way to ${target}.`;
    } else {
      tag = `<span class="tag plain">PCM</span>`; title = "PCM";
      copy = `The sender is delivering uncompressed audio, and it stays lossless all the way to ${target}.`;
    }
    spec = codec === "AAC" ? `${kbps ? `${kbps} kbps · ` : ""}${kHz}` : `${bits ? `${bits}-bit / ` : ""}${kHz}`;
    if (multi) copy += ` The bridge sends stereo, so it reaches ${target} mixed down to two channels.`;
    const surround = ["5.1", "7.1"].includes(channels) ? `<span class="tag">${SURROUND}${channels}</span>` : "";
    const received = [codec, `${channelLabel(channels)} · ${kHz}${bits ? ` · ${bits}-bit` : ""}${codec === "AAC" && kbps ? ` · ${kbps} kbps` : ""}`];
    return { tags: surround + tag, spec, title, copy, received, output };
  }

  /* ---------- What the page should show, from the status and the connection ---------- */
  function model() {
    const data = snapshot, recipient = data?.recipient, audio = data?.audio, track = data?.track || {};
    const name = recipient?.name || "", target = name || "the speaker", Target = name || "The speaker";
    const base = { title: "", artist: "", album: "", note: "", eyebrow: "", art: safeArt(track.artwork || ""), look: "", label: name || "AirPlay Bridge", warn: false, volume: null, tags: false, rest: false, live: "" };
    const trackLabel = track.title ? (track.title.length > 34 && track.artist ? track.artist : track.artist ? `${track.title} · ${track.artist}` : track.title) : "";
    const problem = (key, eyebrow, title, note) => ({ ...base, mode: "problem", key, eyebrow, title, note, look: "dim", label: trackLabel || base.label, live: title });
    const fresh = recipient?.configured && recipient.volume.value != null && !recipient.volume.error && !recipient.volume.stale;
    if (offline) return problem("offline", "Reconnecting…", "Can’t reach the bridge", "This page lost its connection. The track shown may be out of date.");
    if (!data) return { ...base, mode: "connecting", key: "connecting", title: "Connecting to bridge…", art: "", live: "Connecting to bridge" };
    if (audio.state === "idle") return { ...base, mode: "ready", key: "ready", title: "Ready for AirPlay", note: "Select the bridge from your AirPlay menu.", art: "", volume: fresh ? recipient.volume.value : null, live: "Ready for AirPlay" };
    if (audio.state === "waiting") return problem("waiting", "Connected to AirPlay", "Waiting for audio format", "Playback starts once the receiver reports the format of the audio.");
    if (audio.state === "error") return problem("error", "Playback stopped", "Audio session failed", "Open Connection for the session state and recent details.");
    if (recipient.configured && (recipient.playback.error === "connection" || recipient.volume.error === "connection"))
      return problem("speaker", "Playback interrupted", `${Target} isn’t responding`, "Audio is still reaching the bridge. Check that the speaker is on and connected to your network.");
    const stale = recipient.configured && recipient.playback.stale;
    // Audio reaching the bridge says nothing about the speaker, so only its reported state counts.
    // A speaker with no usable report may still be playing, so that wording stays neutral.
    const speaker = recipient.configured && !stale ? SPEAKER_STATES[recipient.playback.value] : null;
    const held = !!speaker?.held, active = !speaker || speaker.active;
    const known = !!track.title;
    const title = known ? track.title : "Track information unavailable";
    const label = stale ? `${Target} status is out of date` : !recipient.configured ? "Receiving AirPlay" : speaker ? `${speaker.label}${target}` : `Sending to ${target}`;
    return { ...base, mode: "track", key: "track", title, artist: known ? track.artist || "" : "AirPlay audio", album: known ? track.album || "" : "", look: held ? "paused" : "", label, warn: stale, active,
      volume: fresh ? recipient.volume.value : null, tags: true, rest: active && !stale,
      live: stale || !recipient.configured || !speaker?.active ? `${label}: ${title}` : known ? `Playing ${title}` : "Playing AirPlay audio", audioInfo: audioInfo(data, target) };
  }

  /* ---------- Colour field and cover ---------- */
  // Covers sit in their own stack above the colour field, so the art is never hidden by a new background.
  let visualKey = null, paintToken = 0, currentLook = "", failedArt = "", rate = 1, rateFrame = 0;
  const prepared = new Map();
  // Decode the image and read its palette before anything is shown. A bad image rejects.
  function prepare(src) {
    if (!prepared.has(src)) {
      const task = new Promise((resolve, reject) => {
        const image = new Image();
        image.onerror = () => reject(new Error("cover"));
        image.src = src;
        image.decode().then(() => resolve(palette(image)), reject);
      });
      task.catch(() => prepared.delete(src));
      prepared.set(src, task);
      if (prepared.size > 8) prepared.delete(prepared.keys().next().value);
    }
    return prepared.get(src);
  }
  // The colour field: the cover's most characterful colours, each pulled into a range white type can sit on.
  function palette(img) {
    const fallback = ["#15131a", "#2a2733", "#1f1d27", "#24222c", "#191720"];
    try {
      const c = document.createElement("canvas"); c.width = c.height = 48;
      const x = c.getContext("2d"); x.drawImage(img, 0, 0, 48, 48);
      const d = x.getImageData(0, 0, 48, 48).data, bins = new Map();
      for (let i = 0; i < d.length; i += 4) {
        const r = d[i] / 255, g = d[i + 1] / 255, b = d[i + 2] / 255;
        const max = Math.max(r, g, b), min = Math.min(r, g, b), l = (max + min) / 2, delta = max - min;
        // Chroma, not HSL saturation, which reads near-white creams as fully saturated.
        const s = delta;
        let h = 0;
        if (delta) h = max === r ? ((g - b) / delta + 6) % 6 : max === g ? (b - r) / delta + 2 : (r - g) / delta + 4;
        h *= 60;
        const key = s < 0.12 ? "n" : String(Math.round(h / 24));
        const bin = bins.get(key) || { n: 0, h: 0, s: 0, l: 0, score: 0 };
        bin.n++; bin.h += h; bin.s += s; bin.l += l;
        bin.score += key === "n" ? 0.12 : 0.25 + s * (1 - Math.abs(l - 0.5));
        bins.set(key, bin);
      }
      const ranked = [...bins.values()].sort((p, q) => q.score - p.score).map((b) => ({ h: b.h / b.n, s: b.s / b.n, l: b.l / b.n }));
      const tones = ranked.slice(0, 4);
      while (tones.length < 4) tones.push({ ...tones[tones.length - 1], l: tones[tones.length - 1].l * 0.82 });
      const paint = ({ h, s, l }) => `hsl(${h.toFixed(0)} ${Math.min(82, s * 165 + 4).toFixed(0)}% ${Math.max(15, Math.min(42, l * 62)).toFixed(0)}%)`;
      const base = tones[0];
      return [`hsl(${base.h.toFixed(0)} ${Math.min(50, base.s * 70).toFixed(0)}% 10%)`, ...tones.map(paint)];
    } catch { return fallback; }
  }
  const topCover = () => $("covers").lastElementChild;

  // Returns true when this is a new cover replacing an earlier one, so the title can follow the art.
  function paintVisual(art, look) {
    currentLook = look;
    if (art === visualKey) {
      [...$("visual").lastElementChild?.querySelectorAll(".mesh") ?? [], topCover()].forEach((node) => {
        if (!node) return;
        node.classList.remove("dim", "paused");
        if (look) node.classList.add(look);
      });
      return false;
    }
    const wasRoom = visualKey === "", first = visualKey === null, newArt = !!art;
    visualKey = art;
    const token = ++paintToken;
    const build = (colors) => {
      if (token !== paintToken) return;
      const layer = document.createElement("div");
      layer.className = "layer";
      layer.innerHTML = art
        ? `<div class="mesh ${currentLook}"><i></i><i></i><i></i><i></i></div>`
        : `<div class="ready-bg"></div><div class="glow" aria-hidden="true">${[0, 1, 2, 3, 4].map((i) => `<i style="--d:${-i * 2.4}s;--s:${0.2 + i * 0.18}"></i>`).join("")}<b></b></div>`;
      colors?.forEach((c, i) => layer.style.setProperty(`--c${i}`, c));
      const oldLayers = [...$("visual").children], oldCovers = [...$("covers").children];
      oldCovers.forEach((node) => node.querySelector("img")?.removeAttribute("id"));
      $("visual").append(layer);
      let hero = null;
      if (art) {
        hero = document.createElement("div");
        hero.className = `hero ${currentLook}`;
        const img = document.createElement("img");
        img.id = "artwork"; img.src = art; img.alt = "";
        hero.append(img);
        $("covers").append(hero);
        coverAlt(snapshot);
      }
      if (rate !== 1) layer.querySelectorAll(".mesh i").forEach((n) => n.getAnimations().forEach((a) => a.updatePlaybackRate(Math.max(rate, 0.0001))));
      if (calm()) {
        oldLayers.forEach((n) => n.remove());
        oldCovers.forEach((n) => n.remove());
      } else if (first) {
        play(layer, [{ opacity: 0 }, { opacity: 1 }], { duration: 1400, easing: "ease" });
        if (hero) {
          play(hero, [{ opacity: 0 }, { opacity: 1 }], { duration: 700, delay: 120, easing: "ease-out" });
          play(hero, [{ scale: 0.9 }, { scale: 1 }], { duration: 1300, delay: 120 });
        }
      } else {
        // The colour field crossfades slowly underneath. The old cover stays solid until the new one fully covers it.
        const fade = play(layer, [{ opacity: 0 }, { opacity: 1 }], { duration: 1000, easing: "ease" });
        fade.onfinish = () => oldLayers.forEach((n) => n.remove());
        if (hero) {
          const inFade = play(hero, [{ opacity: 0 }, { opacity: 1 }], { duration: 480, easing: "ease-out" });
          play(hero, wasRoom ? [{ scale: 0.55 }, { scale: 1 }] : [{ scale: 1.025 }, { scale: 1 }], { duration: wasRoom ? 1200 : 900, easing: wasRoom ? POP : SPRING });
          inFade.onfinish = () => oldCovers.forEach((n) => n.remove());
        } else {
          oldCovers.forEach((n) => { n.animate([{ opacity: 1, scale: 1 }, { opacity: 0, scale: 0.96 }], { duration: 420, easing: "ease-in", fill: "forwards" }).onfinish = () => n.remove(); });
        }
      }
    };
    if (!art) build(null);
    else prepare(art).then((colors) => build(colors), () => {
      // An image that will not load is treated as no artwork at all.
      if (token !== paintToken) return;
      failedArt = art; apply();
    });
    return newArt && !first;
  }
  const coverAlt = (data) => {
    const img = $("artwork"), track = data?.track;
    if (img) img.alt = `Cover art for ${track?.album || track?.title || "the current track"}`;
  };

  // Background motion eases to a stop on pause rather than freezing.
  function setRate(target) {
    cancelAnimationFrame(rateFrame);
    if (calm()) return;
    const start = rate, began = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - began) / 1100);
      rate = start + (target - start) * (t * (2 - t));
      $("visual").querySelectorAll(".mesh i").forEach((n) => n.getAnimations().forEach((a) => a.updatePlaybackRate(Math.max(rate, 0.0001))));
      if (t < 1) rateFrame = requestAnimationFrame(step);
    };
    rateFrame = requestAnimationFrame(step);
  }

  /* ---------- Text that changes moves out, then in ---------- */
  let headKey = null, headToken = 0, tagsKey, tagsToken = 0, booted = false, wasPaused = false, wakeKey = "";
  const head = () => [$("eyebrow"), $("title"), document.querySelector(".by"), $("note")];
  function headIn(base) {
    const eyebrow = $("eyebrow"), words = $("title").querySelectorAll(".w");
    if (!eyebrow.hidden) rise(eyebrow, base, 8);
    words.forEach((w, i) => play(w, [{ opacity: 0, translate: "0 0.42em", filter: "blur(10px)" }, { opacity: 1, translate: "0 0", filter: "blur(0)" }], { duration: 900, delay: base + 40 + i * 62 }));
    document.querySelectorAll(".by, .note").forEach((n) => { if (!n.hidden) rise(n, base + 150 + words.length * 62, 10); });
  }
  async function setHead(m, base) {
    const key = [m.mode, m.key, m.title, m.artist, m.album, m.note, m.eyebrow].join("\u0001");
    if (key === headKey) return false;
    const shown = headKey !== null;
    headKey = key;
    const token = ++headToken;
    if (shown && !calm()) {
      const leaving = [...document.querySelectorAll("#head .w, .by, .note, .eyebrow")].filter((n) => !n.hidden && n.getClientRects().length);
      await Promise.all(leaving.map((n, i) => n.animate([{ opacity: 1, translate: "0 0", filter: "blur(0)" }, { opacity: 0, translate: "0 -10px", filter: "blur(6px)" }], { duration: 200, delay: Math.min(i * 12, 60), easing: ACCEL, fill: "forwards" }).finished.catch(() => {})));
      if (token !== headToken) return false;
    }
    head().forEach((n) => n.getAnimations().forEach((a) => a.cancel()));
    const title = $("title"), words = m.title.split(" ").filter(Boolean);
    title.className = m.title.length > 44 ? "longer" : m.title.length > 22 ? "long" : "";
    title.replaceChildren(...words.flatMap((word, i) => {
      const span = document.createElement("span"); span.className = "w"; span.textContent = word;
      return i ? [" ", span] : [span];
    }));
    $("eyebrow").hidden = !m.eyebrow;
    $("eyebrow").lastElementChild.textContent = m.eyebrow;
    document.querySelector(".by").hidden = m.mode !== "track";
    $("artist").textContent = m.artist; $("album").textContent = m.album;
    $("note").hidden = !m.note; $("note").textContent = m.note;
    headIn(base);
    return true;
  }
  function tagsIn(base) {
    document.querySelectorAll("#tags .tag").forEach((t, i) => play(t, [{ opacity: 0, scale: 0.72, filter: "blur(4px)" }, { opacity: 1, scale: 1, filter: "blur(0)" }], { duration: 760, delay: base + i * 70, easing: POP }));
    rise($("spec"), base + 120, 6);
  }
  async function setTags(info, base) {
    const el = $("tags"), key = info ? info.tags : null;
    if (key !== tagsKey) {
      tagsKey = key;
      const token = ++tagsToken;
      if (!el.hidden && !calm()) {
        const leaving = [...el.querySelectorAll(".tag, .spec")];
        await Promise.all(leaving.map((n, i) => n.animate([{ opacity: 1, translate: "0 0", filter: "blur(0)" }, { opacity: 0, translate: "0 -10px", filter: "blur(6px)" }], { duration: 200, delay: Math.min(i * 12, 60), easing: ACCEL, fill: "forwards" }).finished.catch(() => {})));
        if (token !== tagsToken) return;
        el.getAnimations({ subtree: true }).forEach((a) => a.cancel());
      }
      el.querySelectorAll(".tag").forEach((n) => n.remove());
      if (!info) { el.hidden = true; $("spec").textContent = ""; return; }
      el.insertAdjacentHTML("afterbegin", info.tags);
      el.hidden = false;
      $("spec").textContent = info.spec;
      tagsIn(base);
    }
    if (info) {
      roll($("spec"), info.spec);
      el.setAttribute("aria-label", `Audio quality: ${info.title}, ${info.spec}. Show details.`);
    }
  }

  /* ---------- Speaker capsule ---------- */
  // The capsule grows and shrinks to fit rather than snapping.
  function morphWidth(el, change) {
    const before = el.getBoundingClientRect().width;
    change();
    const after = el.getBoundingClientRect().width;
    if (!calm() && booted && Math.abs(before - after) > 1 && innerWidth > 760) {
      el.style.overflow = "hidden";
      el.animate([{ width: `${before}px` }, { width: `${after}px` }], { duration: 620, easing: SPRING }).onfinish = () => { el.style.overflow = ""; };
    }
  }
  let shownVolume = null, volumeTimer = null;
  function setVolume(value) {
    const vol = $("vol"), show = value != null;
    clearTimeout(volumeTimer);
    if (show) {
      if (vol.hidden) { vol.hidden = false; vol.getBoundingClientRect(); }
      vol.classList.remove("off");
      $("vol").querySelector(".level").style.setProperty("--v", String(value / 100));
      $("vol").querySelector(".level").setAttribute("aria-label", `Speaker volume ${value} percent`);
      roll($("volume"), `${value}%`);
      if (shownVolume !== null && shownVolume !== value && !calm()) $("vol").querySelector(".level").animate([{ height: "4px" }, { height: "7px" }, { height: "4px" }], { duration: 480, easing: "ease-out" });
    } else if (!vol.hidden) {
      vol.classList.add("off");
      const done = () => { vol.hidden = true; $("volume").textContent = ""; };
      if (calm() || !booted) done(); else volumeTimer = setTimeout(done, 600);
    }
    shownVolume = value;
  }
  function setRoom(m) {
    const label = $("room-label"), sig = `${m.warn ? "!" : ""}${m.label}`;
    morphWidth($("room"), () => {
      if (label.dataset.sig !== sig) {
        label.dataset.sig = sig; label.classList.toggle("warn", m.warn);
        if (m.warn) {
          const dot = document.createElement("span"); dot.className = "dot warn-dot";
          label.replaceChildren(dot, m.label);
        } else if (label.querySelector(".warn-dot")) label.textContent = m.label;
        else crossText(label, m.label);
      }
      $("eq").classList.toggle("off", m.mode !== "track");
      setVolume(m.volume);
    });
  }

  /* ---------- Pulse: the colour field breathes with the music ---------- */
  const pulse = { live: false, gain: 0, anchor: 0, period: 0, shift: 0, level: 0, lvl: 0 };
  let pulseFrame = 0, pulseTime = 0;
  const validPulse = (p) => p && Number.isFinite(p.bpm) && p.bpm >= 30 && p.bpm <= 300 && Number.isFinite(p.next_beat_in) && p.next_beat_in >= 0 && Number.isFinite(p.level);
  function readPulse(p, live) {
    if (!live || !validPulse(p)) { pulse.live = false; return; }
    const now = performance.now(), period = 60000 / p.bpm, anchor = now + p.next_beat_in * 1000;
    // Keep the running beat unless it drifted more than 60ms, then ease it across instead of jumping.
    if (pulse.period) {
      pulse.anchor += Math.floor((now - pulse.anchor) / pulse.period) * pulse.period;
      let diff = (((anchor - pulse.anchor) % period) + period) % period;
      if (diff > period / 2) diff -= period;
      pulse.shift = Math.abs(diff) > 60 ? diff : 0;
    } else pulse.anchor = anchor;
    pulse.period = period; pulse.level = Math.max(0, Math.min(1, p.level)); pulse.live = true;
    startPulse();
  }
  function startPulse() {
    if (pulseFrame || !pulse.period || calm() || document.hidden) return;
    pulseTime = performance.now();
    pulseFrame = requestAnimationFrame(pulseStep);
  }
  function resetPulse() {
    $("visual").style.transform = ""; $("beat-light").style.opacity = ""; $("beat-mark").style.opacity = "";
    document.querySelectorAll("#tags rect[data-eq], #eq i").forEach((bar) => { bar.style.transform = ""; });
    pulse.gain = 0; pulse.period = 0; pulse.shift = 0;
  }
  function pulseStep(now) {
    pulseFrame = 0;
    if (document.hidden || calm()) { resetPulse(); return; }
    const dt = Math.min(0.1, (now - pulseTime) / 1000); pulseTime = now;
    const target = pulse.live ? 1 : 0;
    pulse.gain += (target - pulse.gain) * Math.min(1, dt * (target ? 0.9 : 3));
    if (!target && pulse.gain < 0.002) { resetPulse(); return; }
    pulseFrame = requestAnimationFrame(pulseStep);
    pulse.lvl += (pulse.level - pulse.lvl) * Math.min(1, dt * 2);
    if (pulse.shift) { const move = pulse.shift * Math.min(1, dt * 2.5); pulse.anchor += move; pulse.shift -= move; }
    const periodMs = pulse.period, period = periodMs / 1000;
    const since = ((((now - pulse.anchor) % periodMs) + periodMs) % periodMs) / 1000;
    // A quick swell and a long, soft release, limited so very fast music can't strobe.
    const attack = Math.min(1, since / 0.05), release = Math.exp(-Math.max(0, since - 0.05) / Math.max(0.16, period * 0.42));
    const beat = attack * release, level = pulse.lvl;
    const amount = beat * (0.45 + 0.55 * level) * pulse.gain;
    $("visual").style.transform = amount > 0.001 ? `scale(${(1 + amount * 0.028).toFixed(4)})` : "";
    $("beat-light").style.opacity = (amount * 0.2).toFixed(3);
    $("beat-mark").style.opacity = (0.25 + 0.75 * beat * pulse.gain).toFixed(2);
    // The glyph bars show the same beat, rippling out from the tallest bar.
    const barAt = (i, count) => {
      const away = Math.abs(i - (count - 1) * 0.6), delayed = since - away * 0.028;
      const hit = delayed < 0 ? 0 : Math.min(1, delayed / 0.04) * Math.exp(-Math.max(0, delayed - 0.04) / Math.max(0.12, period * 0.34));
      return hit * (0.55 + 0.45 * level) * pulse.gain * (1 - away * 0.14);
    };
    const tagBars = document.querySelectorAll("#tags rect[data-eq]");
    tagBars.forEach((bar, i) => { bar.style.transform = `scaleY(${(1 - 0.5 * pulse.gain + 0.8 * barAt(i, tagBars.length)).toFixed(3)})`; });
    const roomBars = document.querySelectorAll("#eq i");
    roomBars.forEach((bar, i) => { bar.style.transform = `scaleY(${(0.3 + 0.75 * barAt(i, roomBars.length)).toFixed(3)})`; });
  }

  /* ---------- Resting: the controls step back while music plays ---------- */
  let restTimer = null, restable = false;
  function wake() {
    $("stage").classList.remove("rest");
    clearTimeout(restTimer);
    if (!restable || !$("panel").hidden) return;
    restTimer = setTimeout(() => {
      const focused = document.activeElement;
      if (!$("stage").contains(focused) || !focused.matches(":focus-visible")) $("stage").classList.add("rest");
    }, 6000);
  }
  ["pointermove", "pointerdown", "keydown", "focusin"].forEach((type) => document.addEventListener(type, wake, { passive: true }));

  /* ---------- The cover leans gently towards the pointer and catches light ---------- */
  const lean = { rx: 0, ry: 0, tx: 0, ty: 0, lift: 0, tl: 0, frame: 0 };
  function leanStep() {
    lean.rx += (lean.tx - lean.rx) * 0.09;
    lean.ry += (lean.ty - lean.ry) * 0.09;
    lean.lift += (lean.tl - lean.lift) * 0.08;
    const hero = topCover();
    if (hero) {
      hero.style.setProperty("--rx", `${lean.rx.toFixed(3)}deg`);
      hero.style.setProperty("--ry", `${lean.ry.toFixed(3)}deg`);
      hero.style.setProperty("--lift", lean.lift.toFixed(3));
    }
    const settled = Math.abs(lean.tx - lean.rx) + Math.abs(lean.ty - lean.ry) + Math.abs(lean.tl - lean.lift) < 0.005;
    lean.frame = settled || document.hidden ? 0 : requestAnimationFrame(leanStep);
  }
  $("stage").addEventListener("pointermove", (e) => {
    const hero = topCover();
    if (e.pointerType !== "mouse" || calm() || !$("panel").hidden || !hero) return;
    const r = hero.getBoundingClientRect();
    const nx = (e.clientX - (r.left + r.width / 2)) / innerWidth, ny = (e.clientY - (r.top + r.height / 2)) / innerHeight;
    lean.ty = Math.max(-1, Math.min(1, nx * 2)) * 4;
    lean.tx = Math.max(-1, Math.min(1, ny * 2)) * -3;
    const inside = e.clientX > r.left && e.clientX < r.right && e.clientY > r.top && e.clientY < r.bottom;
    lean.tl = inside ? 1 : 0.25;
    hero.style.setProperty("--mx", `${((e.clientX - r.left) / r.width * 100).toFixed(1)}%`);
    hero.style.setProperty("--my", `${((e.clientY - r.top) / r.height * 100).toFixed(1)}%`);
    if (!lean.frame) lean.frame = requestAnimationFrame(leanStep);
  });
  $("stage").addEventListener("pointerleave", () => {
    lean.tx = lean.ty = lean.tl = 0;
    if (!lean.frame) lean.frame = requestAnimationFrame(leanStep);
  });

  /* ---------- Render ---------- */
  function apply() {
    const m = model(), data = snapshot, base = booted ? 0 : 420;
    const trackChanged = paintVisual(m.art === failedArt ? "" : m.art, m.look);
    if (m.art && m.art !== failedArt) coverAlt(data);
    setHead(m, base + (trackChanged ? 120 : 40)).then((changed) => { if (changed) wake(); });
    setTags(m.tags ? m.audioInfo : null, base);
    setRoom(m);
    const paused = m.look === "paused";
    if (paused !== wasPaused) setRate(paused ? 0.02 : 1);
    wasPaused = paused;
    restable = m.rest;
    if (wakeKey !== `${m.mode}${m.rest}`) { wakeKey = `${m.mode}${m.rest}`; wake(); }
    if ($("live").textContent !== m.live) $("live").textContent = m.live;
    renderAudio(m);
    readPulse(data?.audio.pulse, m.mode === "track" && m.active);
    if (!booted) { rise($("room"), 980, 12); booted = true; }
  }

  function setFormat(el, [codec, rest]) {
    const sig = `${codec}|${rest}`;
    if (el.dataset.sig === sig) return;
    el.dataset.sig = sig;
    if (!codec) { el.replaceChildren(); return; }
    const strong = document.createElement("b"), tail = document.createElement("span");
    strong.textContent = codec; tail.textContent = rest;
    el.replaceChildren(strong, ...(rest ? [" ", tail] : []));
  }
  function renderAudio(m) {
    const data = snapshot, active = !!data && data.audio.state !== "idle";
    const info = m.audioInfo || (active && data ? audioInfo(data, data.recipient.name || "the speaker") : null);
    $("quality-title").textContent = info ? info.title : "No audio yet";
    $("quality-copy").textContent = info ? info.copy : "Audio quality details appear once a sender starts playing.";
    $("path").hidden = !info;
    setFormat($("codec"), info ? info.received : ["", ""]);
    setFormat($("format"), info ? info.output : ["", ""]);
    const speaker = data?.recipient.configured;
    $("path-room-row").hidden = !speaker;
    $("path-room").firstElementChild.textContent = data?.recipient.name || "Speaker";
    $("path-room").lastElementChild.textContent = m.volume != null ? `${m.volume}%` : "";
    const tempo = validPulse(data?.audio.pulse) ? Math.round(data.audio.pulse.bpm) : null;
    $("tempo-stat").hidden = tempo === null;
    if (tempo !== null) roll($("tempo-now"), String(tempo));
    drawHistory();
  }

  function render(data) {
    snapshot = data; offline = false;
    recordBitrate(data);
    lastStatus = Date.now();
    if (instance !== data.instance) {
      instance = data.instance;
      lastSequence = 0; lines = []; logBytes = 0;
      $("log-output").textContent = "";
      following = true; unreadLogs = 0; pendingLogCount = 0;
      updateLogState();
    }
    if (failedArt && failedArt !== safeArt(data.track.artwork || "")) failedArt = "";
    details(data);
    apply();
  }

  /* ---------- Logs ---------- */
  function atLogBottom() {
    const output = $("log-output");
    return output.scrollHeight - output.clientHeight - output.scrollTop <= 8;
  }

  function stopLogPulse() {
    $("log-state").classList.remove("log-pulse");
  }

  const lineShown = (item) => (!logIssuesOnly || item.issue) && (!logQuery || item.lower.includes(logQuery));
  // Text and the highlighted part go in as text nodes, so log content is never parsed as markup.
  function paintLine(item) {
    const node = item.node, show = lineShown(item);
    node.hidden = !show;
    if (!show || item.painted === logQuery) return;
    item.painted = logQuery;
    const add = (parent, text) => {
      const lower = text.toLowerCase();
      let at = 0, hit = logQuery && lower.length === text.length ? lower.indexOf(logQuery) : -1;
      while (hit >= 0) {
        parent.append(text.slice(at, hit));
        const mark = document.createElement("mark"); mark.textContent = text.slice(hit, hit + logQuery.length);
        parent.append(mark); at = hit + logQuery.length; hit = lower.indexOf(logQuery, at);
      }
      parent.append(text.slice(at));
    };
    const stamp = item.line.match(/^\[bridge\] \S+/)?.[0] || "";
    node.replaceChildren();
    if (stamp) { const dim = document.createElement("span"); dim.className = "stamp"; add(dim, stamp); node.append(dim); }
    add(node, item.line.slice(stamp.length));
    node.append("\n");
  }

  function updateLogState(state = logConnection) {
    logConnection = state;
    $("log-state").textContent = state;
    $("new-logs").hidden = following || !unreadLogs;
    $("new-logs").textContent = `${unreadLogs} new ↓`;
    $("new-logs").setAttribute("aria-label", `Show ${unreadLogs} new log entries`);
    $("log-state").classList.toggle("log-pulse", following && state === "Live" && panelView === "logs" && !$("panel").hidden && !document.hidden && !calm());
    const visible = lines.filter(lineShown).length, filtered = logQuery || logIssuesOnly;
    $("log-count").textContent = filtered ? `${visible} of ${lines.length} lines` : `${lines.length} lines`;
    $("log-issues").textContent = `Issues ${lines.filter((item) => item.issue).length}`;
    $("log-empty").hidden = visible > 0 || !(lines.length || filtered);
  }

  function addLog(line, arriving = false) {
    const size = encoder.encode(line).length;
    const item = { line, size, lower: line.toLowerCase(), issue: ISSUE.test(line), node: document.createElement("span"), painted: null };
    if (item.issue) item.node.className = "issue";
    item.node.textContent = `${line}\n`;
    paintLine(item);
    lines.push(item); logBytes += size;
    if (arriving && lineShown(item)) pendingLogCount += 1;
    while (lines.length > 500 || logBytes > 512 * 1024) logBytes -= lines.shift().size;
    if (!logRenderPending) {
      logRenderPending = true;
      requestAnimationFrame(() => {
        logRenderPending = false;
        const output = $("log-output");
        const visible = panelView === "logs" && !$("panel").hidden;
        // Check before appending, including scroll input since the previous frame.
        if (visible && following && !atLogBottom()) following = false;
        const top = output.getBoundingClientRect().top + output.clientTop;
        const anchor = !following && visible ? Array.from(output.children).find((node) => node.getBoundingClientRect().bottom > top) : null;
        const offset = anchor ? anchor.getBoundingClientRect().top - top : 0;
        const retained = new Set(lines.map((entry) => entry.node));
        for (const node of Array.from(output.children)) if (!retained.has(node)) node.remove();
        const fragment = document.createDocumentFragment();
        for (const entry of lines) if (entry.node.parentNode !== output) fragment.append(entry.node);
        output.append(fragment);
        if (visible && following) output.scrollTop = output.scrollHeight;
        else if (visible && anchor) {
          if (anchor.parentNode === output) output.scrollTop += anchor.getBoundingClientRect().top - top - offset;
          else output.scrollTop = 0;
        }
        if (following && visible) {
          unreadLogs = 0;
        } else unreadLogs += pendingLogCount;
        pendingLogCount = 0;
        updateLogState();
      });
    }
  }

  function filterLogs() {
    lines.forEach(paintLine);
    if (following) $("log-output").scrollTop = $("log-output").scrollHeight;
    updateLogState();
  }

  /* ---------- Connection ---------- */
  function retryClosedSource() {
    if (demo || document.hidden || !source || source.readyState !== EventSource.CLOSED || retryTimer !== null) return;
    retryTimer = setTimeout(() => { retryTimer = null; connect(false); }, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 30000);
  }

  function connect(resetBackoff = true) {
    clearTimeout(retryTimer); retryTimer = null;
    if (resetBackoff) retryDelay = 1000;
    if (source) { source.close(); source = null; }
    if (demo || document.hidden) return;
    const logs = panelView === "logs";
    source = new EventSource(`/api/events${logs ? "?logs=1" : ""}`);
    const current = source;
    let gapAnnounced = false;
    current.addEventListener("status", (event) => {
      if (source !== current) return;
      try { render(JSON.parse(event.data)); } catch { disconnected(); }
    });
    current.addEventListener("log", (event) => {
      if (!logs || source !== current) return;
      try {
        const entry = JSON.parse(event.data);
        if (entry.sequence > lastSequence) {
          if (lastSequence && entry.sequence > lastSequence + 1 && !gapAnnounced) addLog("[viewer] Some older log lines expired.");
          gapAnnounced = false;
          lastSequence = entry.sequence; addLog(entry.line, true);
        }
      } catch { addLog("[viewer] Could not read a log message."); }
    });
    current.addEventListener("gap", () => {
      if (source !== current) return;
      gapAnnounced = true; addLog("[viewer] Some older log lines expired.");
    });
    current.addEventListener("open", () => {
      if (source === current) { retryDelay = 1000; updateLogState("Live"); }
    });
    current.addEventListener("error", () => { if (source === current) disconnected(); });
  }

  function disconnected() {
    historyGap = true; offline = true;
    updateLogState("Reconnecting…");
    retryClosedSource();
    pulse.live = false;
    apply();
  }

  /* ---------- Panel ---------- */
  let opener = null, closing = null;
  const views = { audio: "audio-view", details: "details-view", logs: "logs-view" };

  function movePill() {
    const seg = document.querySelector(".seg"), active = seg.querySelector('[aria-pressed="true"]');
    if (!active || $("panel").hidden) return;
    seg.style.setProperty("--px", `${active.offsetLeft}px`);
    seg.style.setProperty("--pw", `${active.offsetWidth}px`);
  }

  function showView(view, opening) {
    const panel = $("panel"), body = document.querySelector(".panel-body"), before = panel.offsetHeight;
    const wasLogs = panelView === "logs";
    panelView = view;
    panel.classList.toggle("logs-tab", view === "logs");
    $("stage").classList.toggle("logs-open", view === "logs");
    panel.querySelectorAll(".seg button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.panel === view)));
    Object.entries(views).forEach(([name, id]) => { $(id).hidden = name !== view; });
    movePill();
    body.scrollTop = 0;
    if (view === "logs") {
      if (following) $("log-output").scrollTop = $("log-output").scrollHeight;
      if (!wasLogs) updateLogState(demo ? "Demo" : "Connecting…");
      if (demo && !lines.length) {
        addLog("[bridge] Sample session started");
        addLog("[bridge] Sample audio format: 48000/S32_LE/2");
        addLog("[bridge] Sample recipient Play accepted");
      }
    }
    if (wasLogs !== (view === "logs")) connect();
    updateLogState();
    if (calm()) return;
    const delayBase = opening ? 110 : 40;
    $(views[view]).querySelectorAll("[data-s]").forEach((n, i) => play(n, [{ opacity: 0, translate: "0 12px" }, { opacity: 1, translate: "0 0" }], { duration: 620, delay: delayBase + Math.min(i, 9) * 34 }));
    $(views[view]).querySelectorAll(".path .node").forEach((n, i) => play(n, [{ scale: 0 }, { scale: 1 }], { duration: 620, delay: delayBase + 170 + i * 90, easing: POP }));
    $(views[view]).querySelectorAll(".path .wire").forEach((n, i) => play(n, [{ transform: "scaleY(0)" }, { transform: "scaleY(1)" }], { duration: 520, delay: delayBase + 240 + i * 90, easing: OUT }));
    if (view === "audio" && !$("bitrate-chart").hidden) {
      play($("bitrate-history"), [{ clipPath: "inset(0 100% 0 0)" }, { clipPath: "inset(0 0 0 0)" }], { duration: 1200, delay: delayBase + 260, easing: "cubic-bezier(.3,.7,.2,1)" });
      play($("trace-dot"), [{ scale: 0, opacity: 0 }, { scale: 1, opacity: 1 }], { duration: 420, delay: delayBase + 1250, easing: OUT });
    }
    if (!opening) {
      const after = panel.offsetHeight;
      if (Math.abs(after - before) > 1) panel.animate([{ height: `${before}px` }, { height: `${after}px` }], { duration: 560, easing: SPRING });
    }
  }

  function openPanel(view, trigger) {
    const panel = $("panel");
    if (closing) { closing.cancel(); closing = null; }
    if (panelView === null) {
      opener = trigger && !trigger.closest(".seg") ? trigger : $("more");
      panel.hidden = false;
      $("stage").classList.add("panel-open");
      // It grows out of whatever opened it.
      const from = opener.getBoundingClientRect(), box = panel.getBoundingClientRect();
      panel.style.transformOrigin = `${from.left + from.width / 2 - box.left}px ${from.top + from.height / 2 - box.top}px`;
      showView(view, true);
      if (!calm()) {
        panel.animate([{ opacity: 0, scale: 0.86, filter: "blur(10px)" }, { opacity: 1, scale: 1, filter: "blur(0)" }], { duration: 640, easing: SPRING });
        $("close-panel").querySelector("svg").animate([{ rotate: "-90deg", scale: 0.4, opacity: 0 }, { rotate: "0deg", scale: 1, opacity: 1 }], { duration: 620, delay: 90, easing: POP, fill: "backwards" });
      }
      panel.querySelector('.seg [aria-pressed="true"]')?.focus({ preventScroll: true });
      wake();
    } else if (view !== panelView) showView(view, false);
  }

  function closePanel() {
    const panel = $("panel");
    if (panel.hidden) return;
    const wasLogs = panelView === "logs";
    panelView = null; stopLogPulse();
    $("stage").classList.remove("panel-open", "logs-open");
    if (wasLogs) connect();
    const back = opener && !opener.hidden && opener.isConnected ? opener : $("more");
    wake();
    back.focus({ preventScroll: true });
    const done = () => { panel.hidden = true; };
    if (calm()) { done(); return; }
    rise($("room"), 90, 8);
    const out = panel.animate([{ opacity: 1, scale: 1, filter: "blur(0)" }, { opacity: 0, scale: 0.92, filter: "blur(8px)" }], { duration: 240, easing: ACCEL, fill: "forwards" });
    closing = out;
    out.onfinish = () => { if (closing === out) { closing = null; done(); out.cancel(); } };
  }

  document.querySelectorAll("[data-panel]").forEach((button) => button.addEventListener("click", () => openPanel(button.dataset.panel, button)));
  $("close-panel").addEventListener("click", closePanel);
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || $("panel").hidden) return;
    if (event.target === $("log-filter") && $("log-filter").value) {
      event.preventDefault(); event.stopPropagation(); $("log-filter").value = ""; logQuery = ""; filterLogs();
    } else closePanel();
  });
  window.addEventListener("resize", movePill);

  $("log-filter").addEventListener("input", (event) => { logQuery = event.target.value.trim().toLowerCase(); filterLogs(); });
  $("log-issues").addEventListener("click", (event) => {
    logIssuesOnly = !logIssuesOnly;
    event.currentTarget.setAttribute("aria-pressed", String(logIssuesOnly));
    filterLogs();
  });
  $("log-copy").addEventListener("click", async (event) => {
    const button = event.currentTarget, text = lines.filter(lineShown).map((item) => item.line).join("\n");
    // The clipboard API needs HTTPS, and this page is normally served over plain HTTP on a LAN.
    let copied = false;
    if (navigator.clipboard) {
      try { await navigator.clipboard.writeText(text); copied = true; } catch { copied = false; }
    }
    if (!copied) {
      const holder = document.createElement("textarea");
      holder.value = text; holder.setAttribute("readonly", ""); holder.setAttribute("aria-hidden", "true");
      holder.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;opacity:0";
      document.body.append(holder);
      holder.select();
      try { copied = document.execCommand("copy"); } catch { copied = false; }
      holder.remove();
      button.focus({ preventScroll: true });
    }
    button.textContent = copied ? "Copied" : "Copy failed";
    setTimeout(() => { if (button.isConnected) button.textContent = "Copy"; }, 1600);
  });
  $("log-output").addEventListener("scroll", () => {
    if (panelView !== "logs" || $("panel").hidden) return;
    following = atLogBottom();
    if (following) unreadLogs = 0;
    updateLogState();
  });
  $("new-logs").addEventListener("wheel", (event) => { $("log-output").scrollTop += event.deltaY; }, { passive: true });
  $("new-logs").addEventListener("click", () => {
    following = true; unreadLogs = 0;
    $("log-output").scrollTop = $("log-output").scrollHeight;
    updateLogState();
    $("log-output").focus({ preventScroll: true });
  });
  document.addEventListener("visibilitychange", () => {
    stopLogPulse(); connect();
    if (document.hidden) { cancelAnimationFrame(pulseFrame); pulseFrame = 0; resetPulse(); } else startPulse();
    updateLogState();
  });
  window.addEventListener("pagehide", () => { clearTimeout(retryTimer); retryTimer = null; if (source) source.close(); });
  window.addEventListener("pageshow", (event) => { if (event.persisted) connect(); });
  setInterval(() => { if (!demo && !document.hidden && lastStatus && Date.now() - lastStatus > 8000) disconnected(); }, 2000);

  if (demo) {
    $("demo-label").hidden = false;
    const scenario = ["playing", "idle", "waiting", "failure"].includes(demoParam) ? demoParam : "playing";
    const active = scenario !== "idle";
    const now = Date.now() / 1000;
    render({
      instance: "demo", uptime_seconds: 3720,
      audio: { state: scenario === "waiting" ? "waiting" : active ? "receiving" : "idle", session: active ? 12 : null,
        codec: active ? "AAC" : null, airplay_version: active ? 2 : null, stream_type: active ? "Buffered" : null,
        source_format: active ? "AAC/48000/F24/5.1" : null,
        output_stream: scenario === "waiting" || !active ? null : { codec: "FLAC", rate: 48000, bits: 32, channels: 2 },
        input_bitrate_bps: active ? 256000 : null,
        receiver_stats: active ? { missing_audio_blocks: 0, too_late_audio_blocks: 0, retry_requests: 0 } : null,
        pulse: scenario === "playing" ? { bpm: 120, confidence: .6, next_beat_in: .3, level: .7 } : null,
        age_seconds: active ? 84 : null, format: scenario === "waiting" || !active ? null : { rate: 48000, bits: 32, channels: 2 },
        buffered_bytes: active ? 2116800 : 0, pending_bytes: scenario === "waiting" ? 176400 : 0, raw_bytes: active ? 14817600 : 0, discarded_bytes: 0 },
      track: { title: active ? "Evening Drive" : "", artist: active ? "Example Artist" : "", album: active ? "After the Light" : "", artwork: active ? "/demo.svg" : null },
      connections: active && scenario !== "waiting" && scenario !== "failure" ? 1 : 0,
      command: active ? { name: "Play", accepted: scenario !== "failure", at: now } : null,
      recipient: { configured: true, name: "Living Room", playback: { value: scenario === "idle" ? "STOPPED" : "PLAYING", at: now - (scenario === "failure" ? 20 : 1), error: scenario === "failure" ? "connection" : null, stale: scenario === "failure" },
        volume: { value: 38, at: now - 1, error: scenario === "failure" ? "connection" : null, stale: false } }
    });
    if (scenario === "playing") {
      let tick = 0;
      setInterval(() => {
        tick++;
        render({...snapshot, audio: {...snapshot.audio,
          input_bitrate_bps: Math.round(256000 + Math.sin(tick * .8) * 14000)}});
      }, 2000);
    }
  } else connect();
})();
