"""One stream pulled from the internet, served to as many viewers as want it.

The receiver hands over a URL; ffmpeg fetches that once and republishes it as a
short HLS window on local disk, which is really RAM -- the segment directory is
under /dev/shm. Viewers read those files over HTTP. A second or tenth viewer
therefore costs nothing upstream, which is the whole point: the link out of the
house carries one copy of the stream no matter how many televisions are on.

Nothing is re-encoded. `-c copy` repackages the incoming segments as they are,
so the work is a few percent of a core rather than a transcode, and the picture
is whatever the source sent.

A stream that fails is retried until the sender says to stop. Upstreams drop,
tokens go stale for a moment, and the alternative is walking to a phone to press
AirPlay again -- so only an explicit stop ends a channel.
"""

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
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from . import status
from .hls import StrippingPuller, payload_offset
from .logo import draw as draw_logo
from .page import PAGE
from .republish import Republisher

_LOGGER = logging.getLogger(__name__)

PLAYLIST = "channel.m3u8"

# The channel list an IPTV player wants. Not to be confused with the playlist
# above: that one lists video segments, this one lists channels. Pointing an
# IPTV app at the segment playlist makes it try to read segments as channels,
# which fails in a way that says nothing useful.
CHANNEL_LIST = "channels.m3u"
LOGO = "logo.png"

# Backoff between attempts, in seconds; the last value repeats forever.
RETRY_DELAYS = (1, 2, 4, 8, 15, 30)

# A stream that ran at least this long was working, so the next failure starts
# the backoff again rather than inheriting a long delay from hours ago.
SETTLED_SECONDS = 60

# How long the channel keeps running with nobody watching and no sender. The
# phone going quiet is not a reason to stop -- the house may well be watching --
# but pulling a stream into an empty room for ever is not sensible either.
IDLE_MINUTES = 15

# Served explicitly because the mimetypes database disagrees with itself across
# distributions, and a playlist sent as text/plain is refused by some players.
mimetypes.add_type("application/vnd.apple.mpegurl", ".m3u8")
mimetypes.add_type("video/mp2t", ".ts")
mimetypes.add_type("audio/x-mpegurl", ".m3u")


