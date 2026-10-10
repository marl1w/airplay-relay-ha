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
