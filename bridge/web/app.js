"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const demoParam = new URLSearchParams(location.search).get("demo");
  const demo = demoParam !== null;
  let source = null, snapshot = null, lastStatus = 0, panelView = null;
  let lastSequence = 0, instance = null, lines = [], logBytes = 0;
  let logRenderPending = false;
  let following = true, unreadLogs = 0, pendingLogCount = 0;
  let logConnection = demo ? "Demo" : "Connecting…";
  const encoder = new TextEncoder();
  const labels = { idle: "Ready for AirPlay", receiving: "Receiving AirPlay", waiting: "Waiting for audio format", error: "Audio session error" };
  const playbackLabels = { PLAYING: "Playing", PAUSED_PLAYBACK: "Paused", STOPPED: "Stopped", TRANSITIONING: "Starting", NO_MEDIA_PRESENT: "No media" };
  let backdropRequest = 0;
  function updateBackdrop(src) {
    const request = ++backdropRequest;
    const current = document.querySelector(".ambience img.visible");
    if (current.getAttribute("src") === src) return;
    const next = current === $("backdrop") ? $("backdrop-next") : $("backdrop");
    next.onload = () => requestAnimationFrame(() => {
      if (request !== backdropRequest) return;
      current.classList.remove("visible");
      next.classList.add("visible");
    });
    next.src = src;
  }

  const duration = (value) => value == null ? "—" : `${Math.floor(value / 60)}m ${Math.floor(value % 60)}s`;
  const bytes = (value) => value >= 1048576 ? `${(value / 1048576).toFixed(1)} MiB` : `${(value / 1024).toFixed(1)} KiB`;
  const format = (pcm) => pcm ? `${pcm.rate / 1000} kHz · ${pcm.bits}-bit · ${pcm.channels === 2 ? "Stereo" : "Mono"}` : "Format not received";
  const observed = (field) => field.at ? `${Math.max(0, Math.floor(Date.now() / 1000 - field.at))} seconds ago` : "Not yet observed";
  const fieldValue = (field, volume = false) => {
    if (field.error === "unavailable") return "Unavailable";
    if (field.error) return "Connection problem";
    if (field.value == null) return "Checking…";
    const text = volume ? `${field.value}%` : playbackLabels[field.value] || "Unknown";
    return field.stale ? `${text} (stale)` : text;
  };

  function details(data) {
    const rows = [
      ["Bridge", demo ? "Demo" : "Online"], ["Uptime", duration(data.uptime_seconds)],
      ["AirPlay input", labels[data.audio.state] || "Unknown"], ["Session", data.audio.session ?? "None"],
      ["Session age", duration(data.audio.age_seconds)], ["Audio format", format(data.audio.format)],
      ["AirPlay version", data.audio.airplay_version ? `AirPlay ${data.audio.airplay_version}` : "Unknown"],
      ["Input codec", data.audio.codec || "Unknown"], ["Speaker stream", "FLAC"],
      ["Buffered audio", bytes(data.audio.buffered_bytes)], ["Waiting for format", bytes(data.audio.pending_bytes)],
      ["Received audio", bytes(data.audio.raw_bytes)], ["Discarded audio", bytes(data.audio.discarded_bytes)],
      ["Audio connections", data.connections],
      ["Last command", data.command ? `${data.command.name} · ${data.command.accepted ? "Accepted" : "Failed"}` : "None"],
      ["Recipient playback", data.recipient.configured ? fieldValue(data.recipient.playback) : "Not configured"],
      ["Playback observation", observed(data.recipient.playback)],
      ["Recipient volume", data.recipient.configured ? fieldValue(data.recipient.volume, true) : "Not configured"],
      ["Volume observation", observed(data.recipient.volume)]
    ];
    $("details").replaceChildren(...rows.map(([name, value]) => {
      const row = document.createElement("div"); row.className = "detail-row";
      const term = document.createElement("dt"); term.textContent = name;
      const definition = document.createElement("dd"); definition.textContent = String(value);
      row.append(term, definition); return row;
    }));
  }

  function render(data) {
    snapshot = data;
    lastStatus = Date.now();
    $("speaker-name").textContent = data.recipient.name || "AirPlay Bridge";
    if (instance !== data.instance) {
      instance = data.instance;
      lastSequence = 0; lines = []; logBytes = 0;
      $("log-output").textContent = "";
      following = true; unreadLogs = 0; pendingLogCount = 0;
      updateLogState();
    }
    const active = data.audio.state !== "idle";
    $("title").textContent = data.track.title || (active ? "Track information unavailable" : "Ready for AirPlay");
    $("artist").textContent = data.track.artist || (active ? "AirPlay audio" : "Select the bridge from your AirPlay menu.");
    $("album").textContent = data.track.album || "";
    const art = data.track.artwork || "/placeholder.svg";
    // Only local artwork routes are accepted, even if a malformed status supplies a URL.
    const safeArt = /^\/art-\d+\.jpg$/.test(art) || ["/placeholder.svg", "/demo.svg"].includes(art) ? art : "/placeholder.svg";
    if ($("artwork").getAttribute("src") !== safeArt) {
      $("artwork").src = safeArt; updateBackdrop(safeArt);
    }
    $("artwork").alt = data.track.artwork ? `Cover art for ${data.track.album || data.track.title || "the current track"}` : "No cover art";
    const recipient = data.recipient;
    let recipientText = "Recipient not configured";
    if (recipient.configured) {
      recipientText = recipient.playback.stale ? "Recipient status stale" : `Recipient ${fieldValue(recipient.playback).toLowerCase()}`;
    }
    $("status-text").textContent = `${labels[data.audio.state] || "Unknown input state"} · ${recipientText}`;
    $("status").dataset.state = data.audio.state;
    $("format").textContent = data.audio.format ? format(data.audio.format) : "";
    $("codec").textContent = active ? `${data.audio.airplay_version ? `AirPlay ${data.audio.airplay_version}` : "AirPlay version unknown"} · ${data.audio.codec || "Codec unknown"} → FLAC` : "";
    $("volume").textContent = recipient.configured && recipient.volume.value != null && !recipient.volume.error && !recipient.volume.stale ? `Recipient volume ${recipient.volume.value}%` : "";
    let issue = "";
    if (data.audio.state === "error") issue = "Audio session failed. View details.";
    else if (data.audio.state === "waiting") issue = "Waiting for audio format. View details.";
    else if (recipient.configured && (recipient.playback.error === "connection" || recipient.volume.error === "connection")) issue = "Recipient did not respond. View details.";
    else if (recipient.configured && recipient.playback.stale) issue = "Recipient status is out of date. View details.";
    $("issue").hidden = !issue; $("issue").textContent = issue;
    details(data);
  }

  function atLogBottom() {
    const output = $("log-output");
    return output.scrollHeight - output.clientHeight - output.scrollTop <= 8;
  }

  function stopLogPulse() {
    $("log-state").classList.remove("log-pulse");
  }

  function updateLogState(state = logConnection) {
    logConnection = state;
    $("log-state").textContent = state;
    $("new-logs").hidden = following || !unreadLogs;
    $("new-logs").textContent = `${unreadLogs} new ↓`;
    $("new-logs").setAttribute("aria-label", `Show ${unreadLogs} new log entries`);
    $("log-state").classList.toggle("log-pulse", following && state === "Live" && panelView === "logs" && !document.hidden);
  }

  function addLog(line, arriving = false) {
    const size = encoder.encode(line).length;
    const node = document.createElement("span");
    node.textContent = `${line}\n`;
    lines.push({ line, size, node }); logBytes += size;
    if (arriving) pendingLogCount += 1;
    while (lines.length > 500 || logBytes > 512 * 1024) logBytes -= lines.shift().size;
    if (!logRenderPending) {
      logRenderPending = true;
      requestAnimationFrame(() => {
        logRenderPending = false;
        const output = $("log-output");
        const visible = panelView === "logs" && $("panel").open;
        // Check before appending, including scroll input since the previous frame.
        if (visible && following && !atLogBottom()) following = false;
        const top = output.getBoundingClientRect().top + output.clientTop;
        const anchor = !following && visible ? Array.from(output.children).find((node) => node.getBoundingClientRect().bottom > top) : null;
        const offset = anchor ? anchor.getBoundingClientRect().top - top : 0;
        const retained = new Set(lines.map((item) => item.node));
        for (const node of Array.from(output.children)) if (!retained.has(node)) node.remove();
        const fragment = document.createDocumentFragment();
        for (const item of lines) if (item.node.parentNode !== output) fragment.append(item.node);
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

  function connect() {
    if (source) { source.close(); source = null; }
    if (demo || document.hidden) return;
    const logs = panelView === "logs";
    source = new EventSource(`/api/events${logs ? "?logs=1" : ""}`);
    const current = source;
    current.addEventListener("status", (event) => {
      if (source !== current) return;
      try { render(JSON.parse(event.data)); } catch { disconnected(); }
    });
    current.addEventListener("log", (event) => {
      if (!logs || source !== current) return;
      try {
        const entry = JSON.parse(event.data);
        if (entry.sequence > lastSequence) { lastSequence = entry.sequence; addLog(entry.line, true); }
      } catch { addLog("[viewer] Could not read a log message."); }
    });
    current.addEventListener("gap", () => addLog("[viewer] Some older log lines expired."));
    current.addEventListener("open", () => { if (source === current) updateLogState("Live"); });
    current.addEventListener("error", () => { if (source === current) disconnected(); });
  }

  function disconnected() {
    $("status-text").textContent = "Bridge status unavailable · Reconnecting…";
    $("status").dataset.state = "error";
    $("volume").textContent = "";
    updateLogState("Reconnecting…");
    $("issue").hidden = false; $("issue").textContent = "Connection lost. Displayed track information can be out of date.";
  }

  function menu(open) {
    $("menu").hidden = !open;
    $("menu-button").setAttribute("aria-expanded", String(open));
    if (open) $("menu").querySelector("button").focus();
  }

  function openPanel(view) {
    panelView = view; menu(false);
    $("panel-title").textContent = view === "logs" ? "Live logs" : "Connection details";
    $("details-view").hidden = view !== "details"; $("logs-view").hidden = view !== "logs";
    $("panel").showModal();
    if (view === "logs") {
      if (following) $("log-output").scrollTop = $("log-output").scrollHeight;
      updateLogState(demo ? "Demo" : "Connecting…");
      if (demo && !lines.length) {
        addLog("[bridge] Sample session started");
        addLog("[bridge] Sample audio format: 44100/S16_LE/2");
        addLog("[bridge] Sample recipient Play accepted");
      }
      connect();
    }
  }

  $("menu-button").addEventListener("click", () => menu($("menu").hidden));
  document.querySelectorAll("[data-panel]").forEach((button) => button.addEventListener("click", () => openPanel(button.dataset.panel)));
  document.addEventListener("click", (event) => { if (!event.target.closest("#menu, #menu-button")) menu(false); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !$("menu").hidden) { menu(false); $("menu-button").focus(); } });
  $("close-panel").addEventListener("click", () => $("panel").close());
  $("panel").addEventListener("close", () => { const wasLogs = panelView === "logs"; panelView = null; stopLogPulse(); if (wasLogs) connect(); $("menu-button").focus(); });
  $("issue").addEventListener("click", () => openPanel("details"));
  $("log-output").addEventListener("scroll", () => {
    if (panelView !== "logs" || !$("panel").open) return;
    following = atLogBottom();
    if (following) unreadLogs = 0;
    updateLogState();
  });
  $("new-logs").addEventListener("click", () => {
    following = true; unreadLogs = 0;
    $("log-output").scrollTop = $("log-output").scrollHeight;
    updateLogState();
  });
  $("artwork").addEventListener("error", () => {
    if ($("artwork").getAttribute("src") !== "/placeholder.svg") {
      $("artwork").src = "/placeholder.svg"; updateBackdrop("/placeholder.svg");
    }
    $("artwork").alt = "No cover art";
  });
  document.addEventListener("visibilitychange", () => { stopLogPulse(); connect(); });
  window.addEventListener("pagehide", () => { if (source) source.close(); });
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
        age_seconds: active ? 84 : null, format: scenario === "waiting" || !active ? null : { rate: 44100, bits: 16, channels: 2 },
        buffered_bytes: active ? 2116800 : 0, pending_bytes: scenario === "waiting" ? 176400 : 0, raw_bytes: active ? 14817600 : 0, discarded_bytes: 0 },
      track: { title: active ? "Evening Drive" : "", artist: active ? "Example Artist" : "", album: active ? "After the Light" : "", artwork: active ? "/demo.svg" : null },
      connections: active && scenario !== "waiting" && scenario !== "failure" ? 1 : 0,
      command: active ? { name: "Play", accepted: scenario !== "failure", at: now } : null,
      recipient: { configured: true, playback: { value: scenario === "idle" ? "STOPPED" : "PLAYING", at: now - (scenario === "failure" ? 20 : 1), error: scenario === "failure" ? "connection" : null, stale: scenario === "failure" },
        volume: { value: 38, at: now - 1, error: scenario === "failure" ? "connection" : null, stale: false } }
    });
  } else connect();
})();
