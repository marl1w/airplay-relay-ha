"""Tests for where the republished playlist tells players to start."""

from __future__ import annotations

import re

from airplay_relay.republish import START_SECONDS_BACK, Republisher


def _start_offset(directory) -> float:
    playlist = (directory / "channel.m3u8").read_text()
    return float(re.search(r"TIME-OFFSET=-([0-9.]+)", playlist).group(1))


def test_players_start_twenty_seconds_behind_the_live_edge(tmp_path) -> None:
    republisher = Republisher(tmp_path, "channel.m3u8", window=6)
    for _ in range(6):
        republisher.add(b"\x47" * 188, 6.006)

    assert START_SECONDS_BACK == 20.0
    assert _start_offset(tmp_path) == 20.0


def test_a_short_window_starts_players_within_what_is_held(tmp_path) -> None:
    republisher = Republisher(tmp_path, "channel.m3u8", window=6)
    for _ in range(3):
        republisher.add(b"\x47" * 188, 2.0)

    assert _start_offset(tmp_path) == 5.0


def test_the_window_counts_only_what_players_are_offered(tmp_path) -> None:
    from airplay_relay import status

    republisher = Republisher(tmp_path, "channel.m3u8", window=20)
    republisher.widen(12)
    for _ in range(5):
        republisher.add(b"\x47" * 1000, 6.0, now=0.0)

    segments, seconds, size = status.window(tmp_path)
    assert (segments, seconds, size) == (3, 18.0, 3000)
