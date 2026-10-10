"""Tests for how the channel records streams that start, end and replace each other."""

from __future__ import annotations

import asyncio

from airplay_relay.channel import Channel
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
