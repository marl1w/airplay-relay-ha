"""Tests for kept-alive fetching and parallel segment download."""

from __future__ import annotations

import asyncio
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from typing import ClassVar

from airplay_relay.fetch import Fetcher
from airplay_relay.hls import StrippingPuller, pullable, variant_uris
from airplay_relay.republish import START_SECONDS_BACK, Republisher

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


def test_widening_starts_players_further_back_and_holds_more(tmp_path) -> None:
    republisher = Republisher(tmp_path, "channel.m3u8", window=6)
    republisher.widen(30)
    for _ in range(12):
        republisher.add(b"\x47" * 188, 6.0)

    playlist = (tmp_path / "channel.m3u8").read_text()
    assert f"TIME-OFFSET=-{START_SECONDS_BACK + 30:.3f}" in playlist
    assert playlist.count("#EXTINF") == 11


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
