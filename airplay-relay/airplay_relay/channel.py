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
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from . import status
from .hls import StrippingPuller, payload_offset
from .logo import draw as draw_logo
from .page import PAGE
from .republish import Republisher

_LOGGER = logging.getLogger(__name__)

PLAYLIST = "channel.m3u8"

CHANNEL_LIST = "channels.m3u"
LOGO = "logo.png"

RETRY_DELAYS = (1, 2, 4, 8, 15, 30)

SETTLED_SECONDS = 60

IDLE_MINUTES = 15

# Explicit: system mimetypes vary, and IPTV apps file audio/x-mpegurl lists as radio.
mimetypes.add_type("application/vnd.apple.mpegurl", ".m3u8")
mimetypes.add_type("video/mp2t", ".ts")
mimetypes.add_type("audio/x-mpegurl", ".m3u")


class _Handler(SimpleHTTPRequestHandler):
    """Serves the segment directory, plus a page describing what it is doing."""

    channel: Channel

    def log_message(self, format: str, *args) -> None:
        """Keep segment and status-page requests out of the add-on log."""

    def do_GET(self) -> None:
        """Answer the status endpoints, and otherwise serve the directory."""
        route = self.path.split("?")[0].rstrip("/") or "/"
        if route in ("/", "/index.html"):
            self._send(PAGE.encode(), "text/html; charset=utf-8")
        elif route == "/status.json":
            self._send(json.dumps(self.channel.snapshot()).encode(), "application/json")
        elif route.endswith(".ts") or route.endswith(".m3u8"):
            self.channel.note_viewer()
            super().do_GET()
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
        self.sender: dict[str, str] = {}
        self._sender_seen: float = 0.0
        self._needs_stripping = False

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

    def _write_channel_list(self) -> None:
        """Write the channel list for IPTV players."""
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
        # A subclass, not a partial keyword: the handler reads `channel` from its class.
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
        """Stop pulling and stop retrying, recording why."""
        self._paused_url = None
        self.source_url = None
        await self._halt()
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
            "last_error": self._last_error,
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
        """Pass the source's own segments through, with no ffmpeg involved."""
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
            # Probe deep enough to find the video behind an image prefixed to each segment.
            "-analyzeduration",
            "15000000",
            "-probesize",
            "50000000",
            *(("-user_agent", self.user_agent) if self.user_agent else ()),
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
            # terminate() runs before any await, so a repeated cancellation cannot orphan it.
            self._process = None

    async def _drain(self, process: asyncio.subprocess.Process) -> int | None:
        """Log ffmpeg's complaints until it exits, then return its code."""
        assert process.stderr is not None
        async for line in process.stderr:
            text = line.decode(errors="replace").rstrip()
            if text:
                _LOGGER.warning("ffmpeg: %s", text)
                self._last_error = text[:200]
        return await process.wait()

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
        """Remove the previous stream's segments."""
        for segment in self.directory.glob("seg_*.ts"):
            segment.unlink(missing_ok=True)
        (self.directory / PLAYLIST).unlink(missing_ok=True)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._write_channel_list()

    def close(self) -> None:
        """Shut the HTTP server down."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
