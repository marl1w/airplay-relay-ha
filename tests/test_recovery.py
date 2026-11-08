"""Tests for riding out a source that drops: retries, a dead link, a fresh one, and the card.

Each reproduces what happened live on 2026-11-08: a link revoked mid-stream (403s for
75s), the TV replaying the same window while nothing new arrived, and every resend
from the phone starting the stream over.
"""

from __future__ import annotations

import asyncio
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from test_jumps import _timed

from airplay_relay import hls
from airplay_relay.channel import Channel, same_stream_shape
from airplay_relay.history import History
from airplay_relay.hls import REFUSED_SECONDS, StrippingPuller, refusal_advice
from airplay_relay.republish import FILL_AFTER_SECONDS, Republisher

# --- what counts as the same stream ----------------------------------------------------

LIVE = "https://{host}.example-cdn.test/secure/{token}/1700000000/1700028800/{name}/index.m3u8"


def test_a_fresh_token_or_another_mirror_is_the_same_stream() -> None:
    first = LIVE.format(host="mirror1", token="Q7mZp2LxV9rTk4NwY8bHc3JdF6gSa1Ue", name="channel_a")
    again = LIVE.format(host="mirror2", token="Hn4Wq8ZtR2vLm6KcX1pJy9BdG3sFa7Ex", name="channel_a")
    assert same_stream_shape(first, again)


def test_another_channel_or_rendition_is_not() -> None:
    token = "Q7mZp2LxV9rTk4NwY8bHc3JdF6gSa1Ue"
    first_channel = LIVE.format(host="mirror1", token=token, name="channel_a")
    assert not same_stream_shape(
        first_channel, LIVE.format(host="mirror1", token=token, name="channel_b")
    )
    assert not same_stream_shape(
        first_channel, LIVE.format(host="mirror1", token=token, name="channel_a_540p")
    )
    assert not same_stream_shape(
        first_channel, first_channel.replace("example-cdn.test", "elsewhere.test")
    )


def test_a_short_number_telling_streams_apart_is_kept() -> None:
    base = "https://lb3.example-live.test/secure/AbCdEfGhIjKlMnOpQr/live/stream/{n}/playlist.m3u8"
    assert not same_stream_shape(base.format(n=1), base.format(n=6))


# --- a source that serves timed segments, and can be made to fail ----------------------


class _Source:
    """A live playlist of 2s segments starting at `clock`, served under any token."""

    def __init__(self, clock: float = 1000.0) -> None:
        self.clock = clock
        self.refuse = False
        self.fail_once: set[int] = set()
        self.requests: list[str] = []
        source = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                source.requests.append(self.path)
                if self.path.endswith(".m3u8"):
                    if source.refuse:
                        self.send_error(403)
                        return
                    body = b"#EXTM3U\n#EXT-X-TARGETDURATION:2\n" + b"".join(
                        b"#EXTINF:2.0,\n%d.ts\n" % n for n in range(4)
                    )
                else:
                    n = int(self.path.rsplit("/", 1)[1].split(".")[0])
                    if n in source.fail_once:
                        source.fail_once.discard(n)
                        self.send_error(503)
                        return
                    body = _timed(source.clock + 2.0 * n)
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, token: str, name: str = "live") -> str:
        return f"http://127.0.0.1:{self.server.server_port}/secure/{token}/{name}/index.m3u8"

    def close(self) -> None:
        self.server.shutdown()


# --- a segment that fails once is retried, not skipped ---------------------------------


def test_a_segment_that_fails_once_is_fetched_again_rather_than_skipped(monkeypatch) -> None:
    monkeypatch.setattr(hls, "SEGMENT_RETRIES", (0.01, 0.01))
    source = _Source()
    source.fail_once = {1}
    puller = StrippingPuller(source.url("AbCdEfGhIjKlMnOpQrSt"))
    got: list[tuple[int | None, bool]] = []

    async def run() -> None:
        def write(data: bytes, seconds: float, gap: bool) -> None:
            got.append((hls.first_pts(data), gap))
            if len(got) == 4:
                raise asyncio.CancelledError

        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(puller.run(write), timeout=5)

    asyncio.run(run())
    source.close()
    # All four, in order, and no discontinuity: the failure never reached players.
    assert [pts for pts, _ in got] == [round((1000.0 + 2 * n) * 90_000) for n in range(4)]
    assert not any(gap for _, gap in got)


# --- a dead link is named as such ------------------------------------------------------


def test_a_link_refused_for_a_while_is_reported_once_with_what_to_do(caplog) -> None:
    puller = StrippingPuller("https://example.com/x.m3u8")
    refused = Exception("HTTP Error 403: Forbidden")
    for second in range(0, 30, 3):
        puller._note_failure(100.0 + second, refused)
    assert puller.refused_for(130.0) == 30.0
    advice = [r.message for r in caplog.records if "revoked" in r.message]
    assert len(advice) == 1
    assert "send it again from the phone" in advice[0]
    # Each failure after that is kept out of the log.
    assert sum("playlist fetch failed" in r.message for r in caplog.records) <= 6


def test_a_short_outage_is_not_called_a_dead_link() -> None:
    puller = StrippingPuller("https://example.com/x.m3u8")
    puller._note_failure(100.0, Exception("Connection reset by peer"))
    assert puller.refused_for(100.0 + REFUSED_SECONDS - 1) < REFUSED_SECONDS


def test_a_network_failure_is_not_blamed_on_a_revoked_link() -> None:
    assert "revoked" not in refusal_advice("<urlopen error [Errno 111] Connection refused>", 20)
    assert "revoked" in refusal_advice("HTTP Error 403: Forbidden", 20)


