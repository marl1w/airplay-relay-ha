"""Tests for kept-alive fetching and parallel segment download."""

from __future__ import annotations

import asyncio
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from typing import ClassVar

from airplay_relay.fetch import Fetcher
from airplay_relay.hls import StrippingPuller
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
