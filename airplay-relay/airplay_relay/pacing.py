"""Choose the rendition the link can sustain, dropping quickly and climbing back slowly."""

from __future__ import annotations

from collections import deque

# Below 1.0 because segments arrive in lumps of a few seconds.
BEHIND_RATIO = 0.9
BEHIND_SECONDS = 45.0

# Stricter than dropping so the ladder does not oscillate between renditions.
STEADY_RATIO = 0.99
STEADY_SECONDS = 300.0

BARRED_SECONDS = 600.0

HISTORY_SECONDS = STEADY_SECONDS + 60.0

REMEMBER = 512


class Pace:
    """How much media the channel has published, against how much time passed."""

    def __init__(self) -> None:
        """Start with no history."""
        self._seen: deque[str] = deque(maxlen=REMEMBER)
        self._known: set[str] = set()
        self._produced = 0.0
        self._samples: deque[tuple[float, float]] = deque()

    def note(self, clock: float, entries: list[tuple[str, float]]) -> None:
        """Record segments not seen before, by name, as the window length is fixed."""
        for name, seconds in entries:
            if name in self._known:
                continue
            if len(self._seen) == self._seen.maxlen:
                self._known.discard(self._seen[0])
            self._seen.append(name)
            self._known.add(name)
            self._produced += seconds

        self._samples.append((clock, self._produced))
        while len(self._samples) > 1 and clock - self._samples[0][0] > HISTORY_SECONDS:
            self._samples.popleft()

    def ratio(self, clock: float, over: float) -> float | None:
        """Return media produced per second of real time, or None if too early."""
        oldest = next((sample for sample in self._samples if clock - sample[0] >= over), None)
        if oldest is None:
            return None
        elapsed = clock - oldest[0]
        if elapsed <= 0:
            return None
        return (self._produced - oldest[1]) / elapsed

    def reset(self) -> None:
        """Forget the pacing history so a new rendition is judged on its own."""
        self._samples.clear()


class Ladder:
    """The renditions a master playlist offers, and which one is in use."""

    def __init__(self, offered: list[tuple[int, str, str]]) -> None:
        """Order the renditions by bandwidth, and start at the top."""
        self.offered = offered
        # Playlist positions (what ffmpeg maps), sorted so that rung 0 is the best.
        self._rungs = sorted(range(len(offered)), key=lambda i: offered[i][0], reverse=True)
        self.rung = 0
        self._barred = 0
        self._barred_until = 0.0

    @property
    def variant(self) -> int:
        """Return the position of the rendition in use, for ffmpeg to map."""
        return self._rungs[self.rung]

    @property
    def bandwidth(self) -> int:
        """Return what the source claims the rendition in use needs."""
        return self.offered[self.variant][0]

    @property
    def name(self) -> str:
        """Return the resolution of the rendition in use."""
        return self.offered[self.variant][1]

    def down(self, clock: float) -> bool:
        """Drop a rendition, and bar the one being left. False if at the bottom."""
        if self.rung + 1 >= len(self._rungs):
            return False
        self.rung += 1
        self._barred = self.rung
        self._barred_until = clock + BARRED_SECONDS
        return True

    def up(self, clock: float) -> bool:
        """Climb a rendition, unless the one above is still barred."""
        if clock >= self._barred_until:
            self._barred = 0
        if self.rung <= self._barred:
            return False
        self.rung -= 1
        return True