class _Handler(SimpleHTTPRequestHandler):
    """Serves the segment directory, plus a page describing what it is doing."""

    channel: Channel

    def log_message(self, format: str, *args) -> None:
        """Keep segment and status-page requests out of the add-on log.

        The page polls twice a second and a player fetches a segment every two;
        logging either drowns the lines that matter.
        """

    def do_GET(self) -> None:
        """Answer the status endpoints, and otherwise serve the directory."""
        route = self.path.split("?")[0].rstrip("/") or "/"
        if route in ("/", "/index.html"):
            self._send(PAGE.encode(), "text/html; charset=utf-8")
        elif route == "/status.json":
            self._send(json.dumps(self.channel.snapshot()).encode(), "application/json")
        elif route.endswith(".ts") or route.endswith(".m3u8"):
            # Nothing is playing, so there is nothing to hand over. Whatever is
            # still in the directory belongs to a stream that has ended, and
            # serving it would show a player the last thing that was on as
            # though it were live -- which is the failure this guards against,
            # not an empty window.
            if not self.channel.requested:
                self.channel.note_leftovers(route)
                self.send_error(404, "nothing is playing")
                return
            # Someone is watching. Recorded before serving, so a viewer who
            # arrives while the sender is silent still counts.
            self.channel.note_viewer()
            super().do_GET()
        elif route == "/stop":
            # The sender's stop is ignored, so this is how a stream is ended.
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
        address: str = "",
    ) -> None:
        """Prepare the segment directory and the server that will offer it."""
        self.directory = Path(directory)
        self.user_agent = user_agent
        self.name = name
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
        # Kept after the sender goes quiet, deliberately: the phone is a remote
        # control, and knowing who put a stream on is still worth showing once
        # they have locked their screen and walked off.
        # Reset whenever the directory is cleared, so one line is logged per
        # window left behind rather than one per request from every player.
        self._warned_leftovers = False
        self.sender: dict[str, str] = {}
        self._sender_seen: float = 0.0
        # Set when the source hides its transport stream behind a prefix.
        self._needs_stripping = False

    @property
    def requested(self) -> bool:
        """Whether a sender has asked for something, whatever came of it."""
        return self.source_url is not None

    @property
    def playing(self) -> bool:
        """Whether segments are actually being produced.

        Deliberately not "has a URL been handed over": ffmpeg can fail for
        minutes on a source it cannot read, and reporting that as playing makes
        the panel and the phone both claim a stream that no player can find.
        """
        return self.requested and (self.directory / PLAYLIST).exists()

    @property
    def position(self) -> float:
        """Seconds since this stream started.

        A sender decides whether playback is moving by watching this. Reporting
        a position that never changes -- which is what reporting zero amounts to
        -- makes the phone show the stream as paused while it plays perfectly
        well on every screen in the house.
        """
        if self._started_at is None:
            return 0.0
        return max(0.0, time.monotonic() - self._started_at)

    def _write_channel_list(self) -> None:
        """Write the channel list, so a player is configured once and for good.

        It is static and always present, whether or not anything is playing --
        an IPTV app expects its channels to exist even when they are off air.
        """
        host = self.address or "127.0.0.1"
        (self.directory / CHANNEL_LIST).write_text(
            "#EXTM3U\n"
            f'#EXTINF:-1 tvg-id="relay" tvg-name="{self.name}"'
            f' tvg-logo="http://{host}:{self.port}/{LOGO}"'
            f' group-title="Home",{self.name}\n'
            f"http://{host}:{self.port}/{PLAYLIST}\n",
            encoding="utf-8",
        )

    def serve(self) -> None:
        """Start serving the segment directory."""
        with contextlib.suppress(RuntimeError):
            self._loop = asyncio.get_running_loop()
        self.directory.mkdir(parents=True, exist_ok=True)
        draw_logo(self.directory / LOGO)
        self._write_channel_list()
        # A subclass per channel, because the handler reads `self.channel` and
        # attributes set on a functools.partial never reach the instances it
        # builds -- which is why /status.json failed while plain files served.
        bound = type("BoundHandler", (_Handler,), {"channel": self})
        handler = partial(bound, directory=str(self.directory))
        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        _LOGGER.info(
            "stream on :%d/%s -- give an IPTV player :%d/%s",
            self.port,
            PLAYLIST,
            self.port,
            CHANNEL_LIST,
        )

    async def play(self, url: str) -> None:
        """Replace whatever is playing with the stream at this URL."""
        await self.stop()
        self._clear()
        self.source_url = url
        self._last_error = None
        self._started_at = time.monotonic()
        self._details = {}
        self._supervisor = asyncio.create_task(self._supervise(url))
        self._describer = asyncio.create_task(self._describe())

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

    def note_leftovers(self, route: str) -> None:
        """Say once that a player asked for a window the channel has ended.

        An empty directory here is ordinary: nobody has AirPlayed anything yet,
        or the last stream was cleared properly. Segments still sitting in it
        are not, and the count and the route are the only evidence of which
        path stopped the stream without clearing up after itself.
        """
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

    def note_viewer(self) -> None:
        """Record that someone fetched part of the stream just now."""
        self._last_viewer = time.monotonic()

    @property
    def seconds_since_viewer(self) -> float:
        """How long since anyone last fetched anything."""
        if not self._last_viewer:
            return 0.0
        return time.monotonic() - self._last_viewer

    def request_stop(self) -> None:
        """Ask for the stream to end, from a thread that is not the loop's."""
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self.stop()))

    async def stop(self) -> None:
        """Stop pulling and stop retrying."""
        self._paused_url = None
        self.source_url = None
        await self._halt()
        # Leaving the last window in place means players keep reading a playlist
        # nobody is updating, and go on showing a stream that ended some time
        # ago -- which looks exactly like the wrong video being served.
        self._clear()

    async def _halt(self) -> None:
        """Stop the running stream without deciding what that means."""
        supervisor, self._supervisor = self._supervisor, None
        describer, self._describer = self._describer, None
        if describer is not None:
            describer.cancel()
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
        """Say what the source actually serves, before ffmpeg has to guess.

        When a CDN does not recognise the client it tends to answer with a
        placeholder rather than an error, and ffmpeg then reports the confusing
        "Could not find codec parameters ... Video: webp". Fetching the playlist
        and its first segment here turns that into a statement of fact: the
        status, the media type, and the first bytes.
        """
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
                if "#EXT-X-STREAM-INF" in body:
                    _LOGGER.info("this is a master playlist -- ffmpeg chooses a variant from it")
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
                    return

                # An image header does not mean there is no video: some sources
                # append the transport stream to a real picture so that an image
                # CDN will host it. Look past the prefix before believing the
                # opening bytes.
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
            # A problem is only a problem while there is no stream: once
            # segments are arriving, whatever went wrong on the way was
            # survived, and saying so over a playing channel is noise.
            "last_error": None if playing else self._last_error,
            "seconds_since_viewer": round(self.seconds_since_viewer),
            "sender": self.sender,
            "sender_seen": round(self.seconds_since_sender),
            "position": round(self.position, 1),
            "source_host": status.source_host(self.source_url),
            "source_url": self.source_url,
            "segments": segments,
            "window_seconds": round(seconds, 1),
            "window_bytes": size,
            "bitrate_kbps": bitrate,
            "stream_url": f"http://{self.address}:{self.port}/{PLAYLIST}",
            "playlist_url": f"http://{self.address}:{self.port}/{CHANNEL_LIST}",
            **self._details,
        }

    async def _describe(self) -> None:
        """Read codecs and resolution once a few segments exist."""
        for _ in range(20):
            await asyncio.sleep(3)
            if not self.playing:
                return
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

    async def _supervise(self, url: str) -> None:
        """Keep the stream running until this task is cancelled."""
        _LOGGER.info("pulling %s", url)
        self._is_hls = self._looks_like_hls(url)
        self._needs_stripping = False
        await asyncio.get_running_loop().run_in_executor(None, self._inspect, url)
        attempt = 0
        while True:
            started = time.monotonic()
            code = await self._attempt(url)
            ran_for = time.monotonic() - started

            # A file plays to its end and exits cleanly; retrying that just
            # starts it again from the beginning, over and over. Only a live
            # source is worth reconnecting to.
            if code == 0 and not self._is_hls:
                _LOGGER.info("the source finished after %.0fs", ran_for)
                self.source_url = None
                self._clear()
                return

            if self.seconds_since_viewer > IDLE_MINUTES * 60:
                _LOGGER.info("nobody has watched for %d minutes; stopping", IDLE_MINUTES)
                self.source_url = None
                self._clear()
                return

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
        if self._needs_stripping:
            return await self._attempt_stripped(url)
        return await self._attempt_direct(url)

    async def _attempt_stripped(self, url: str) -> int | None:
        """Pass the source's own segments through, with no ffmpeg involved.

        Stripped of their prefix these segments are already valid MPEG-TS with
        the durations the source states, so repackaging them achieved nothing
        except to add a pipe that could stall and a process that could exit.
        """
        republisher = Republisher(self.directory, PLAYLIST, self.hls_list_size)
        puller = StrippingPuller(url, self.user_agent)
        _LOGGER.info("republishing the source's own segments, unchanged")
        try:
            await puller.run(republisher.add)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._last_error = f"source stopped: {error}"
            _LOGGER.error("the source stopped: %s", error)
        return 0

    async def _attempt_direct(self, url: str) -> int | None:
        """Let ffmpeg fetch the source itself."""
        command = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "warning",
            # Some sources prepend a real WebP image to each segment so an image
            # CDN will host it, with the h264 and aac behind. ffmpeg sniffs the
            # first bytes, decides the segment is a picture and stops -- inside
            # the HLS demuxer analyzeduration is 0, so it never reads far enough
            # to find the video. Apple's own player scans past it, which is why
            # the same stream plays on an Apple TV and not here.
            "-analyzeduration",
            "15000000",
            "-probesize",
            "50000000",
            # ffmpeg refuses HLS segments whose file extension is not on a safe
            # list, and real playlists do not respect that: the first stream
            # this ever saw served its segments as ".image" from a CDN, and
            # ffmpeg rejected the lot with "Invalid data found when processing
            # input". All three switches say the same thing to different ffmpeg
            # versions, and all three exist in the one this image carries.
            *(("-user_agent", self.user_agent) if self.user_agent else ()),
            "-extension_picky",
            "0",
            "-allowed_extensions",
            "ALL",
            "-allowed_segment_extensions",
            "ALL",
            # A live HLS source already arrives at playback speed, so -re would
            # throttle it a second time and the window would fall behind.
            "-i",
            url,
            "-c",
            "copy",
            "-f",
            "hls",
            "-hls_time",
            str(self.hls_time),
            "-hls_list_size",
            str(self.hls_list_size),
            # delete_segments keeps RAM flat; omit_endlist keeps players from
            # treating a pause in the source as the end of the stream.
            "-hls_flags",
            "delete_segments+omit_endlist+temp_file",
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
            self._process = None

    async def _drain(self, process: asyncio.subprocess.Process) -> int | None:
        """Log ffmpeg's complaints until it exits, then return its code.

        Everything ffmpeg says at this log level is worth having in the add-on
        log, and almost none of it is fatal: "mime type is not rfc8216
        compliant" is a CDN labelling its playlist loosely, and the stream then
        plays for hours. Only a complaint from a run that actually failed is
        shown on the page, or the panel reports a problem with a stream the
        viewers are watching quite happily.
        """
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

    def _clear(self) -> None:
        """Remove the previous stream's segments.

        Without this a player that reconnects can be handed segments from the
        stream before, which it will happily decode into someone else's film.
        """
        removed = 0
        for segment in self.directory.glob("seg_*.ts"):
            segment.unlink(missing_ok=True)
            removed += 1
        (self.directory / PLAYLIST).unlink(missing_ok=True)
        self._warned_leftovers = False
        if removed:
            _LOGGER.info("cleared %d segments", removed)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._write_channel_list()

    def close(self) -> None:
        """Shut the HTTP server down."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
