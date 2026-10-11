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

# The longest a smoothed feed waits to fill its reservoir before it begins anyway.
PRIME_SECONDS = 20.0


class Republisher:
    """Keeps a rolling HLS window on disk from segments handed to it.

    With a `depth`, arrivals wait in a reservoir and are listed at a steady pace,
    one segment per segment-length, so a source that delivers in bursts reaches
    players as an even stream, for as long as the reservoir lasts.
    """

    def __init__(self, directory: Path, playlist: str, window: int, start: int = 0) -> None:
        """Publish into this directory, keeping `window` segments, numbering from `start`."""
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
            if self._pending and (filled or now - self._waiting_since >= PRIME_SECONDS):
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
                self._list(entry)
                # Late after running dry, the schedule restarts from now.
                base = now if self._starved else self._next_release
                self._next_release = base + entry[1]
                self._starved = False
                changed = True
            if not self._pending and now >= self._next_release and not self._starved:
                # Players have been listed everything there is, and are about to run dry.
                self._starved = True
                self.underruns.append(now)
                _LOGGER.warning("the reservoir ran dry: no segment arrived in time")

        if changed:
            self._write_playlist()

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
