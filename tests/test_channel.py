"""Tests for how the channel records streams that start, end and replace each other."""

from __future__ import annotations

import asyncio
import time

from airplay_relay import channel as channel_module
from airplay_relay.channel import IDLE_MINUTES, WAKE_MINUTES, Channel
from airplay_relay.history import History


def _channel(tmp_path) -> Channel:
    channel = Channel(
        directory=str(tmp_path / "channel"),
        port=0,
        hls_time=2,
        hls_list_size=6,
        history=History(tmp_path / "state"),
    )
    channel._inspect = lambda url: None

    async def attempt(url: str) -> int:
        await asyncio.Event().wait()
        return 0

    channel._attempt = attempt
    return channel


def test_a_replaced_stream_is_recorded_as_ended(tmp_path) -> None:
    async def scenario() -> None:
        channel = _channel(tmp_path)
        await channel.play("https://example.com/first.m3u8")
        await channel.play("https://example.com/second.m3u8")

        first, second = channel.history.sessions
        assert first["reason"] == "replaced"
        assert first["ended"] is not None
        assert second["ended"] is None

        await channel.stop()
        assert second["reason"] == "stopped"

    asyncio.run(scenario())


def _drops_once(channel: Channel) -> asyncio.Event:
    """Have the source drop once, when the returned event is set."""
    drop = asyncio.Event()
    calls = 0

    async def attempt(url: str) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            await drop.wait()
            return 1
        await asyncio.Event().wait()
        return 0

    channel._attempt = attempt
    return drop


def test_a_new_stream_is_not_judged_on_who_watched_the_last_one(tmp_path) -> None:
    async def scenario() -> None:
        channel = _channel(tmp_path)
        drop = _drops_once(channel)
        channel._last_viewer = time.monotonic() - 3 * 3600

        await channel.play("https://example.com/live.m3u8")
        drop.set()
        await asyncio.sleep(0.1)

        assert channel.requested
        assert channel.history.sessions[-1]["reason"] is None
        await channel.stop()

    asyncio.run(scenario())


