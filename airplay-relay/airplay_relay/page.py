"""The page the add-on shows in Home Assistant's sidebar.

Served from the same port as the stream, so Home Assistant's ingress can proxy
it, and written as one string rather than a file tree because it is one page
with no build step and nothing to fetch from anywhere else.

Every figure on it is measured from the segments in RAM, so the page describes
what viewers are receiving rather than what the source claims.
"""

from __future__ import annotations

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Relay</title>
<style>
  :root {
    --bg: #f6f7f9; --card: #fff; --ink: #1b1b1f; --muted: #6b7280;
    --line: #e5e7eb; --live: #16a34a; --idle: #9ca3af; --accent: #2563eb; --warn: #a16207;
    --good: #15803d; --bad: #be123c;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #111114; --card: #1b1b20; --ink: #f3f4f6; --muted: #9ca3af;
      --line: #2b2b33; --live: #22c55e; --idle: #6b7280; --accent: #60a5fa; --warn: #fbbf24;
      --good: #4ade80; --bad: #fb7185;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px 16px; background: var(--bg); color: var(--ink);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  .wrap { max-width: 720px; margin: 0 auto; }
  header { display: flex; align-items: center; gap: 12px; margin-bottom: 20px; }
  header img { width: 44px; height: 44px; border-radius: 10px; }
  h1 { font-size: 20px; margin: 0; font-weight: 650; }
  .state { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 14px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--idle); }
  .dot.live { background: var(--live); box-shadow: 0 0 0 4px color-mix(in srgb, var(--live) 22%, transparent); }
  .dot.starting { background: var(--warn); }
  .problem { color: var(--warn); font-size: 13px; margin-top: 8px; word-break: break-word; }
  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 12px;
    padding: 18px; margin-bottom: 16px;
  }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 16px; }
  .label { font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
  .value { font-size: 20px; font-weight: 600; font-variant-numeric: tabular-nums; margin-top: 2px; }
  .value.small { font-size: 15px; font-weight: 500; word-break: break-all; }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .04em;
       color: var(--muted); margin: 0 0 12px; font-weight: 600; }
  code {
    display: block; background: var(--bg); border: 1px solid var(--line);
    border-radius: 8px; padding: 10px 12px; font-size: 13px; word-break: break-all;
    margin-bottom: 8px;
  }
  code span { color: var(--accent); }
  .idle-note { color: var(--muted); font-size: 13px; }
  button {
    font: inherit; padding: 9px 16px; border-radius: 8px; cursor: pointer;
    border: 1px solid var(--line); background: var(--bg); color: var(--ink);
  }
  button:hover:not(:disabled) { border-color: var(--warn); color: var(--warn); }
  button:disabled { opacity: .45; cursor: default; }

  .tabs { display: flex; gap: 8px; margin-bottom: 16px; }
  .tabs button { padding: 7px 14px; font-size: 14px; }
  .tabs button[aria-selected="true"] {
    border-color: var(--accent); color: var(--accent); font-weight: 600;
  }

  /* A verdict is a word first and a colour second: the dot repeats what the
     label already says, for anyone who cannot tell the colours apart. */
  .verdict { display: flex; align-items: center; gap: 10px; }
  .verdict .dot { width: 11px; height: 11px; flex: none; }
  .verdict .dot.good { background: var(--good); }
  .verdict .dot.fair { background: var(--warn); }
  .verdict .dot.poor { background: var(--bad); }
  .verdict .word { font-size: 20px; font-weight: 650; }
  .verdict .because { color: var(--muted); font-size: 13px; }

  .chart { margin-top: 18px; }
  .chart h3 {
    font-size: 12px; text-transform: uppercase; letter-spacing: .04em;
    color: var(--muted); margin: 0 0 6px; font-weight: 600;
    display: flex; justify-content: space-between; align-items: baseline;
  }
  .chart h3 .now {
    color: var(--ink); font-size: 15px; font-variant-numeric: tabular-nums;
    text-transform: none; letter-spacing: 0;
  }
  .chart svg { display: block; width: 100%; height: 88px; overflow: visible; }
  .chart .grid-line { stroke: var(--line); stroke-width: 1; }
  .chart .axis { fill: var(--muted); font-size: 10px; }
  .chart .series { fill: none; stroke: var(--accent); stroke-width: 2;
                   stroke-linejoin: round; stroke-linecap: round; }
  .chart .area { fill: var(--accent); opacity: .12; }
  .chart .head { fill: var(--accent); stroke: var(--card); stroke-width: 2; }
  .chart .crosshair { stroke: var(--muted); stroke-width: 1; stroke-dasharray: 3 3; }
  .chart .empty { fill: var(--muted); font-size: 12px; }

  code.row { display: flex; align-items: center; gap: 10px; }
  code.row span.url { flex: 1; min-width: 0; word-break: break-all; }
  .copy {
    flex: none; padding: 4px 8px; font-size: 12px; border-radius: 6px;
    background: var(--card); display: inline-flex; align-items: center; gap: 5px;
  }
  .copy svg { width: 13px; height: 13px; fill: none; stroke: currentColor; stroke-width: 1.8; }

  .past { border-top: 1px solid var(--line); padding: 14px 0; }
  .past:first-of-type { border-top: 0; padding-top: 0; }
  .past .when { font-weight: 600; }
  .past .line { color: var(--muted); font-size: 13px; margin-top: 3px; word-break: break-all; }
  .past .figures { display: flex; flex-wrap: wrap; gap: 6px 18px; margin-top: 8px; font-size: 13px; }
  .past .figures b { font-weight: 600; font-variant-numeric: tabular-nums; }
  .past details summary { cursor: pointer; color: var(--accent); font-size: 13px; margin-top: 8px; }
  .past .again { margin-top: 10px; font-size: 13px; padding: 6px 12px; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <img src="logo.png" alt="">
    <div>
      <h1 id="name">Relay</h1>
      <div class="state"><span class="dot" id="dot"></span><span id="stateText">connecting…</span></div>
    </div>
  </header>

  <div class="tabs" role="tablist">
    <button id="tabNow" role="tab" aria-selected="true">Now</button>
    <button id="tabPast" role="tab" aria-selected="false">Last month</button>
  </div>

  <div id="now">
  <div class="card">
    <div class="grid">
      <div><div class="label">Resolution</div><div class="value" id="resolution">–</div></div>
      <div><div class="label">Bitrate</div><div class="value" id="bitrate">–</div></div>
      <div><div class="label">Video</div><div class="value" id="video">–</div></div>
      <div><div class="label">Audio</div><div class="value" id="audio">–</div></div>
      <div><div class="label">Playing for</div><div class="value" id="uptime">–</div></div>
      <div><div class="label">Buffer</div><div class="value" id="buffer">–</div></div>
      <div><div class="label">Watching</div><div class="value" id="viewers">–</div></div>
      <div><div class="label">Last viewed</div><div class="value" id="viewed">–</div></div>
    </div>
  </div>

  <div class="card">
    <h2>How it is going</h2>
    <div class="verdict">
      <span class="dot" id="verdictDot"></span>
      <div>
        <div class="word" id="verdictWord">–</div>
        <div class="because" id="verdictWhy">waiting for the first figures</div>
      </div>
    </div>
    <div class="chart">
      <h3><span>Watching</span><span class="now" id="chartViewersNow">–</span></h3>
      <svg id="chartViewers" viewBox="0 0 600 88" preserveAspectRatio="none"
           role="img" aria-label="Players watching over this stream"></svg>
    </div>
    <div class="chart">
      <h3><span>Bitrate published</span><span class="now" id="chartRateNow">–</span></h3>
      <svg id="chartRate" viewBox="0 0 600 88" preserveAspectRatio="none"
           role="img" aria-label="Bitrate published over this stream"></svg>
    </div>
    <div class="idle-note" id="sessionRecap" hidden></div>
  </div>

  <div class="card">
    <h2>Started by</h2>
    <div class="value small" id="sender">Nothing playing</div>
    <div class="idle-note" id="senderSeen" hidden></div>
  </div>

  <div class="card">
    <h2>Source</h2>
    <div class="value small" id="source">Nothing playing</div>
    <div class="idle-note" id="rendition" hidden></div>
    <div class="problem" id="problem" hidden></div>
  </div>

  <div class="card">
    <h2>Control</h2>
    <button id="stop">Stop the channel</button>
    <div class="idle-note" style="margin-top:8px">
      The phone cannot stop it. Once a stream starts the channel keeps running
      until something replaces it, you stop it here, or nobody watches for
      fifteen minutes.
    </div>
  </div>

  <div class="card">
    <h2>Watch it</h2>
    <code class="row">IPTV playlist&nbsp;<span class="url" id="playlistUrl">–</span>
      <button class="copy" data-copy="playlistUrl">
        <svg viewBox="0 0 24 24"><rect x="9" y="9" width="12" height="12" rx="2"/>
          <path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>Copy</button></code>
    <code class="row">Direct stream&nbsp;<span class="url" id="streamUrl">–</span>
      <button class="copy" data-copy="streamUrl">
        <svg viewBox="0 0 24 24"><rect x="9" y="9" width="12" height="12" rx="2"/>
          <path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>Copy</button></code>
    <div class="idle-note" id="byName" hidden></div>
  </div>
  </div>

  <div id="past" hidden>
    <div class="card">
      <h2>Last month</h2>
      <div id="pastList" class="idle-note">Reading the record…</div>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);

