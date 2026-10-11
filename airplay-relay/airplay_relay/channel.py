"""Relay one upstream stream to any number of local HLS viewers."""

from __future__ import annotations

import asyncio
import contextlib
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import mimetypes
from pathlib import Path
import threading
import time
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import Request, urlopen

from . import standby, status
from .history import History
from .hls import (
    LEAD_SECONDS,
    PACKET,
    SYNC,
    StrippingPuller,
    audio_renditions,
    payload_offset,
    pullable,
    variant_uris,
    variants,
)
from .logo import draw as draw_logo
from .pacing import BEHIND_RATIO, BEHIND_SECONDS, STEADY_RATIO, STEADY_SECONDS, Ladder, Pace
from .page import PAGE
from .republish import Republisher

_LOGGER = logging.getLogger(__name__)

PLAYLIST = "channel.m3u8"
CHANNEL_LIST = "channels.m3u"
LOGO = "logo.png"

RETRY_DELAYS = (1, 2, 4, 8, 15, 30)
WATCHING_SECONDS = 30
VERDICT_SECONDS = 20.0
SAMPLE_SECONDS = 5
SETTLED_SECONDS = 60
# Extra delay granted to a struggling stream: a step at a time, up to a ceiling.
CUSHION_STEP = 15.0
CUSHION_MAX = 45.0
IDLE_MINUTES = 15
WAKE_MINUTES = 60
# 2026-01-01 UTC, so clock-based segment numbers stay small.
NUMBERING_EPOCH = 1_767_225_600

# Explicit: system mimetypes vary, and IPTV apps file audio/x-mpegurl lists as radio.
mimetypes.add_type("application/vnd.apple.mpegurl", ".m3u8")
mimetypes.add_type("video/mp2t", ".ts")
mimetypes.add_type("application/x-mpegurl", ".m3u")


def _track_names(languages: list[tuple[str, str]]) -> tuple[str, ...]:
    """Return the ffmpeg arguments that label each audio track."""
    arguments: list[str] = []
    for position, (language, name) in enumerate(languages):
        if language:
            arguments += [f"-metadata:s:a:{position}", f"language={language}"]
        if name:
            arguments += [f"-metadata:s:a:{position}", f"title={name}"]
    return tuple(arguments)


class _Handler(SimpleHTTPRequestHandler):
    """Serves the segment directory, plus a page describing what it is doing."""

    channel: Channel

    # HTTP/1.1 keeps one connection per player instead of one per segment request.
    protocol_version = "HTTP/1.1"

    # Kept-alive connections expire, so an abandoned player does not hold its thread.
    timeout = 30

    def log_message(self, format: str, *args) -> None:
        """Keep segment and status-page requests out of the add-on log."""

    def do_GET(self) -> None:
        """Answer the status endpoints, and otherwise serve the directory."""
        route = self.path.split("?")[0].rstrip("/") or "/"
        if route in ("/", "/index.html"):
            self._send(PAGE.encode(), "text/html; charset=utf-8")
        elif route == f"/{CHANNEL_LIST}":
            host = self.headers.get("Host", "")
            self._send(self.channel.channel_list(host).encode(), "application/x-mpegurl")
        elif route == "/status.json":
            self._send(json.dumps(self.channel.snapshot()).encode(), "application/json")
        elif route.endswith(".ts") or route.endswith(".m3u8"):
            if not self.channel.requested:
                self.channel.wake()
            # Leftovers are from an ended stream; serving them would pass it off as live.
            if not self.channel.requested and not self.channel.standing_by:
                self.channel.note_leftovers(route)
                self.send_error(404, "nothing is playing")
                return
            self.channel.note_viewer(self.client_address[0] if self.client_address else "")
            super().do_GET()
        elif route == "/history.json":
            sessions = self.channel.history.recent() if self.channel.history else []
            self._send(json.dumps(sessions).encode(), "application/json")
        elif route == "/replay":
            # By record id, not URL, so nothing on the network can name a source to fetch.
            wanted = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            self._send(
                json.dumps({"playing": self.channel.replay(wanted)}).encode(),
                "application/json",
            )
        elif route == "/stop":
            self.channel.request_stop()
            self._send(b'{"stopping": true}', "application/json")
        else:
            super().do_GET()

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self) -> None:
        """Add the headers a player needs to follow a live window."""
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()


