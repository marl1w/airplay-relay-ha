"""Tests for how the channel records streams that start, end and replace each other."""

from __future__ import annotations

import asyncio
import time

from airplay_relay.channel import IDLE_MINUTES, Channel
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


def test_a_stream_nobody_has_watched_for_a_while_still_stops(tmp_path) -> None:
    async def scenario() -> None:
        channel = _channel(tmp_path)
        drop = _drops_once(channel)

        await channel.play("https://example.com/live.m3u8")
        channel._started_at = time.monotonic() - (IDLE_MINUTES + 1) * 60
        drop.set()
        await asyncio.sleep(0.1)

        assert not channel.requested
        assert channel.history.sessions[-1]["reason"] == "nobody watching"

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
