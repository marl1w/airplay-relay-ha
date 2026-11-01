"""Tests for timeline jumps in the source, byte ranges, and learned delay."""

from __future__ import annotations

import asyncio
import contextlib
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from urllib.request import Request, urlopen

from airplay_relay.channel import _Handler as ChannelHandler
from airplay_relay.history import History
from airplay_relay.hls import WRAP, StrippingPuller, first_pts
from airplay_relay.republish import Republisher


def _timed(seconds: float) -> bytes:
    """Return one MPEG-TS packet opening a video PES stamped at this time."""
    ticks = round(seconds * 90_000) % WRAP
    pts = bytes(
        [
            0x21 | ((ticks >> 29) & 0x0E),
            (ticks >> 22) & 0xFF,
            ((ticks >> 14) & 0xFE) | 1,
            (ticks >> 7) & 0xFF,
            ((ticks << 1) & 0xFE) | 1,
        ]
    )
    pes = b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + pts
    return (b"\x47\x41\x00\x10" + pes).ljust(188, b"\xff")


def test_the_first_timestamp_is_read_from_the_stream() -> None:
    plain = b"\x47\x00\x00\x10".ljust(188, b"\x00")
    assert first_pts(plain + _timed(12.5)) == 12.5 * 90_000
    assert first_pts(plain * 3) is None


def _source(starts: list[float]):
    """Serve a playlist of 2s segments whose timestamps begin at `starts`."""

    class Source(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            if self.path.endswith(".m3u8"):
                body = b"#EXTM3U\n#EXT-X-TARGETDURATION:2\n" + b"".join(
                    b"#EXTINF:2.0,\n%d.ts\n" % n for n in range(len(starts))
                )
            else:
                body = _timed(starts[int(self.path.strip("/").split(".")[0])])
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Source)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/x.m3u8"


def _gaps(starts: list[float], expect: int | None = None) -> list[bool]:
    """Return, for each segment passed on, whether it was marked as after a gap."""
    server, url = _source(starts)
    puller = StrippingPuller(url)
    marked: list[bool] = []
    wanted = len(starts) if expect is None else expect

    async def run() -> None:
        def write(data: bytes, seconds: float, gap: bool) -> None:
            marked.append(gap)
            if len(marked) == wanted:
                raise asyncio.CancelledError

        # Bounded, so a segment wrongly dropped fails the test instead of hanging it.
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(puller.run(write), timeout=5)

    asyncio.run(run())
    server.shutdown()
    return marked


def test_a_source_that_skips_ahead_is_marked_as_a_discontinuity() -> None:
    assert _gaps([100.0, 102.0, 110.0, 112.0]) == [False, False, True, False]


def test_segments_the_source_sends_again_under_new_names_are_dropped() -> None:
    # As seen live: two 2s segments listed a second time, then the stream goes on.
    assert _gaps([100.0, 102.0, 104.0, 102.0, 104.0, 106.0], expect=4) == [
        False,
        False,
        False,
        False,
    ]


def test_a_source_that_restarts_its_clock_is_a_jump_not_a_repeat() -> None:
    assert _gaps([1000.0, 1002.0, 5.0, 7.0]) == [False, False, True, False]


def test_segments_a_little_off_their_listed_length_are_not_jumps() -> None:
    assert _gaps([100.0, 101.4, 103.9, 105.9]) == [False, False, False, False]


def test_timestamps_wrapping_round_are_not_a_jump() -> None:
    top = WRAP / 90_000
    assert _gaps([top - 2.0, 0.0, 2.0]) == [False, False, False]


def test_the_relay_writes_the_discontinuity_it_is_told_of(tmp_path) -> None:
    republisher = Republisher(tmp_path, "channel.m3u8", window=6)
    republisher.add(b"\x47" * 188, 2.0)
    republisher.add(b"\x47" * 188, 2.0, after_gap=True)
    playlist = (tmp_path / "channel.m3u8").read_text().splitlines()
    assert playlist[playlist.index("#EXT-X-DISCONTINUITY") + 2] == "seg_00001.ts"


class _Watching:
    requested = True
    standing_by = False

    def note_viewer(self, peer: str = "") -> None:
        pass

    def note_transfer(self, *args) -> None:
        pass


def _get(base: str, span: str | None) -> tuple[int, dict, bytes]:
    request = Request(f"{base}/seg_00000.ts", headers={"Range": span} if span else {})
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, dict(response.headers), response.read()
    except Exception as error:  # urllib raises for 416
        return error.code, dict(error.headers), b""


def test_a_player_can_ask_for_part_of_a_segment(tmp_path) -> None:
    body = bytes(range(256)) * 40
    (tmp_path / "seg_00000.ts").write_bytes(body)
    bound = type("Bound", (ChannelHandler,), {"channel": _Watching()})
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(bound, directory=str(tmp_path)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, headers, data = _get(base, "bytes=1000-1999")
        assert (status, data) == (206, body[1000:2000])
        assert headers["Content-Range"] == f"bytes 1000-1999/{len(body)}"

        assert _get(base, "bytes=10000-")[2] == body[10000:]
        assert _get(base, "bytes=-100")[2] == body[-100:]
        assert _get(base, f"bytes={len(body)}-")[0] == 416

        status, headers, data = _get(base, None)
        assert (status, data) == (200, body)
        assert headers["Accept-Ranges"] == "bytes"
    finally:
        server.shutdown()


def test_a_site_that_ran_late_lately_is_remembered_across_its_hosts(tmp_path) -> None:
    history = History(tmp_path)
    late = history.start(source_host="vishnu.example.st")
    late["samples"].append({"extra_delay": 45})
    history.start(source_host="other.test")["samples"].append({"extra_delay": 60})

    assert history.delay_needed("bharat.example.st") == 45
    assert history.delay_needed("new.example.org") == 0

    late["started"] = time.time() - 8 * 86400
    assert history.delay_needed("bharat.example.st") == 0
