"""Tests for kept-alive fetching and parallel segment download."""

from __future__ import annotations

import asyncio
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from typing import ClassVar

from airplay_relay.fetch import Fetcher
from airplay_relay.hls import StrippingPuller, pullable, variant_uris
from airplay_relay.republish import Republisher

SEGMENT = b"\x47" + b"\x00" * 187


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    connections: ClassVar[set] = set()

    def do_GET(self) -> None:
        self.connections.add(self.client_address)
        if self.path == "/go":
            self.send_response(302)
            self.send_header("Location", "/a.ts")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = SEGMENT * 8
        if self.path.startswith("/big"):
            body = bytes(range(256)) * 8192
            if ranged := self.headers.get("Range"):
                start, end = (int(n) for n in ranged.removeprefix("bytes=").split("-"))
                chunk = body[start : end + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                self.wfile.write(chunk)
                return
        if self.path.endswith(".m3u8"):
            body = (
                b"#EXTM3U\n#EXT-X-TARGETDURATION:2\n"
                b"#EXTINF:2.0,\na.ts\n#EXTINF:2.0,\nb.ts\n#EXTINF:2.0,\nc.ts\n"
            )
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def _serve():
    _Handler.connections = set()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def test_one_connection_serves_many_requests() -> None:
    server, base = _serve()
    fetcher = Fetcher()
    for _ in range(5):
        assert fetcher.get(f"{base}/a.ts") == SEGMENT * 8
    server.shutdown()
    assert len(_Handler.connections) == 1


def test_a_redirect_is_followed() -> None:
    server, base = _serve()
    assert Fetcher().get(f"{base}/go") == SEGMENT * 8
    server.shutdown()


def test_the_puller_writes_every_segment_in_order(tmp_path) -> None:
    server, base = _serve()
    puller = StrippingPuller(f"{base}/x.m3u8")
    written: list[float] = []

    async def run() -> None:
        def write(data: bytes, seconds: float, missed: bool) -> None:
            written.append(seconds)
            if len(written) == 3:
                raise asyncio.CancelledError

        with contextlib.suppress(asyncio.CancelledError):
            await puller.run(write)

    asyncio.run(run())
    server.shutdown()
    assert written == [2.0, 2.0, 2.0]


def test_a_large_segment_is_split_across_connections_and_reassembled() -> None:
    server, base = _serve()
    body = Fetcher().get_split(f"{base}/big.ts")
    server.shutdown()
    assert body == bytes(range(256)) * 8192
    assert len(_Handler.connections) > 1


def test_a_server_without_ranges_still_serves_the_whole_segment() -> None:
    server, base = _serve()
    assert Fetcher().get_split(f"{base}/a.ts") == SEGMENT * 8
    server.shutdown()


def test_variant_uris_follow_the_order_of_the_variants() -> None:
    master = (
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000,RESOLUTION=640x360\nlow/index.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=3000,RESOLUTION=1280x720\nhigh/index.m3u8\n"
    )
    assert variant_uris(master, "http://h/a/master.m3u8") == [
        "http://h/a/low/index.m3u8",
        "http://h/a/high/index.m3u8",
    ]


def test_only_live_unencrypted_playlists_are_pullable() -> None:
    assert pullable("#EXTM3U\n#EXTINF:2,\na.ts\n")
    assert not pullable("#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=k\n#EXTINF:2,\na.ts\n")
    assert not pullable("#EXTM3U\n#EXTINF:2,\na.ts\n#EXT-X-ENDLIST\n")


def _listed(directory) -> int:
    return (directory / "channel.m3u8").read_text().count("#EXTINF")


def test_without_a_depth_segments_are_listed_at_once(tmp_path) -> None:
    republisher = Republisher(tmp_path, "channel.m3u8", window=6)
    republisher.add(b"\x47" * 188, 6.0, now=0.0)

    assert _listed(tmp_path) == 1


def test_a_burst_is_listed_evenly_and_a_short_gap_goes_unseen(tmp_path) -> None:
    republisher = Republisher(tmp_path, "channel.m3u8", window=20)
    republisher.widen(12)
    for _ in range(5):
        republisher.add(b"\x47" * 188, 6.0, now=0.0)

    # Three to begin with, the rest held back.
    assert _listed(tmp_path) == 3
    assert republisher.reservoir == 12.0

    republisher.release(now=6.0)
    assert _listed(tmp_path) == 4
    republisher.release(now=12.0)
    assert _listed(tmp_path) == 5
    assert not republisher.underruns


def test_a_reservoir_that_runs_dry_is_recorded_once(tmp_path) -> None:
    republisher = Republisher(tmp_path, "channel.m3u8", window=20)
    republisher.widen(12)
    for _ in range(5):
        republisher.add(b"\x47" * 188, 6.0, now=0.0)
    republisher.release(now=6.0)
    republisher.release(now=12.0)
    republisher.release(now=17.0)
    assert not republisher.underruns

    republisher.release(now=18.0)
    republisher.release(now=19.0)
    assert list(republisher.underruns) == [18.0]

    # Late, the next segment is listed at once, and the schedule starts again from it.
    republisher.add(b"\x47" * 188, 6.0, now=25.0)
    assert _listed(tmp_path) == 6


def test_a_slow_start_begins_anyway_after_the_wait(tmp_path) -> None:
    republisher = Republisher(tmp_path, "channel.m3u8", window=20)
    republisher.widen(30)
    republisher.add(b"\x47" * 188, 6.0, now=0.0)
    assert not (tmp_path / "channel.m3u8").exists()

    republisher.release(now=21.0)
    assert _listed(tmp_path) == 1


def test_the_referer_is_sent_on_every_kind_of_fetch() -> None:
    seen: list[str | None] = []

    class Gate(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            seen.append(self.headers.get("Referer"))
            allowed = self.headers.get("Referer") == "https://page.example/"
            body = SEGMENT if allowed else b"no"
            self.send_response(200 if allowed else 412)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Gate)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/a.ts"
        fetcher = Fetcher("test", referer="https://page.example/")
        assert fetcher.get_plain(url) == SEGMENT
        assert fetcher.get(url) == SEGMENT
    finally:
        server.shutdown()
    assert seen == ["https://page.example/", "https://page.example/"]