# --- a feed that runs dry shows the card instead of a frozen window --------------------


def _dry_feed(tmp_path, filler: list[bytes] | None):
    republisher = Republisher(tmp_path, "channel.m3u8", window=6, filler=filler)
    republisher.depth = 4.0
    clock = 0.0
    for _ in range(4):
        republisher.add(b"\x47" * 188, 2.0, now=clock)
    republisher.release(now=clock + 25.0)  # starts playing once it has waited
    clock = 25.0
    while republisher.reservoir:  # list everything it holds
        clock += 2.0
        republisher.release(now=clock)
    return republisher, clock


def _listing(tmp_path) -> list[str]:
    return (tmp_path / "channel.m3u8").read_text().splitlines()


def test_a_feed_dry_for_a_few_seconds_is_left_alone(tmp_path) -> None:
    republisher, clock = _dry_feed(tmp_path, [b"card"])
    republisher.release(now=clock + 2.0)
    republisher.release(now=clock + FILL_AFTER_SECONDS - 1)
    assert republisher.filled == 0


def test_a_feed_dry_for_long_carries_the_card_rather_than_standing_still(tmp_path) -> None:
    republisher, clock = _dry_feed(tmp_path, [b"card"])
    republisher.release(now=clock + 2.0)  # runs dry
    for step in range(8):
        republisher.release(now=clock + 2.0 + FILL_AFTER_SECONDS + 2.0 * step)
    assert republisher.filled >= 6
    listing = _listing(tmp_path)
    # The window moves on; nothing old is left for a player to replay.
    names = [line for line in listing if line.startswith("seg_")]
    assert all((tmp_path / name).read_bytes() == b"card" for name in names)
    assert names[0] != "seg_00000.ts"


def test_the_stream_coming_back_after_the_card_is_marked_as_a_new_picture(tmp_path) -> None:
    republisher, clock = _dry_feed(tmp_path, [b"card"])
    republisher.release(now=clock + 2.0)
    clock += 2.0 + FILL_AFTER_SECONDS
    republisher.release(now=clock)
    republisher.add(b"\x47" * 188, 2.0, now=clock + 3.0)
    listing = _listing(tmp_path)
    assert listing.count("#EXT-X-DISCONTINUITY") == 2  # into the card, and out of it
    assert listing[-1] == f"seg_{republisher._written - 1:05d}.ts"
    assert listing[-3] == "#EXT-X-DISCONTINUITY"


def test_without_a_card_a_dry_feed_behaves_as_before(tmp_path) -> None:
    republisher, clock = _dry_feed(tmp_path, None)
    republisher.release(now=clock + 2.0 + FILL_AFTER_SECONDS * 3)
    assert republisher.filled == 0


# --- a fresh link to the same stream carries on, without a restart ---------------------


def _carrying(tmp_path, source: _Source, old: str) -> tuple[Channel, StrippingPuller]:
    channel = Channel(
        directory=str(tmp_path / "channel"),
        port=0,
        hls_time=2,
        hls_list_size=6,
        history=History(tmp_path / "state"),
    )
    puller = StrippingPuller(old)
    # Where the relay had got to in the source's clock: just past segment 3.
    puller.expected = round((source.clock + 8.0) * 90_000)
    channel.source_url = old
    channel._puller = puller
    channel._session = channel.history.start(source_url=old)
    return channel, puller


def test_a_fresh_link_to_the_same_stream_carries_on_without_a_restart(tmp_path) -> None:
    source = _Source()
    old, new = source.url("OldTokenAbCdEfGhIjKl"), source.url("NewTokenMnOpQrStUvWx")
    channel, puller = _carrying(tmp_path, source, old)
    channel.directory.mkdir(parents=True)
    (channel.directory / "seg_00042.ts").write_bytes(b"held")

    async def scenario() -> None:
        # What the phone sending the stream again does.
        await channel.play(new, "https://page.example/")

    asyncio.run(scenario())
    source.close()
    assert puller.url == new and puller.referer == "https://page.example/"
    assert channel.source_url == new
    assert channel.history.sessions[-1]["source_url"] == new
    assert len(channel.history.sessions) == 1  # the same session, not a new one
    assert (channel.directory / "seg_00042.ts").read_bytes() == b"held"  # nothing cleared


def test_a_link_shaped_alike_on_another_clock_is_started_afresh(tmp_path) -> None:
    source = _Source(clock=50_000.0)  # a different channel's timeline
    old, new = source.url("OldTokenAbCdEfGhIjKl"), source.url("NewTokenMnOpQrStUvWx")
    channel, puller = _carrying(tmp_path, source, old)
    puller.expected = round(1008.0 * 90_000)

    async def scenario() -> None:
        assert not await channel._carry_on(new, "")

    asyncio.run(scenario())
    source.close()
    assert puller.url == old


def test_a_different_stream_is_not_even_fetched(tmp_path) -> None:
    source = _Source()
    old, new = source.url("OldTokenAbCdEfGhIjKl"), source.url("NewTokenMnOpQrStUvWx", "other")
    channel, _ = _carrying(tmp_path, source, old)

    async def scenario() -> None:
        assert not await channel._carry_on(new, "")

    asyncio.run(scenario())
    source.close()
    assert source.requests == []


def test_a_fresh_link_that_cannot_be_read_falls_back_to_a_restart(tmp_path) -> None:
    source = _Source()
    old, new = source.url("OldTokenAbCdEfGhIjKl"), source.url("NewTokenMnOpQrStUvWx")
    channel, puller = _carrying(tmp_path, source, old)
    source.refuse = True

    async def scenario() -> None:
        assert not await channel._carry_on(new, "")

    asyncio.run(scenario())
    source.close()
    assert puller.url == old
