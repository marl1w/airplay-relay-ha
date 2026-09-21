"""The rules that decide which rendition to pull, driven from both sides.

A wrong decision here is expensive and quiet: a rendition dropped for no reason
costs everyone picture quality, and one climbed back too eagerly costs everyone
a stutter every few minutes. Both are cheap to check without a network.
"""

from __future__ import annotations

from airplay_relay.pacing import BARRED_SECONDS, Ladder, Pace

# Bandwidth, resolution and audio group, as a master playlist lists them --
# deliberately out of order, which is how they arrive.
OFFERED = [
    (3608000, "1280x720", "program_audio"),
    (9328000, "1920x1080", "program_audio"),
    (1091200, "640x360", "program_audio"),
]


def segments(first: int, count: int, seconds: float = 4.0) -> list[tuple[str, float]]:
    return [(f"seg_{first + n:05d}.ts", seconds) for n in range(count)]


def test_a_stream_that_keeps_up_reads_as_one_second_per_second() -> None:
    pace = Pace()
    for step in range(30):
        pace.note(step * 4.0, segments(step, 6))
    assert pace.ratio(116.0, 45.0) == 1.0


def test_a_stream_falling_behind_reads_as_less() -> None:
    pace = Pace()
    for step in range(30):
        # Four seconds of media arriving every eight seconds of real time.
        pace.note(step * 8.0, segments(step, 6))
    assert pace.ratio(232.0, 45.0) == 0.5


def test_nothing_is_claimed_before_there_is_enough_history() -> None:
    pace = Pace()
    pace.note(0.0, segments(0, 6))
    pace.note(10.0, segments(1, 6))
    assert pace.ratio(10.0, 45.0) is None


def test_a_window_that_rolls_past_what_is_remembered_still_counts() -> None:
    pace = Pace()
    for step in range(400):
        pace.note(step * 4.0, segments(step, 6))
    assert pace.ratio(1596.0, 45.0) == 1.0


def test_the_best_rendition_is_taken_first() -> None:
    ladder = Ladder(OFFERED)
    assert ladder.name == "1920x1080"
    assert ladder.variant == 1


def test_dropping_goes_down_one_at_a_time_and_stops_at_the_bottom() -> None:
    ladder = Ladder(OFFERED)
    assert ladder.down(0.0) and ladder.name == "1280x720"
    assert ladder.down(0.0) and ladder.name == "640x360"
    assert not ladder.down(0.0)
    assert ladder.name == "640x360"


def test_a_rendition_that_failed_is_barred_for_a_while() -> None:
    ladder = Ladder(OFFERED)
    ladder.down(100.0)
    assert not ladder.up(100.0 + BARRED_SECONDS / 2)
    assert ladder.up(100.0 + BARRED_SECONDS + 1)
    assert ladder.name == "1920x1080"


def test_climbing_stops_at_the_top() -> None:
    ladder = Ladder(OFFERED)
    assert not ladder.up(0.0)
    assert ladder.name == "1920x1080"


def test_the_bar_follows_the_rendition_that_failed_most_recently() -> None:
    ladder = Ladder(OFFERED)
    ladder.down(0.0)
    ladder.down(0.0)
    assert ladder.name == "640x360"
    # The middle rung was the one that just failed, so even after the first
    # bar would have expired it is the ceiling that matters.
    assert not ladder.up(BARRED_SECONDS / 2)
    assert ladder.up(BARRED_SECONDS + 1)
    assert ladder.name == "1280x720"