class Channel:
    """The single stream this add-on is republishing, if any."""

    def __init__(
        self,
        directory: str,
        port: int,
        hls_time: int,
        hls_list_size: int,
        user_agent: str = "",
        name: str = "Relay",
        hostname: str = "",
        history: History | None = None,
        address: str = "",
    ) -> None:
        """Prepare the segment directory and the server that will offer it."""
        self.directory = Path(directory)
        self.user_agent = user_agent
        self.name = name
        self.hostname = hostname
        self.dns_name = status.dns_name(address) if address else ""
        self.history = history
        self._session: dict | None = None
        self.address = address
        self.port = port
        self.hls_time = hls_time
        self.hls_list_size = hls_list_size
        self.source_url: str | None = None
        self._started_at: float | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._details: dict = {}
        self._describer: asyncio.Task[None] | None = None
        self._paused_url: str | None = None
        self._last_error: str | None = None
        self._is_hls = True
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_viewer: float = 0.0
        self._viewers: dict[str, float] = {}
        self._next_segment = 0
        self._warned_leftovers = False
        self.sender: dict[str, str] = {}
        self._sender_seen: float = 0.0
        self._needs_stripping = False
        # Playlists, one per rendition, that the relay pulls itself rather than ffmpeg.
        self._pull_urls: list[str] = []
        # Whether ffmpeg has been replaced by the relay's own parallel fetching.
        self._pulling = False
        self._variant: int | None = None
        self._languages: list[tuple[str, str]] = []
        self._ladder: Ladder | None = None
        self._pace = Pace()
        self._adapter: asyncio.Task[None] | None = None
        self._watcher: asyncio.Task[None] | None = None
        self._stopping: asyncio.Task[None] | None = None
        # The stream stopped for want of viewers, and when, so a player can wake it.
        self._dozing: tuple[str, float] | None = None
        self._dozing_lock = threading.Lock()
        self._switching = False
        self._republisher: Republisher | None = None
        self._puller: StrippingPuller | None = None
        self._cushion = 0.0
        self._cushion_changed = 0.0
        self._card: list[bytes] = []
        self._idler: asyncio.Task[None] | None = None

    @property
    def requested(self) -> bool:
        """Whether a sender has asked for something, whatever came of it."""
        return self.source_url is not None

    @property
    def playing(self) -> bool:
        """Whether segments are actually being produced."""
        return self.requested and (self.directory / PLAYLIST).exists()

    @property
    def position(self) -> float:
        """Seconds since this stream started."""
        if self._started_at is None:
            return 0.0
        return max(0.0, time.monotonic() - self._started_at)

    def _by_name(self, path: str) -> str | None:
        """Return a URL using whichever name is likeliest to resolve quickly."""
        if self.dns_name:
            return f"http://{self.dns_name}:{self.port}/{path}"
        if self.hostname:
            return f"http://{self.hostname}.local:{self.port}/{path}"
        return None

    def channel_list(self, host: str = "") -> str:
        """Return the channel list, pointing back at however it was asked for."""
        where = host or f"{self.address or '127.0.0.1'}:{self.port}"
        return (
            "#EXTM3U\n"
            f'#EXTINF:-1 tvg-id="relay" tvg-name="{self.name}"'
            f' tvg-logo="http://{where}/{LOGO}"'
            f' group-title="Home",{self.name}\n'
            f"http://{where}/{PLAYLIST}\n"
        )

    def serve(self) -> None:
        """Start serving the segment directory."""
        with contextlib.suppress(RuntimeError):
            self._loop = asyncio.get_running_loop()
        self.directory.mkdir(parents=True, exist_ok=True)
        draw_logo(self.directory / LOGO)
        # A subclass, not a partial keyword: the handler reads `channel` from its class.
        bound = type("BoundHandler", (_Handler,), {"channel": self})
        handler = partial(bound, directory=str(self.directory))
        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        if self._loop is not None:
            self._idler = self._loop.create_task(self._stand_by())
        _LOGGER.info(
            "stream on :%d/%s -- give an IPTV player :%d/%s",
            self.port,
            PLAYLIST,
            self.port,
            CHANNEL_LIST,
        )

    async def play(self, url: str) -> None:
        """Replace whatever is playing with the stream at this URL."""
        covered = self.standing_by
        with self._dozing_lock:
            self._dozing = None
        self._finish("replaced")
        self._paused_url = None
        self.source_url = None
        await self._halt()
        if not covered:
            self._clear()
        self.source_url = url
        self._last_error = None
        self._started_at = time.monotonic()
        self._details = {}
        if self.history is not None:
            self._session = self.history.start(
                source_url=url,
                source_host=status.source_host(url),
                sender=dict(self.sender),
                name=self.name,
            )
        self._supervisor = asyncio.create_task(self._supervise(url))
        self._describer = asyncio.create_task(self._describe())
        self._adapter = asyncio.create_task(self._adapt())
        self._watcher = asyncio.create_task(self._watch())

    async def pause(self) -> None:
        """Stop pulling but remember what was playing, so it can resume."""
        if not self.playing:
            return
        url, self.source_url = self.source_url, None
        self._paused_url = url
        await self._halt()
        _LOGGER.info("paused")

    async def resume(self) -> None:
        """Start again on whatever was paused."""
        if self.playing or not self._paused_url:
            return
        url, self._paused_url = self._paused_url, None
        _LOGGER.info("resuming")
        await self.play(url)

    def note_sender(self, details: dict[str, str]) -> None:
        """Record who is sending, and that they were heard from just now."""
        self.sender = details
        self._sender_seen = time.monotonic()

    @property
    def seconds_since_sender(self) -> float:
        """How long since the sender last said anything."""
        if not self._sender_seen:
            return 0.0
        return time.monotonic() - self._sender_seen

    def quality(self) -> dict[str, object]:
        """Say in a word how the stream is going, and why."""
        if not self.playing:
            return {"state": "idle", "because": "nothing is playing"}
        keeping = self._pace.ratio(time.monotonic(), VERDICT_SECONDS)
        stepped = self._ladder.rung if self._ladder else 0
        if keeping is None:
            return {"state": "starting", "because": "not enough of the stream has arrived to judge"}
        if keeping < BEHIND_RATIO:
            return {
                "state": "poor",
                "because": f"the window is gaining {keeping:.0%} of real time; the link is behind",
                "keeping_up": round(keeping, 3),
            }
        if stepped:
            return {
                "state": "fair",
                "because": f"steady, but {stepped} rendition(s) below the best the source offers",
                "keeping_up": round(keeping, 3),
            }
        return {
            "state": "good",
            "because": "keeping up with the source at its best rendition",
            "keeping_up": round(keeping, 3),
        }

    @property
    def standing_by(self) -> bool:
        """Whether the channel is carrying the standby card."""
        return bool(self._card) and not self.requested

    def _finish(self, reason: str) -> None:
        """Close the record of the stream that has just ended."""
        if self.history is not None:
            self.history.finish(self._session, reason)
        self._session = None

    def note_leftovers(self, route: str) -> None:
        """Say once that a player asked for a window the channel has ended."""
        if self._warned_leftovers:
            return
        self._warned_leftovers = True
        segments, seconds, _ = status.window(self.directory)
        if segments:
            _LOGGER.warning(
                "%s asked for with nothing playing, and %d segments (%.0fs) are still "
                "in the directory -- the stream that wrote them ended without clearing up",
                route,
                segments,
                seconds,
            )

    def note_viewer(self, peer: str = "") -> None:
        """Record that someone fetched part of the stream just now."""
        self._last_viewer = time.monotonic()
        if peer:
            self._viewers[peer] = self._last_viewer

    @property
    def viewers(self) -> int:
        """How many players have asked for anything lately."""
        cutoff = time.monotonic() - WATCHING_SECONDS
        for peer, seen in list(self._viewers.items()):
            if seen < cutoff:
                self._viewers.pop(peer, None)
        return len(self._viewers)

    @property
    def seconds_unwatched(self) -> float:
        """How long the stream now playing has had nobody watching it."""
        if self._started_at is None or self.viewers:
            return 0.0
        # The count reaches zero when the last player's watching window runs out.
        left = self._last_viewer + WATCHING_SECONDS if self._last_viewer else 0.0
        return max(0.0, time.monotonic() - max(left, self._started_at))

    @property
    def stops_in(self) -> float | None:
        """Seconds until an unwatched stream is stopped, or None while someone watches."""
        if not self.requested or self.viewers:
            return None
        return max(0.0, IDLE_MINUTES * 60 - self.seconds_unwatched)

    @property
    def seconds_since_viewer(self) -> float:
        """How long since anyone last fetched anything."""
        if not self._last_viewer:
            return 0.0
        return time.monotonic() - self._last_viewer

    def replay(self, identifier: str) -> bool:
        """Start a stream from the record again, if its source is still named."""
        if self.history is None:
            return False
        session = self.history.find(identifier)
        url = session.get("source_url") if session else None
        if not url:
            return False
        _LOGGER.info("playing again from the record: %s", url)
        loop = self._loop
        if loop is None:
            return False
        loop.call_soon_threadsafe(lambda: asyncio.create_task(self.play(url)))
        return True

    def request_stop(self) -> None:
        """Ask for the stream to end, from a thread that is not the loop's."""
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self.stop()))

    async def stop(self, reason: str = "stopped") -> None:
        """Stop pulling and stop retrying, recording why."""
        with self._dozing_lock:
            self._dozing = None
        if self.history is not None:
            self.history.finish(self._session, reason)
        self._session = None
        self._paused_url = None
        self.source_url = None
        await self._halt()
        self._clear()

    async def _halt(self) -> None:
        """Stop the running stream without deciding what that means."""
        supervisor, self._supervisor = self._supervisor, None
        describer, self._describer = self._describer, None
        adapter, self._adapter = self._adapter, None
        watcher, self._watcher = self._watcher, None
        for task in (describer, adapter, watcher):
            if task is not None:
                task.cancel()
        self._switching = False
        self._pace = Pace()
        self._cushion = 0.0
        self._pulling = False
        self._started_at = None
        self._details = {}
        if supervisor is not None:
            supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await supervisor
        await self._kill()

    @staticmethod
    def _looks_like_hls(url: str) -> bool:
        """Guess from the URL, for when the source cannot be fetched first."""
        return ".m3u8" in url.split("?")[0].lower()

    def _inspect(self, url: str) -> None:
        """Log what the source actually serves, and pick a rendition from a master playlist."""
        try:
            headers = {"User-Agent": self.user_agent} if self.user_agent else {}
            with urlopen(Request(url, headers=headers), timeout=10) as response:
                raw = response.read(65536)
                self._is_hls = raw.lstrip()[:7] == b"#EXTM3U"
                body = raw.decode("utf-8", "replace")
                _LOGGER.info(
                    "playlist: HTTP %s, %s, %d bytes",
                    response.status,
                    response.headers.get("content-type", "no type"),
                    len(body),
                )
                for line in body.splitlines()[:10]:
                    _LOGGER.info("  | %s", line[:160])
                self._variant = None
                self._languages = []
                self._ladder = None
                if offered := variants(body):
                    self._ladder = Ladder(offered)
                    self._variant = self._ladder.variant
                    _LOGGER.info(
                        "master playlist offers %s; taking %s",
                        ", ".join(f"{name} at {rate // 1000}kbps" for rate, name, _ in offered),
                        offered[self._variant][1],
                    )
                    if group := offered[self._variant][2]:
                        self._languages = audio_renditions(body, group)
                    if self._languages:
                        _LOGGER.info(
                            "audio: %s",
                            ", ".join(
                                name or language or "unnamed" for language, name in self._languages
                            ),
                        )
                    self._needs_stripping = False
                    # Separate audio playlists need ffmpeg to mux them in.
                    if not any(group for _, _, group in offered):
                        self._consider_pulling(variant_uris(body, url), self._variant)
                    return
            segment = next(
                (
                    line.strip()
                    for line in body.splitlines()
                    if line.strip() and not line.startswith("#")
                ),
                None,
            )
            if not self._is_hls:
                _LOGGER.info("not a playlist: a media file, which is played as it is")
                return
            if segment is None:
                _LOGGER.warning("playlist lists no segments; it may be a master playlist")
                return
            with urlopen(Request(urljoin(url, segment), headers=headers), timeout=15) as response:
                sample = response.read(400_000)
                head = sample[:16]
                _LOGGER.info(
                    "first segment: HTTP %s, %s, starts %s",
                    response.status,
                    response.headers.get("content-type", "no type"),
                    head.hex(),
                )
                if head[:1] == b"\x47":
                    _LOGGER.info("the segment is MPEG-TS, which is what we want")
                    if pullable(body):
                        self._pull_urls = [url]
                        _LOGGER.info("plain MPEG-TS; parallel pulling is held in reserve")
                    return
                if sample.lstrip()[:7] == b"#EXTM3U":
                    _LOGGER.info("the playlist points at other playlists; ffmpeg follows them")
                    return

                offset = payload_offset(sample)
                if offset:
                    self._needs_stripping = True
                    _LOGGER.warning(
                        "the segment hides MPEG-TS behind %d bytes of %s; it will be "
                        "fetched and stripped here, because ffmpeg's HLS reader stops "
                        "at the prefix",
                        offset,
                        head[8:12].decode(errors="replace").strip() or "prefix",
                    )
                else:
                    _LOGGER.error(
                        "the segment is an image, not video -- the CDN is not serving us the stream"
                    )
        except Exception as error:
            _LOGGER.warning("could not inspect the source: %s", error)

    def _consider_pulling(self, playlists: list[str], variant: int) -> None:
        """Pull a master playlist's renditions here if its segments are plain MPEG-TS."""
        if not playlists or not all(playlists):
            return
        try:
            headers = {"User-Agent": self.user_agent} if self.user_agent else {}
            with urlopen(Request(playlists[variant], headers=headers), timeout=10) as response:
                media = response.read(65536).decode("utf-8", "replace")
            segment = next(
                (
                    line.strip()
                    for line in media.splitlines()
                    if line.strip() and not line.startswith("#")
                ),
                None,
            )
            if segment is None or not pullable(media):
                return
            request = Request(urljoin(playlists[variant], segment), headers=headers)
            with urlopen(request, timeout=15) as response:
                head = response.read(PACKET + 1)
        except Exception as error:
            _LOGGER.info("not pulling the segments here: %s", error)
            return
        if len(head) > PACKET and head[0] == SYNC and head[PACKET] == SYNC:
            self._pull_urls = playlists
            _LOGGER.info(
                "the segments are plain MPEG-TS; pulling them here over parallel connections"
            )

    def snapshot(self) -> dict:
        """Return everything worth showing about the channel right now."""
        segments, seconds, size = status.window(self.directory)
        playing = self.playing
        bitrate = round(size * 8 / seconds / 1000) if seconds > 0 else None
        return {
            "name": self.name,
            "playing": playing,
            "requested": self.requested,
            "starting": self.requested and not playing,
            "paused": self._paused_url is not None,
            "last_error": None if playing else self._last_error,
            "seconds_since_viewer": round(self.seconds_since_viewer),
            "viewers": self.viewers,
            "stops_in": None if (left := self.stops_in) is None else round(left),
            "wakes_for": None if (wakes := self.wakes_for) is None else round(wakes),
            "sender": self.sender,
            "sender_seen": round(self.seconds_since_sender),
            "position": round(self.position, 1),
            "source_host": status.source_host(self.source_url),
            "source_url": self.source_url,
            "segments": segments,
            "window_seconds": round(seconds, 1),
            "window_bytes": size,
            "bitrate_kbps": bitrate,
            "standby": self.standing_by,
            "quality": self.quality(),
            "session_id": self._session["id"] if self._session else None,
            "session_started": self._session["started"] if self._session else None,
            "samples": self._session["samples"][-240:] if self._session else [],
            "rendition": self._ladder.name if self._ladder and self.requested else None,
            "rendition_kbps": (
                self._ladder.bandwidth // 1000 if self._ladder and self.requested else None
            ),
            "rendition_rung": self._ladder.rung + 1 if self._ladder and self.requested else None,
            "renditions": len(self._ladder.offered) if self._ladder and self.requested else None,
            "stream_url": f"http://{self.address}:{self.port}/{PLAYLIST}",
            "playlist_url": f"http://{self.address}:{self.port}/{CHANNEL_LIST}",
            "stream_name_url": self._by_name(PLAYLIST),
            "playlist_name_url": self._by_name(CHANNEL_LIST),
            "name_is_mdns": not self.dns_name,
            **self._details,
        }

    async def _describe(self) -> None:
        """Read codecs and resolution once a few segments exist."""
        for _ in range(20):
            await asyncio.sleep(3)
            # `requested`, not `playing`: a slow first segment must not end the description.
            if not self.requested:
                return
            if not self.playing:
                continue
            if details := await status.probe(self.directory):
                self._details = details
                _LOGGER.info(
                    "stream is %sx%s %s / %s",
                    details.get("width"),
                    details.get("height"),
                    details.get("video_codec"),
                    details.get("audio_codec"),
                )
                return

    async def _stand_by(self) -> None:
        """Publish the standby card whenever nothing else is on the channel."""
        self._card = await standby.render(self.directory)
        if not self._card:
            return
        frame = 0
        republisher: Republisher | None = None
        while True:
            if self.requested:
                republisher = None
                await asyncio.sleep(0.5)
                continue
            if republisher is None:
                republisher = Republisher(
                    self.directory, PLAYLIST, self.hls_list_size, start=self._numbering_starts_at()
                )
            republisher.add(self._card[frame % len(self._card)], standby.SECONDS)
            frame += 1
            await asyncio.sleep(standby.SECONDS)

    async def _watch(self) -> None:
        """Stop the stream once nobody has watched it for IDLE_MINUTES."""
        while True:
            await asyncio.sleep(SAMPLE_SECONDS)
            if self.seconds_unwatched >= IDLE_MINUTES * 60:
                break
        _LOGGER.info(
            "nobody has watched for %d minutes; stopping until a player tunes in again",
            IDLE_MINUTES,
        )
        # A task of its own, because stop() cancels this one.
        self._stopping = asyncio.create_task(self._doze())

    async def _doze(self) -> None:
        """Stop the unwatched stream, remembering it so a player can wake it."""
        url = self.source_url
        await self.stop("nobody watching")
        if url:
            with self._dozing_lock:
                self._dozing = (url, time.monotonic())

    @property
    def wakes_for(self) -> float | None:
        """Seconds left in which a player tuning in restarts the stream, or None."""
        dozing = self._dozing
        if dozing is None or self.requested:
            return None
        left = WAKE_MINUTES * 60 - (time.monotonic() - dozing[1])
        return left if left > 0 else None

    def wake(self) -> None:
        """Restart the stream that stopped for want of viewers, if it is still recent."""
        with self._dozing_lock:
            dozing, self._dozing = self._dozing, None
        if dozing is None or self._loop is None:
            return
        url, since = dozing
        if time.monotonic() - since > WAKE_MINUTES * 60:
            return
        _LOGGER.info("a player tuned in; starting the stream again: %s", url)
        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self.play(url)))

    async def _adapt(self) -> None:
        """Move down the source's renditions when the link cannot keep up."""
        while True:
            await asyncio.sleep(SAMPLE_SECONDS)
            if self._switching:
                continue
            if not self.playing:
                self._pace.reset()
                continue

            # time.monotonic, not the loop clock: the page reads the same pace from a thread.
            clock = time.monotonic()
            self._pace.note(clock, status.published(self.directory))
            self._remember()

            # Sampled before the ladder check, so single-rendition streams are measured too.
            ladder = self._ladder
            if ladder is None:
                continue

            behind = self._pace.ratio(clock, BEHIND_SECONDS)
            if behind is not None and behind < BEHIND_RATIO:
                if await self._pull_in_parallel(behind):
                    continue
                if self._widen(clock):
                    continue
            if behind is not None and behind < BEHIND_RATIO and ladder.down(clock):
                await self._change_rendition(
                    f"published {behind:.0%} of real time over the last "
                    f"{BEHIND_SECONDS:.0f}s, which the link cannot sustain"
                )
                continue

            settled = self._pace.ratio(clock, STEADY_SECONDS)
            if settled is not None and settled >= STEADY_RATIO and self._narrow(clock):
                continue
            if settled is not None and settled >= STEADY_RATIO and ladder.up(clock):
                await self._change_rendition(
                    f"kept up for {STEADY_SECONDS / 60:.0f} minutes, so there is room for more"
                )

    async def _pull_in_parallel(self, behind: float) -> bool:
        """Swap ffmpeg's single connection for parallel, split fetching. False if unavailable."""
        if self._pulling or not self._pull_urls:
            return False
        _LOGGER.warning(
            "published %.0f%% of real time over the last %.0fs; fetching the segments over "
            "parallel connections instead of ffmpeg's one",
            behind * 100,
            BEHIND_SECONDS,
        )
        self._pulling = True
        self._pace.reset()
        self._switching = True
        await self._kill()
        return True

    def _apply_cushion(self) -> None:
        """Give the republishing pair the delay currently granted."""
        if self._republisher is not None:
            self._republisher.widen(self._cushion)
        if self._puller is not None:
            self._puller.lead = LEAD_SECONDS + self._cushion

    def _widen(self, clock: float) -> bool:
        """Accept more delay before giving up quality. False if that is not possible."""
        if self._republisher is None or self._cushion >= CUSHION_MAX:
            return False
        self._cushion = min(CUSHION_MAX, self._cushion + CUSHION_STEP)
        self._cushion_changed = clock
        self._apply_cushion()
        # Judged afresh, so the extra delay gets time to show whether it was enough.
        self._pace.reset()
        _LOGGER.warning(
            "the link is behind; players now start %.0fs further back and the feed may "
            "run %.0fs ahead",
            self._cushion,
            self._cushion,
        )
        return True

    def _narrow(self, clock: float) -> bool:
        """Give back a step of delay once the stream has held steady for a while."""
        if self._cushion <= 0 or clock - self._cushion_changed < STEADY_SECONDS:
            return False
        self._cushion = max(0.0, self._cushion - CUSHION_STEP)
        self._cushion_changed = clock
        self._apply_cushion()
        _LOGGER.info("steady again; players start %.0fs further back than usual", self._cushion)
        return True

    def _remember(self) -> None:
        """Add the stream's current shape to its own record."""
        if self.history is None or self._session is None:
            return
        _, seconds, size = status.window(self.directory)
        verdict = self.quality()
        self.history.sample(
            self._session,
            {
                "viewers": self.viewers,
                "kbps": round(size * 8 / seconds / 1000) if seconds > 0 else None,
                "rung": (self._ladder.rung + 1) if self._ladder else None,
                "state": verdict["state"],
            },
        )
        self.history.describe(
            self._session,
            width=self._details.get("width"),
            height=self._details.get("height"),
            video_codec=self._details.get("video_codec"),
            audio_codec=self._details.get("audio_codec"),
            audio_tracks=self._details.get("audio_tracks"),
            rendition=self._ladder.name if self._ladder else None,
            renditions=len(self._ladder.offered) if self._ladder else None,
            sender=dict(self.sender) or None,
        )

    async def _change_rendition(self, because: str) -> None:
        """Restart the pull on the rendition the ladder now points at."""
        ladder = self._ladder
        if ladder is None:
            return
        _LOGGER.warning(
            "moving to the %s rendition (%dkbps): %s",
            ladder.name,
            ladder.bandwidth // 1000,
            because,
        )
        self._variant = ladder.variant
        self._pace.reset()
        self._details = {}
        describer, self._describer = self._describer, None
        if describer is not None:
            describer.cancel()
        self._describer = asyncio.create_task(self._describe())
        self._switching = True
        await self._kill()

    async def _supervise(self, url: str) -> None:
        """Keep the stream running until this task is cancelled."""
        _LOGGER.info("pulling %s", url)
        self._is_hls = self._looks_like_hls(url)
        self._needs_stripping = False
        self._pull_urls = []
        self._pulling = False
        await asyncio.get_running_loop().run_in_executor(None, self._inspect, url)
        attempt = 0
        while True:
            started = time.monotonic()
            code = await self._attempt(url)
            ran_for = time.monotonic() - started

            if code == 0 and not self._is_hls:
                _LOGGER.info("the source finished after %.0fs", ran_for)
                self.source_url = None
                self._finish("finished")
                self._clear()
                return

            if self._switching:
                self._switching = False
                self._last_error = None
                attempt = 0
                continue

            attempt = 1 if ran_for >= SETTLED_SECONDS else attempt + 1
            delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
            _LOGGER.warning(
                "stream ended after %.0fs (ffmpeg exit %s); retrying in %ds",
                ran_for,
                code,
                delay,
            )
            await asyncio.sleep(delay)

    async def _attempt(self, url: str) -> int | None:
        """Run ffmpeg once and return its exit code."""
        if self._needs_stripping or (self._pulling and self._pull_urls):
            return await self._attempt_stripped(url)
        return await self._attempt_direct(url)

    async def _attempt_stripped(self, url: str) -> int | None:
        """Pass the source's own segments through, with no ffmpeg involved."""
        republisher = Republisher(
            self.directory, PLAYLIST, self.hls_list_size, start=self._numbering_starts_at()
        )
        if self._pulling and self._pull_urls:
            at = self._variant if self._variant is not None and len(self._pull_urls) > 1 else 0
            url = self._pull_urls[at]
        puller = StrippingPuller(url, self.user_agent)
        self._republisher, self._puller = republisher, puller
        self._apply_cushion()
        _LOGGER.info("republishing the source's own segments, unchanged")
        try:
            await puller.run(republisher.add)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._last_error = f"source stopped: {error}"
            _LOGGER.error("the source stopped: %s", error)
        finally:
            self._republisher = self._puller = None
        return 0

    async def _attempt_direct(self, url: str) -> int | None:
        """Let ffmpeg fetch the source itself."""
        command = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "warning",
            # Probe deep enough to find the video behind an image prefixed to each segment.
            "-analyzeduration",
            "15000000",
            "-probesize",
            "50000000",
            *(("-user_agent", self.user_agent) if self.user_agent else ()),
            # Keep connections open between segments, and ride out a dropped one.
            "-http_persistent",
            "1",
            "-multiple_requests",
            "1",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "2",
            # Accept any segment extension; each switch is the spelling of one ffmpeg version.
            "-extension_picky",
            "0",
            "-allowed_extensions",
            "ALL",
            "-allowed_segment_extensions",
            "ALL",
            # No -re: a live HLS source already arrives at playback speed.
            "-i",
            url,
            # Every audio track; no subtitles, as MPEG-TS cannot carry HLS WebVTT.
            *(
                ("-map", f"0:p:{self._variant}:v:0", "-map", f"0:p:{self._variant}:a")
                if self._variant is not None
                else ("-map", "0:v:0", "-map", "0:a?")
            ),
            *_track_names(self._languages),
            "-c",
            "copy",
            "-f",
            "hls",
            "-hls_time",
            str(self.hls_time),
            "-hls_list_size",
            str(self.hls_list_size),
            # omit_endlist: a source stall is not the end; discont_start: reset at each join.
            "-hls_flags",
            "delete_segments+omit_endlist+temp_file+discont_start",
            "-start_number",
            str(self._numbering_starts_at()),
            "-hls_segment_type",
            "mpegts",
            "-hls_segment_filename",
            str(self.directory / "seg_%05d.ts"),
            str(self.directory / PLAYLIST),
        ]
        _LOGGER.debug("ffmpeg %s", " ".join(command))
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._process = process
        try:
            return await self._drain(process)
        finally:
            # terminate() runs before any await, so a repeated cancellation cannot orphan it.
            self._process = None
            if process.returncode is None:
                process.terminate()
                with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(process.wait(), timeout=5)
                if process.returncode is None:
                    process.kill()

    async def _drain(self, process: asyncio.subprocess.Process) -> int | None:
        """Log ffmpeg's complaints until it exits, then return its code."""
        assert process.stderr is not None
        complaint = None
        async for line in process.stderr:
            text = line.decode(errors="replace").rstrip()
            if text:
                _LOGGER.warning("ffmpeg: %s", text)
                complaint = text[:200]
        code = await process.wait()
        if code:
            self._last_error = complaint
        return code

    async def _kill(self) -> None:
        """Stop the running ffmpeg, if there is one."""
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5)
        if process.returncode is None:
            process.kill()
            await process.wait()
        _LOGGER.info("stopped")

    def _numbering_starts_at(self) -> int:
        """Return the first segment number a new run may use."""
        for segment in self.directory.glob("seg_*.ts"):
            with contextlib.suppress(ValueError):
                index = int(segment.stem.removeprefix("seg_"))
                self._next_segment = max(self._next_segment, index + 1)
        # Never below the clock: segments come slower than one a second, so numbering
        # keeps moving forward across restarts, and players never see it go back.
        clock = int(time.time()) - NUMBERING_EPOCH
        self._next_segment = max(self._next_segment, clock)
        return self._next_segment

    def _clear(self) -> None:
        """Remove the previous stream's segments."""
        removed = 0
        # Read the numbering before deleting the segments it is read from.
        self._numbering_starts_at()
        for segment in self.directory.glob("seg_*.ts"):
            segment.unlink(missing_ok=True)
            removed += 1
        (self.directory / PLAYLIST).unlink(missing_ok=True)
        self._warned_leftovers = False
        if removed:
            _LOGGER.info("cleared %d segments", removed)
        self.directory.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        """Shut the HTTP server down."""
        idler, self._idler = self._idler, None
        if idler is not None:
            idler.cancel()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
