"""Republish HLS segments as received, maintaining a rolling playlist on disk."""

from __future__ import annotations

import logging
import math
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# Seconds back from the live edge where players start: in step, and with a buffer.
START_SECONDS_BACK = 20.0


class Republisher:
    """Keeps a rolling HLS window on disk from segments handed to it."""

    def __init__(self, directory: Path, playlist: str, window: int, start: int = 0) -> None:
        """Publish into this directory, keeping `window` segments, numbering from `start`."""
        self.directory = directory
        self.playlist = playlist
        self.window = window
        self.sequence = start
        self._entries: list[tuple[str, float, bool]] = []
        self._written = start
        # Segments numbered before `start` belong to a different timeline.
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
        for name, _, _ in self._entries:
            (self.directory / name).unlink(missing_ok=True)
        self._entries.clear()
        (self.directory / self.playlist).unlink(missing_ok=True)
