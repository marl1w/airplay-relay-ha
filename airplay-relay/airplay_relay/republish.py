"""Republish HLS segments as received, maintaining a rolling playlist on disk."""

from __future__ import annotations

from collections import deque
import logging
import math
from pathlib import Path
import time

_LOGGER = logging.getLogger(__name__)

# Seconds back from the live edge where players start: in step, and with a buffer.
START_SECONDS_BACK = 20.0

# Segments listed at once when a smoothed feed begins, as players need a few to start.
OPENING_SEGMENTS = 3

# The longest a smoothed feed waits to fill its reservoir before it begins anyway,
# unless it was asked to hold back more than this.
PRIME_SECONDS = 20.0

# How long a smoothed feed may run dry before the card fills in: about when players,
# which start this far back, run out. Until then they still have picture in hand.
FILL_AFTER_SECONDS = START_SECONDS_BACK


class Republisher:
    """Keeps a rolling HLS window on disk from segments handed to it.

    With a `depth`, arrivals wait in a reservoir and are listed at a steady pace,
    one segment per segment-length, so a source that delivers in bursts reaches
    players as an even stream, for as long as the reservoir lasts.
    """

    def __init__(
        self,
        directory: Path,
        playlist: str,
        window: int,
        start: int = 0,
        filler: list[bytes] | None = None,
        filler_seconds: float = 2.0,
    ) -> None:
        """Publish into this directory, keeping `window` segments, numbering from `start`.

        With `filler`, segments of `filler_seconds` each, a feed that runs dry carries
        them instead of standing still: a player left with a window that no longer
        moves reopens it and plays the same seconds over and over.
        """
        self.directory = directory
        self.playlist = playlist
        self.window = window
        self.sequence = start
        # Seconds of media held back to ride out a late source; 0 lists segments at once.
        self.depth = 0.0
        self.added = 0
        self.underruns: deque[float] = deque(maxlen=64)
        self._entries: list[tuple[str, float, bool]] = []
        self._pending: list[tuple[str, float, bool]] = []
        self._written = start
        # Segments numbered before `start` belong to a different timeline.
        self._pending_gap = start > 0
        self._playing = False
        self._starved = False
        self._waiting_since = 0.0
        self._next_release = 0.0
        self.filler = filler or []
        self.filler_seconds = filler_seconds
        self.filled = 0
        self._starved_at = 0.0
        self._filling = False
        self._next_fill = 0.0

    @property
    def reservoir(self) -> float:
        """Seconds of media received but not yet listed."""
        return sum(duration for _, duration, _ in self._pending)

    def add(
        self,
        payload: bytes,
        duration: float,
        after_gap: bool = False,
        now: float | None = None,
    ) -> None:
        """Write one segment, and list it now or when its turn comes."""
        name = f"seg_{self._written:05d}.ts"
        (self.directory / name).write_bytes(payload)
        self._written += 1
        self.added += 1
        if not self._pending and not self._playing:
            self._waiting_since = time.monotonic() if now is None else now
        self._pending.append((name, duration, after_gap or self._pending_gap))
        self._pending_gap = False
        self.release(now)

    def release(self, now: float | None = None) -> None:
        """List whatever segments are due. Called as segments arrive and on a timer."""
        now = time.monotonic() if now is None else now
        changed = False

        if self.depth <= 0:
            while self._pending:
                self._list(self._pending.pop(0))
                changed = True
        elif not self._playing:
            typical = max((duration for _, duration, _ in self._pending), default=0.0)
            filled = self.reservoir >= self.depth + OPENING_SEGMENTS * typical
            waited = now - self._waiting_since >= max(PRIME_SECONDS, self.depth)
            if self._pending and (filled or waited):
                self._playing = True
                last = 0.0
                for _ in range(min(OPENING_SEGMENTS, len(self._pending))):
                    entry = self._pending.pop(0)
                    self._list(entry)
                    last = entry[1]
                self._next_release = now + last
                changed = True
        else:
            while self._pending and now >= self._next_release:
                entry = self._pending.pop(0)
                if self._filling:
                    # Back from the card: a different picture, so players must reset.
                    entry = (entry[0], entry[1], True)
                    self._filling = False
                self._list(entry)
                # Late after running dry, the schedule restarts from now.
                base = now if self._starved else self._next_release
                self._next_release = base + entry[1]
                self._starved = False
                changed = True
            if not self._pending and now >= self._next_release and not self._starved:
                # Players have been listed everything there is, and are about to run dry.
                self._starved = True
                self._starved_at = now
                self.underruns.append(now)
                _LOGGER.warning("the reservoir ran dry: no segment arrived in time")
            if self._fill(now):
                changed = True

        if changed:
            self._write_playlist()

    def _fill(self, now: float) -> bool:
        """List the next card segment if the feed has been dry long enough. True if listed."""
        if not (self.filler and self._starved and not self._pending):
            return False
        if now - self._starved_at < FILL_AFTER_SECONDS or now < self._next_fill:
            return False
        if not self._filling:
            _LOGGER.warning(
                "nothing new for %.0fs; showing the card until the source is back",
                now - self._starved_at,
            )
        name = f"seg_{self._written:05d}.ts"
        (self.directory / name).write_bytes(self.filler[self.filled % len(self.filler)])
        self._written += 1
        self._list((name, self.filler_seconds, not self._filling))
        self._filling = True
        self.filled += 1
        self._next_fill = now + self.filler_seconds
        return True

    def widen(self, depth: float) -> None:
        """Hold back `depth` seconds of media, from here on."""
        gained = depth - self.depth
        self.depth = max(0.0, depth)
        # Pausing the listing for the difference fills the reservoir by that much.
        if self._playing and gained > 0:
            self._next_release += gained

    def _list(self, entry: tuple[str, float, bool]) -> None:
        """Put a segment on the playlist, and drop whatever has left the window."""
        self._entries.append(entry)
        while len(self._entries) > self.window:
            stale, _, _ = self._entries.pop(0)
            (self.directory / stale).unlink(missing_ok=True)
            self.sequence += 1

    def _write_playlist(self) -> None:
        """Write the playlist atomically so a player never reads a partial one."""
        longest = max((duration for _, duration, _ in self._entries), default=1.0)
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{max(1, math.ceil(longest))}",
            f"#EXT-X-MEDIA-SEQUENCE:{self.sequence}",
        ]
        held = sum(duration for _, duration, _ in self._entries)
        if held:
            # Clamped so it never points before the oldest segment still held.
            offset = min(START_SECONDS_BACK, max(0.0, held - longest / 2))
            lines.append(f"#EXT-X-START:TIME-OFFSET=-{offset:.3f},PRECISE=YES")
        for name, duration, after_gap in self._entries:
            if after_gap:
                lines.append("#EXT-X-DISCONTINUITY")
            lines.append(f"#EXTINF:{duration:.3f},")
            lines.append(name)

        target = self.directory / self.playlist
        temporary = target.with_suffix(".tmp")
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temporary.replace(target)

    def clear(self) -> None:
        """Remove every segment and the playlist."""
        for name, _, _ in [*self._entries, *self._pending]:
            (self.directory / name).unlink(missing_ok=True)
        self._entries.clear()
        self._pending.clear()
        (self.directory / self.playlist).unlink(missing_ok=True)