def test_a_new_stream_nobody_watches_stops_after_the_countdown(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(channel_module, "SAMPLE_SECONDS", 0.01)

    async def scenario() -> None:
        channel = _channel(tmp_path)
        await channel.play("https://example.com/live.m3u8")
        assert IDLE_MINUTES * 60 - 1 < channel.stops_in <= IDLE_MINUTES * 60

        channel._started_at = time.monotonic() - (IDLE_MINUTES * 60 + 1)
        await asyncio.sleep(0.1)

        assert not channel.requested
        assert channel.history.sessions[-1]["reason"] == "nobody watching"

    asyncio.run(scenario())


def test_the_countdown_starts_when_the_last_viewer_leaves(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(channel_module, "SAMPLE_SECONDS", 0.01)

    async def scenario() -> None:
        channel = _channel(tmp_path)
        await channel.play("https://example.com/live.m3u8")
        now = time.monotonic()
        channel._started_at = now - 3600
        # The last player left 14 minutes ago; the count hit zero 30 seconds later.
        channel._last_viewer = now - (IDLE_MINUTES - 1) * 60
        await asyncio.sleep(0.1)

        assert channel.requested
        assert 85 <= channel.stops_in <= 90

        channel._last_viewer = now - (IDLE_MINUTES + 1) * 60
        await asyncio.sleep(0.1)
        assert not channel.requested

    asyncio.run(scenario())


def test_someone_watching_cancels_the_countdown(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(channel_module, "SAMPLE_SECONDS", 0.01)

    async def scenario() -> None:
        channel = _channel(tmp_path)
        await channel.play("https://example.com/live.m3u8")
        channel._started_at = time.monotonic() - 3600
        channel.note_viewer("192.0.2.10")
        await asyncio.sleep(0.1)

        assert channel.requested
        assert channel.stops_in is None
        await channel.stop()

    asyncio.run(scenario())


def test_a_stream_replaced_by_itself_is_still_resumed_after_a_restart(tmp_path) -> None:
    async def scenario() -> None:
        channel = _channel(tmp_path)
        await channel.play("https://example.com/live.m3u8")
        await channel.play("https://example.com/live.m3u8")
        await channel.stop("interrupted")

        resuming = channel.history.unfinished()
        assert resuming is not None
        assert resuming["source_url"] == "https://example.com/live.m3u8"

    asyncio.run(scenario())


async def _dozed(channel: Channel, url: str) -> None:
    """Play a stream and let the countdown stop it."""
    channel._loop = asyncio.get_running_loop()
    await channel.play(url)
    channel._started_at = time.monotonic() - (IDLE_MINUTES * 60 + 1)
    await asyncio.sleep(0.1)
    assert not channel.requested


def test_a_player_tuning_in_restarts_a_stream_stopped_for_nobody_watching(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(channel_module, "SAMPLE_SECONDS", 0.01)

    async def scenario() -> None:
        channel = _channel(tmp_path)
        await _dozed(channel, "https://example.com/live.m3u8")
        assert WAKE_MINUTES * 60 - 5 < channel.wakes_for <= WAKE_MINUTES * 60

        channel.wake()
        await asyncio.sleep(0.05)

        assert channel.source_url == "https://example.com/live.m3u8"
        assert channel.wakes_for is None
        stopped, woken = channel.history.sessions
        assert stopped["reason"] == "nobody watching"
        assert woken["ended"] is None
        await channel.stop()

    asyncio.run(scenario())


def test_a_stream_stopped_over_an_hour_ago_stays_stopped(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(channel_module, "SAMPLE_SECONDS", 0.01)

    async def scenario() -> None:
        channel = _channel(tmp_path)
        await _dozed(channel, "https://example.com/live.m3u8")
        url, _ = channel._dozing
        channel._dozing = (url, time.monotonic() - (WAKE_MINUTES * 60 + 1))
        assert channel.wakes_for is None

        channel.wake()
        await asyncio.sleep(0.05)
        assert not channel.requested

    asyncio.run(scenario())


def test_a_stream_stopped_from_the_panel_does_not_wake(tmp_path) -> None:
    async def scenario() -> None:
        channel = _channel(tmp_path)
        channel._loop = asyncio.get_running_loop()
        await channel.play("https://example.com/live.m3u8")
        await channel.stop()

        assert channel.wakes_for is None
        channel.wake()
        await asyncio.sleep(0.05)
        assert not channel.requested

    asyncio.run(scenario())


def test_numbering_keeps_moving_forward_across_a_restart(tmp_path, monkeypatch) -> None:
    now = 1_791_650_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    before = _channel(tmp_path / "before")
    first = before._numbering_starts_at()
    # Ten minutes of 6-second segments, as fast as a live stream can produce them.
    last = first + 100
    before.directory.mkdir(parents=True)
    (before.directory / f"seg_{last:05d}.ts").write_bytes(b"")

    now += 600 + 5
    after = _channel(tmp_path / "after")
    after.directory.mkdir(parents=True)
    assert after._numbering_starts_at() > last


def test_the_snapshot_says_how_the_stream_is_fetched(tmp_path) -> None:
    channel = Channel(str(tmp_path), 0, 2, 6)
    assert channel.fetching == "ffmpeg"
    assert channel.snapshot()["extra_delay_seconds"] == 0
    channel._pulling = True
    channel._cushion = 30.0
    assert channel.fetching == "parallel"
    assert channel.snapshot()["extra_delay_seconds"] == 30


def test_a_slow_read_alone_raises_no_alarm(tmp_path) -> None:
    channel = Channel(str(tmp_path), 0, 2, 6)
    channel.note_transfer("10.0.0.5", 6_000_000, 12.0, True)

    assert channel.slow_player(time.monotonic() + 1, 8000) is None


def test_a_dropped_download_is_reported_even_when_fast(tmp_path) -> None:
    channel = Channel(str(tmp_path), 0, 2, 6)
    channel.note_transfer("10.0.0.5", 3_000_000, 0.5, False)

    assert channel.slow_player(time.monotonic() + 1, 8000)[2] is True


def test_healthy_players_raise_no_alarm(tmp_path) -> None:
    channel = Channel(str(tmp_path), 0, 2, 6)
    channel.note_transfer("10.0.0.5", 6_000_000, 1.0, True)

    assert channel.slow_player(time.monotonic() + 1, 8000) is None


def test_a_cpu_starved_box_is_named_as_the_cause(tmp_path, monkeypatch) -> None:
    channel = Channel(str(tmp_path), 0, 2, 6)
    monkeypatch.setattr(channel_module.status, "cpu_pressure", lambda: (25.0, 12.0))
    assert channel._starved_of_cpu() == (25.0, 12.0)

    monkeypatch.setattr(channel_module.status, "cpu_pressure", lambda: (2.0, 4.0))
    assert channel._starved_of_cpu() is None

    monkeypatch.setattr(channel_module.status, "cpu_pressure", lambda: None)
    assert channel._starved_of_cpu() is None


def _judged(tmp_path, monkeypatch, pressure, keeping):
    channel = Channel(str(tmp_path), 0, 2, 6)
    monkeypatch.setattr(Channel, "playing", property(lambda self: True))
    monkeypatch.setattr(channel_module.status, "cpu_pressure", lambda: pressure)
    monkeypatch.setattr(channel._pace, "ratio", lambda clock, over: keeping)
    channel._stall = lambda clock: None
    channel.slow_player = lambda clock, bitrate: None
    return channel.quality()


def test_cpu_pressure_alone_is_no_verdict(tmp_path, monkeypatch) -> None:
    verdict = _judged(tmp_path, monkeypatch, (15.0, 14.0), 1.0)

    assert verdict["state"] == "good"
    assert "cpu_pressure" not in verdict


def test_cpu_pressure_is_named_when_the_stream_is_late(tmp_path, monkeypatch) -> None:
    verdict = _judged(tmp_path, monkeypatch, (25.0, 12.0), 0.5)

    assert verdict["state"] == "poor"
    assert "the link is behind" in verdict["because"]
    assert "short of CPU (25%" in verdict["because"]
    assert verdict["cpu_pressure"] == 25.0


def test_a_late_stream_on_an_unloaded_box_does_not_blame_the_cpu(tmp_path, monkeypatch) -> None:
    verdict = _judged(tmp_path, monkeypatch, (2.0, 3.0), 0.5)

    assert verdict["state"] == "poor"
    assert "CPU" not in verdict["because"]


def test_a_rendition_hiding_mpeg_ts_behind_a_prefix_is_stripped_here(tmp_path) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    image = b"RIFF\x00\x10\x00\x00WEBPVP8 " + b"\x00" * 2000
    segment = image + (b"\x47" + b"\x00" * 187) * 16

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/master.m3u8":
                body = (
                    b"#EXTM3U\n"
                    b'#EXT-X-STREAM-INF:BANDWIDTH=2820000,RESOLUTION=1920x1080,CODECS="avc1,mp4a"\n'
                    b"tracks/mono.m3u8\n"
                )
            elif self.path == "/tracks/mono.m3u8":
                body = b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\n1.ts\n"
            else:
                body = segment
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        channel = Channel(directory=str(tmp_path / "channel"), port=0, hls_time=2, hls_list_size=6)
        base = f"http://127.0.0.1:{server.server_port}"
        channel._inspect(f"{base}/master.m3u8")
    finally:
        server.shutdown()

    assert channel._needs_stripping
    assert channel._pull_urls == [f"{base}/tracks/mono.m3u8"]
