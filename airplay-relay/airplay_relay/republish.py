"""Republish an HLS stream by passing its segments straight through.

Once the prefix is stripped, these segments are already valid MPEG-TS with
durations the source states. ffmpeg was only ever repackaging them, and every
problem that caused came from the repackaging rather than the video: a pipe
that stalls, a pacing clock that has to be guessed at, a process that exits and
takes the stream with it.

So this writes each segment to disk as it arrives and maintains the playlist
itself. The source's own segmentation and timing carry through untouched,
nothing is re-encoded or even remuxed, and the only failure left is a segment
that cannot be fetched -- which costs that segment and nothing else.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# Where a player should begin, as a distance back from the live edge. Without
# this each one picks for itself -- some start at the newest segment, some at
# the oldest -- so two televisions tuning in together can be half a minute
# apart, and the one that started furthest back can fall off the end of the
# window and stall. Roughly two and a half segments is the usual compromise
# between starting together and having something buffered.
START_SEGMENTS_BACK = 2.5


class Republisher:
    """Keeps a rolling HLS window on disk from segments handed to it."""

    def __init__(self, directory: Path, playlist: str, window: int, start: int = 0) -> None:
        """Publish into this directory, keeping `window` segments.

        `start` is where to begin numbering. A stream that replaces another has
        to carry on from where that one left off: a player already tuned in
        keeps the sequence numbers it has seen, and one that goes backwards --
        or a name it has already fetched -- is one it believes it has played,
        so it sits on the buffered remains of the old stream instead of taking
        the new one.
        """
        self.directory = directory
        self.playlist = playlist
        self.window = window
        self.sequence = start
        self._entries: list[tuple[str, float, bool]] = []
        self._written = start
        # Whatever came before was a different timeline, whether it ended on a
        # failure being retried or on someone choosing something else.
        self._pending_gap = start > 0

    def add(self, payload: bytes, duration: float, after_gap: bool = False) -> None:
        """Write one segment and bring the playlist up to date."""
        name = f"seg_{self._written:05d}.ts"
        (self.directory / name).write_bytes(payload)
        self._written += 1
        self._entries.append((name, duration, after_gap or self._pending_gap))
        self._pending_gap = False

        while len(self._entries) > self.window:
            stale, _, _ = self._entries.pop(0)
            (self.directory / stale).unlink(missing_ok=True)
            self.sequence += 1

        self._write_playlist()

    def _write_playlist(self) -> None:
        """Write the playlist describing the segments currently held.

        Written whole and moved into place, because a player that reads it
        halfway through an update sees a truncated list and stops.
        """
        longest = max((duration for _, duration, _ in self._entries), default=1.0)
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{max(1, math.ceil(longest))}",
            f"#EXT-X-MEDIA-SEQUENCE:{self.sequence}",
        ]
        held = sum(duration for _, duration, _ in self._entries)
        if held:
            # Negative means "from the end". Clamped so it never points before
            # the start of what we are still holding.
            offset = min(longest * START_SEGMENTS_BACK, max(0.0, held - longest / 2))
            lines.append(f"#EXT-X-START:TIME-OFFSET=-{offset:.3f},PRECISE=YES")
        for name, duration, after_gap in self._entries:
            if after_gap:
                # Tells the player the timeline jumps here, so it resets its
                # decoder instead of trying to reconcile the timestamps.
                lines.append("#EXT-X-DISCONTINUITY")
            lines.append(f"#EXTINF:{duration:.3f},")
            lines.append(name)

        target = self.directory / self.playlist
        temporary = target.with_suffix(".tmp")
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temporary.replace(target)

    def clear(self) -> None:
        """Remove every segment and the playlist."""
        for name, _, _ in self._entries:
            (self.directory / name).unlink(missing_ok=True)
        self._entries.clear()
        (self.directory / self.playlist).unlink(missing_ok=True)
