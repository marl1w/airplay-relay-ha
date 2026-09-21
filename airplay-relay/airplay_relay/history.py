"""Remember what has been played, and how well it went.

Two things want this. The panel, so that a month of streams can be looked back
over rather than vanishing the moment they end; and the add-on itself, so that a
restart -- an update, a crash, the box rebooting -- can pick up the stream it was
carrying instead of leaving the house staring at the standby card.

Both come from the same record, because they are the same question asked at
different times: what was playing, and was it still playing when we stopped?

Written to the add-on's own durable volume, which is small, so a stream is
sampled every half minute rather than continuously: enough to draw the shape of
an evening, far short of what it would take to fill anything up.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
import time
from typing import Any
import uuid

_LOGGER = logging.getLogger(__name__)

FILENAME = "history.json"

# How much to keep. A month is what the panel offers to look back over.
KEEP_DAYS = 30

# How often a playing stream adds a point to its own series.
SAMPLE_SECONDS = 30.0

# A stream interrupted longer ago than this is not worth resuming: whatever was
# on is over, and starting it again would be a surprise rather than a recovery.
RESUME_WITHIN_SECONDS = 15 * 60


class History:
    """Every stream of the last month, and the one playing now."""

    def __init__(self, directory: Path) -> None:
        """Keep the record in this directory, reading back what is already there."""
        self.path = directory / FILENAME
        self.sessions: list[dict[str, Any]] = self._read()
        self._sampled = 0.0

    def _read(self) -> list[dict[str, Any]]:
        """Return what was stored, dropping anything too old to show."""
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(stored, list):
            return []
        cutoff = time.time() - KEEP_DAYS * 86400
        return [
            session
            for session in stored
            if isinstance(session, dict) and session.get("started", 0) >= cutoff
        ]

    def _write(self) -> None:
        """Store the record, whole, and leave the old one alone if it fails.

        The volume this lives on is shared with backups and the recorder, and a
        write that fails is not a reason to bring the stream down -- so it is
        logged once per failure and otherwise ignored.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.sessions), encoding="utf-8")
            temporary.replace(self.path)
        except OSError as error:
            _LOGGER.warning("could not store the history: %s", error)

    def start(self, **details: Any) -> dict[str, Any]:
        """Record that a stream has begun, and return it for updating."""
        session = {
            "id": uuid.uuid4().hex[:12],
            "started": time.time(),
            "ended": None,
            "reason": None,
            "samples": [],
            **details,
        }
        self.sessions.append(session)
        del self.sessions[:-500]
        self._sampled = 0.0
        self._write()
        return session

    def sample(self, session: dict[str, Any] | None, point: dict[str, Any]) -> None:
        """Add a point to the stream's series, at most every half minute."""
        if session is None:
            return
        now = time.monotonic()
        if self._sampled and now - self._sampled < SAMPLE_SECONDS:
            return
        self._sampled = now
        session["samples"].append({"at": round(time.time()), **point})
        # Fold the running figures in as they are taken, so a stream cut short
        # by a crash still has them.
        session["peak_viewers"] = max(session.get("peak_viewers", 0), point.get("viewers", 0))
        if rate := point.get("kbps"):
            rates = [s["kbps"] for s in session["samples"] if s.get("kbps")]
            session["mean_kbps"] = round(sum(rates) / len(rates))
            session["peak_kbps"] = max(session.get("peak_kbps", 0), rate)
        self._write()

    def describe(self, session: dict[str, Any] | None, **details: Any) -> None:
        """Fold in what has been learned about the stream since it started."""
        if session is None:
            return
        session.update({key: value for key, value in details.items() if value is not None})
        self._write()

    def finish(self, session: dict[str, Any] | None, reason: str) -> None:
        """Record that a stream has ended, and why.

        The reason is what makes the record answer the resume question: a stream
        the add-on was still carrying when it went down reads differently from
        one somebody chose to end.
        """
        if session is None or session.get("ended"):
            return
        session["ended"] = time.time()
        session["reason"] = reason
        self._write()

    def unfinished(self) -> dict[str, Any] | None:
        """Return a stream that was still playing when the add-on last stopped.

        Nothing means there is nothing to resume: the last stream was stopped on
        purpose, ran out, or ended too long ago to still be wanted.
        """
        if not self.sessions:
            return None
        last = self.sessions[-1]
        if last.get("reason") not in (None, "interrupted"):
            return None
        if not last.get("source_url"):
            return None
        last_seen = last.get("ended") or last.get("started", 0)
        if last["samples"]:
            last_seen = max(last_seen, last["samples"][-1]["at"])
        if time.time() - last_seen > RESUME_WITHIN_SECONDS:
            return None
        return last

    def find(self, identifier: str) -> dict[str, Any] | None:
        """Return one stream by its identifier."""
        return next((s for s in self.sessions if s.get("id") == identifier), None)

    def recent(self) -> list[dict[str, Any]]:
        """Return every stream of the last month, newest first."""
        return sorted(self.sessions, key=lambda s: s.get("started", 0), reverse=True)


def open_in(preferred: str, fallback: str) -> History:
    """Return the history stored in the add-on's volume, or beside the segments.

    /data is the add-on's own durable directory and does not exist anywhere
    else, so running from a terminal keeps its record with the stream instead of
    failing over something that is not the point.
    """
    directory = Path(preferred)
    with contextlib.suppress(OSError):
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".writable"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return History(directory)
    return History(Path(fallback))