function duration(seconds) {
  if (seconds === null || seconds === undefined) return "–";
  const s = Math.floor(seconds % 60), m = Math.floor(seconds / 60) % 60, h = Math.floor(seconds / 3600);
  const pad = n => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

function timeOfDay(seconds) {
  return new Date(seconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// One series, drawn thin over a recessive grid, with the latest value named in
// the heading rather than a number on every point. A crosshair reads the rest.
function drawChart(svg, points, format) {
  const W = 600, H = 88, PAD = 14;
  svg.innerHTML = "";
  const ns = "http://www.w3.org/2000/svg";
  const make = (kind, attrs, text) => {
    const node = document.createElementNS(ns, kind);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (text !== undefined) node.textContent = text;
    svg.appendChild(node);
    return node;
  };

  if (points.length < 2) {
    make("text", { x: 0, y: H / 2, class: "empty" },
         points.length ? "one reading so far" : "nothing measured yet");
    return;
  }

  const values = points.map(p => p.value);
  const top = Math.max(...values, 1) * 1.15;
  const first = points[0].at, last = points[points.length - 1].at;
  const span = Math.max(1, last - first);
  const x = at => PAD + ((at - first) / span) * (W - PAD * 2);
  const y = value => H - PAD - (value / top) * (H - PAD * 2);

  make("line", { x1: 0, y1: y(0), x2: W, y2: y(0), class: "grid-line" });
  make("line", { x1: 0, y1: y(top), x2: W, y2: y(top), class: "grid-line" });
  make("text", { x: 0, y: y(top) - 4, class: "axis" }, format(top));
  make("text", { x: 0, y: H - 2, class: "axis" }, timeOfDay(first));
  make("text", { x: W, y: H - 2, class: "axis", "text-anchor": "end" }, timeOfDay(last));

  const line = points.map((p, i) => `${i ? "L" : "M"}${x(p.at)},${y(p.value)}`).join("");
  make("path", { d: `${line}L${x(last)},${y(0)}L${x(first)},${y(0)}Z`, class: "area" });
  make("path", { d: line, class: "series" });
  const head = points[points.length - 1];
  make("circle", { cx: x(head.at), cy: y(head.value), r: 4.5, class: "head" });

  const crosshair = make("line", { x1: 0, y1: 0, x2: 0, y2: H, class: "crosshair", opacity: 0 });
  const readout = make("text", { x: 0, y: 12, class: "axis", opacity: 0 });
  svg.onmousemove = event => {
    const box = svg.getBoundingClientRect();
    const at = first + ((event.clientX - box.left) / box.width) * span;
    const near = points.reduce((a, b) => Math.abs(b.at - at) < Math.abs(a.at - at) ? b : a);
    crosshair.setAttribute("x1", x(near.at));
    crosshair.setAttribute("x2", x(near.at));
    crosshair.setAttribute("opacity", 1);
    readout.setAttribute("x", Math.min(x(near.at) + 6, W - 90));
    readout.setAttribute("opacity", 1);
    readout.textContent = `${timeOfDay(near.at)} · ${format(near.value)}`;
  };
  svg.onmouseleave = () => {
    crosshair.setAttribute("opacity", 0);
    readout.setAttribute("opacity", 0);
  };
}

const rate = value => `${Math.round(value)} kbps`;
const players = value => `${Math.round(value)}`;

async function refresh() {
  let data;
  try {
    data = await (await fetch("status.json", { cache: "no-store" })).json();
  } catch {
    $("stateText").textContent = "cannot reach the add-on";
    return;
  }

  $("name").textContent = data.name || "Relay";
  $("dot").className = "dot" + (data.playing ? " live" : data.starting ? " starting" : "");
  $("stateText").textContent = data.playing ? "Playing"
    : data.starting ? "Asked for a stream, nothing coming through yet"
    : "Nothing playing";

  const has = data.playing && data.width;
  $("resolution").textContent = has ? `${data.width}×${data.height}` : "–";
  $("bitrate").textContent = data.playing && data.bitrate_kbps
    ? `${data.bitrate_kbps} kbps` : "–";
  $("video").textContent = has ? [data.video_codec, data.frame_rate && `${data.frame_rate}fps`]
    .filter(Boolean).join(" · ") : "–";
  const tracks = (data.playing && data.audio_tracks) || [];
  // More than one track means the player's own audio menu has something to
  // offer, which is worth saying here: nothing on this page switches it.
  const named = tracks.filter(Boolean);
  $("audio").textContent = data.playing && data.audio_codec
    ? [
        data.audio_codec,
        data.audio_channels && `${data.audio_channels}ch`,
        // Named only when every track has a name: "5 tracks" is honest where
        // "zxx" would suggest the other four do not exist.
        tracks.length > 1 && (named.length === tracks.length
          ? named.join(", ") : `${tracks.length} tracks`),
      ].filter(Boolean).join(" · ") : "–";
  $("uptime").textContent = data.playing ? duration(data.position) : "–";
  $("buffer").textContent = data.playing
    ? `${data.window_seconds}s · ${data.segments} seg` : "–";

  // Only while there is a stream to have been started. Who last AirPlayed
  // something is remembered for as long as the add-on runs, and saying it over
  // an idle channel reads as if that stream were still on.
  const who = data.requested ? (data.sender || {}) : {};
  const parts = [];
  if (who.app) parts.push(who.app);
  if (who.device || who.model) parts.push(`from ${who.device || who.model}`);
  const aside = [who.device && who.model, who.os].filter(Boolean);
  if (aside.length) parts.push(`(${aside.join(", ")})`);
  const sender = $("sender");
  if (parts.length) {
    const gone = data.sender_seen > 20;
    sender.textContent = parts.join(" ");
    sender.classList.remove("idle-note");
    $("senderSeen").textContent = gone
      ? `phone last heard from ${duration(data.sender_seen)} ago — the channel carries on without it`
      : "phone connected";
    $("senderSeen").hidden = false;
  } else {
    sender.textContent = data.requested
      ? "The sender did not say who it is"
      : "Nothing playing — nobody has started a stream";
    sender.classList.add("idle-note");
    $("senderSeen").hidden = true;
  }

  const source = $("source");
  if (data.playing && data.source_url) {
    source.textContent = data.source_url;
    source.classList.remove("idle-note");
  } else {
    source.textContent = data.standby
      ? "Nothing playing — the standby card is on the channel, so a player can tune in and wait"
      : "Nothing playing — AirPlay something from your phone";
    source.classList.add("idle-note");
  }

  // Which of the source's renditions is being pulled, and whether the link
  // made us settle for it. Only worth a line when there was a choice.
  const rendition = $("rendition");
  if (data.playing && data.renditions > 1) {
    const chosen = `${data.rendition} at ${data.rendition_kbps} kbps`;
    rendition.textContent = data.rendition_rung === 1
      ? `${chosen} — the best of ${data.renditions} the source offers`
      : `${chosen} — ${data.rendition_rung} of ${data.renditions}, stepped down to keep up`;
    rendition.hidden = false;
  } else {
    rendition.hidden = true;
  }

  const problem = $("problem");
  if (data.starting && data.last_error) {
    problem.textContent = data.last_error;
    problem.hidden = false;
  } else {
    problem.hidden = true;
  }

  // Counted while the standby card is up too: tuning a television in early is
  // the point of the card, and a zero there would look like it had not worked.
  $("viewers").textContent = (data.playing || data.standby)
    ? (data.viewers === 1 ? "1 player" : `${data.viewers} players`)
    : "–";

  $("viewed").textContent = data.playing
    ? (data.seconds_since_viewer <= 5 ? "now" : `${duration(data.seconds_since_viewer)} ago`)
    : "–";
  $("stop").disabled = !(data.playing || data.starting);

  showQuality(data);

  $("playlistUrl").textContent = data.playlist_url || "–";
  $("streamUrl").textContent = data.stream_url || "–";

  // The name is friendlier to type and survives the address changing, but it
  // only reaches players that speak mDNS -- so it is offered, not substituted.
  const byName = $("byName");
  if (data.playlist_name_url) {
    byName.textContent = data.name_is_mdns
      ? `${data.playlist_name_url} works too, but only for players on this network `
        + "segment: a .local lookup that crosses a VLAN can take seconds, and a player "
        + "paying that on every connection buffers on a stream the address serves fine."
      : `${data.playlist_name_url} works too, and is the better thing to type: `
        + "it is the name your router already answers for, so it survives the address changing.";
    byName.hidden = false;
  } else {
    byName.hidden = true;
  }
}

// The verdict, the two series, and a recap of the stream so far.
function showQuality(data) {
  const verdict = data.quality || {};
  const known = ["good", "fair", "poor"].includes(verdict.state);
  $("verdictDot").className = "dot" + (known ? " " + verdict.state : "");
  $("verdictWord").textContent = {
    good: "Good", fair: "Fair", poor: "Struggling",
    starting: "Starting", idle: "Nothing playing",
  }[verdict.state] || "–";
  $("verdictWhy").textContent = verdict.because || "";

  const samples = data.samples || [];
  drawChart($("chartViewers"),
            samples.map(s => ({ at: s.at, value: s.viewers || 0 })), players);
  drawChart($("chartRate"),
            samples.filter(s => s.kbps).map(s => ({ at: s.at, value: s.kbps })), rate);
  $("chartViewersNow").textContent = data.playing || data.standby
    ? players(data.viewers) : "–";
  $("chartRateNow").textContent = data.playing && data.bitrate_kbps
    ? rate(data.bitrate_kbps) : "–";

  const recap = $("sessionRecap");
  if (data.playing && data.session_started) {
    const peak = Math.max(0, ...samples.map(s => s.viewers || 0), data.viewers || 0);
    const rates = samples.filter(s => s.kbps).map(s => s.kbps);
    const mean = rates.length ? Math.round(rates.reduce((a, b) => a + b, 0) / rates.length) : null;
    const stepped = samples.filter(s => s.rung > 1).length;
    recap.textContent = `Since ${timeOfDay(data.session_started)}: `
      + `${peak} player${peak === 1 ? "" : "s"} at the busiest`
      + (mean ? `, ${mean} kbps on average` : "")
      + (stepped ? `, ${stepped} reading${stepped === 1 ? "" : "s"} below the best rendition`
                 : ", never below the best rendition");
    recap.hidden = false;
  } else {
    recap.hidden = true;
  }
}

function duration_between(from, to) {
  return duration(Math.max(0, (to || Date.now() / 1000) - from));
}

// The same rule the live panel uses: name the tracks only when they all have
// names, and otherwise say how many there were.
function describeTracks(tracks, codec) {
  const list = tracks || [];
  const named = list.filter(Boolean);
  if (list.length > 1) return named.length === list.length ? named.join(", ") : `${list.length} tracks`;
  return named[0] || codec || null;
}

function renderPast(sessions) {
  const list = $("pastList");
  if (!sessions.length) {
    list.textContent = "Nothing has been played yet. Streams appear here for a month.";
    return;
  }
  list.className = "";
  list.innerHTML = "";
  for (const session of sessions) {
    const started = new Date(session.started * 1000);
    const who = session.sender || {};
    const from = [who.app, who.device || who.model].filter(Boolean).join(" from ");
    const rates = (session.samples || []).filter(s => s.kbps).map(s => s.kbps);
    const worst = (session.samples || []).filter(s => s.state === "poor").length;

    const row = document.createElement("div");
    row.className = "past";
    const figure = (label, value) => value === null || value === undefined || value === ""
      ? "" : `<span>${label} <b>${value}</b></span>`;
    row.innerHTML =
      `<div class="when">${started.toLocaleDateString([], { weekday: "short", day: "numeric",
         month: "short" })}, ${timeOfDay(session.started)}</div>`
      + `<div class="line">${session.source_host || session.source_url || "unknown source"}`
      + `${from ? " — started by " + from : ""}</div>`
      + `<div class="figures">`
      + figure("for", duration_between(session.started, session.ended))
      + figure("picture", session.width ? `${session.width}×${session.height}` : null)
      + figure("rendition", session.rendition
          ? `${session.rendition}${session.renditions ? " of " + session.renditions : ""}` : null)
      + figure("video", session.video_codec)
      + figure("audio", describeTracks(session.audio_tracks, session.audio_codec))
      + figure("busiest", session.peak_viewers !== undefined
          ? `${session.peak_viewers} watching` : null)
      + figure("average", session.mean_kbps ? `${session.mean_kbps} kbps` : null)
      + figure("peak", session.peak_kbps ? `${session.peak_kbps} kbps` : null)
      + figure("ended", session.reason)
      + figure("struggling", worst ? `${worst} readings` : null)
      + `</div>`;

    if (rates.length > 1) {
      const detail = document.createElement("details");
      detail.innerHTML = "<summary>How it went</summary>"
        + `<div class="chart"><h3><span>Bitrate published</span></h3>`
        + `<svg viewBox="0 0 600 88" preserveAspectRatio="none"></svg></div>`
        + `<div class="chart"><h3><span>Watching</span></h3>`
        + `<svg viewBox="0 0 600 88" preserveAspectRatio="none"></svg></div>`;
      row.appendChild(detail);
      const [rateSvg, viewerSvg] = detail.querySelectorAll("svg");
      detail.addEventListener("toggle", () => {
        if (!detail.open) return;
        drawChart(rateSvg, session.samples.filter(s => s.kbps)
                  .map(s => ({ at: s.at, value: s.kbps })), rate);
        drawChart(viewerSvg, session.samples
                  .map(s => ({ at: s.at, value: s.viewers || 0 })), players);
      });
    }

    if (session.source_url) {
      const again = document.createElement("button");
      again.className = "again";
      again.textContent = "Play this again";
      again.addEventListener("click", async () => {
        again.disabled = true;
        again.textContent = "Starting…";
        try {
          const answer = await (await fetch(`replay?id=${session.id}`, { cache: "no-store" })).json();
          again.textContent = answer.playing ? "Started" : "The source is no longer there";
        } catch {
          again.textContent = "Could not start it";
        }
        setTimeout(() => { showTab("now"); }, 700);
      });
      row.appendChild(again);
    }
    list.appendChild(row);
  }
}

async function loadPast() {
  try {
    renderPast(await (await fetch("history.json", { cache: "no-store" })).json());
  } catch {
    $("pastList").textContent = "Could not read the record.";
  }
}

function showTab(which) {
  const past = which === "past";
  if ((location.hash === "#past") !== past) location.hash = past ? "past" : "";
  $("now").hidden = past;
  $("past").hidden = !past;
  $("tabNow").setAttribute("aria-selected", String(!past));
  $("tabPast").setAttribute("aria-selected", String(past));
  if (past) loadPast();
}

$("tabNow").addEventListener("click", () => showTab("now"));
$("tabPast").addEventListener("click", () => showTab("past"));
// So the tab can be linked to, and survives a reload of the page it is on.
if (location.hash === "#past") showTab("past");
window.addEventListener("hashchange", () => showTab(location.hash === "#past" ? "past" : "now"));

// Clipboard access needs a secure context, which a page served over plain HTTP
// on the local network is not, so the old selection trick is the fallback
// rather than an afterthought.
async function copyText(text, button) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const holder = document.createElement("textarea");
    holder.value = text;
    holder.style.position = "fixed";
    holder.style.opacity = "0";
    document.body.appendChild(holder);
    holder.select();
    try { document.execCommand("copy"); } catch {}
    holder.remove();
  }
  const was = button.lastChild.textContent;
  button.lastChild.textContent = "Copied";
  setTimeout(() => { button.lastChild.textContent = was; }, 1200);
}

for (const button of document.querySelectorAll(".copy")) {
  button.addEventListener("click", () => {
    const text = $(button.dataset.copy).textContent.trim();
    if (text && text !== "–") copyText(text, button);
  });
}

$("stop").addEventListener("click", async () => {
  $("stop").disabled = true;
  try { await fetch("stop", { cache: "no-store" }); } catch {}
  setTimeout(refresh, 400);
});

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""
