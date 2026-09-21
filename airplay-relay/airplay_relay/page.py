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
    --line: #e5e7eb; --live: #16a34a; --idle: #9ca3af; --accent: #2563eb; --warn: #d97706;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #111114; --card: #1b1b20; --ink: #f3f4f6; --muted: #9ca3af;
      --line: #2b2b33; --live: #22c55e; --idle: #6b7280; --accent: #60a5fa; --warn: #f59e0b;
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
    <code>IPTV playlist &nbsp;<span id="playlistUrl">–</span></code>
    <code>Direct stream &nbsp;<span id="streamUrl">–</span></code>
    <div class="idle-note" id="byName" hidden></div>
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
        tracks.length > 1 && (named.length ? named.join(", ") : `${tracks.length} tracks`),
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
