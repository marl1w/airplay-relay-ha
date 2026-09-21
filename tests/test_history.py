"""What the record has to get right for the panel and for a restart.

The costly mistakes are both silent: forgetting a stream the add-on was carrying
when it was updated, so the house is left looking at the standby card, and
starting something again hours later that nobody asked for.
"""

from __future__ import annotations

import json
import time

from airplay_relay.history import KEEP_DAYS, RESUME_WITHIN_SECONDS, History


def test_a_stream_still_playing_when_the_add_on_stopped_is_resumed(tmp_path) -> None:
    history = History(tmp_path)
    history.start(source_url="https://example.com/live.m3u8")
    history.finish(history.sessions[-1], "interrupted")

    resuming = history.unfinished()
    assert resuming is not None
    assert resuming["source_url"] == "https://example.com/live.m3u8"


def test_a_stream_somebody_stopped_is_left_alone(tmp_path) -> None:
    history = History(tmp_path)
    history.start(source_url="https://example.com/live.m3u8")
    history.finish(history.sessions[-1], "stopped")

    assert history.unfinished() is None


def test_a_stream_that_ran_out_is_left_alone(tmp_path) -> None:
    history = History(tmp_path)
    history.start(source_url="https://example.com/live.m3u8")
    history.finish(history.sessions[-1], "finished")

    assert history.unfinished() is None


def test_an_old_interruption_is_not_resumed(tmp_path) -> None:
    history = History(tmp_path)
    session = history.start(source_url="https://example.com/live.m3u8")
    session["started"] = time.time() - RESUME_WITHIN_SECONDS - 60
    history.finish(session, "interrupted")
    session["ended"] = time.time() - RESUME_WITHIN_SECONDS - 60

    assert history.unfinished() is None


def test_a_crash_leaves_no_reason_and_is_still_resumed(tmp_path) -> None:
    """Nothing wrote an ending, because nothing got the chance to."""
    history = History(tmp_path)
    history.start(source_url="https://example.com/live.m3u8")
    history.sample(history.sessions[-1], {"viewers": 2, "kbps": 6000})

    reopened = History(tmp_path)
    assert reopened.unfinished() is not None


def test_the_record_survives_being_reopened(tmp_path) -> None:
    history = History(tmp_path)
    session = history.start(source_url="https://example.com/live.m3u8", name="Relay")
    history.sample(session, {"viewers": 3, "kbps": 6000})
    history.finish(session, "stopped")

    reopened = History(tmp_path)
    assert [s["id"] for s in reopened.recent()] == [session["id"]]
    assert reopened.recent()[0]["peak_viewers"] == 3


def test_streams_older_than_a_month_are_dropped(tmp_path) -> None:
    (tmp_path / "history.json").write_text(
        json.dumps(
            [
                {"id": "old", "started": time.time() - (KEEP_DAYS + 1) * 86400},
                {"id": "recent", "started": time.time() - 86400},
            ]
        ),
        encoding="utf-8",
    )
    assert [s["id"] for s in History(tmp_path).recent()] == ["recent"]


def test_running_figures_are_kept_as_the_stream_goes(tmp_path) -> None:
    history = History(tmp_path)
    session = history.start(source_url="https://example.com/live.m3u8")
    for viewers, kbps in ((1, 4000), (4, 6000), (2, 8000)):
        history._sampled = 0.0  # as if half a minute had passed
        history.sample(session, {"viewers": viewers, "kbps": kbps})

    assert session["peak_viewers"] == 4
    assert session["peak_kbps"] == 8000
    assert session["mean_kbps"] == 6000


def test_samples_are_not_taken_more_often_than_they_are_wanted(tmp_path) -> None:
    history = History(tmp_path)
    session = history.start(source_url="https://example.com/live.m3u8")
    for _ in range(5):
        history.sample(session, {"viewers": 1, "kbps": 4000})

    assert len(session["samples"]) == 1


def test_a_stream_can_be_found_again_to_replay(tmp_path) -> None:
    history = History(tmp_path)
    session = history.start(source_url="https://example.com/live.m3u8")

    assert history.find(session["id"])["source_url"] == "https://example.com/live.m3u8"
    assert history.find("nothing like it") is None
