"""Decide which rendition of a source the link can actually carry.

A master playlist offers the same programme at several bitrates. The highest is
what anyone would choose, and for as long as the stream keeps up it is the right
answer. When it does not keep up the failure is not gentle: we fall behind the
live edge, the source drops the segments we have not fetched, and every screen
in the house stutters at once.

Falling behind is visible without measuring the network. A live stream produces
one second of media per second of real time, so if the window we publish gains
less than that, the pull is losing ground -- whatever the reason, which is the
point: a slow CDN, a saturated uplink and a busy box all show up the same way
and all have the same answer.

Climbing back is deliberately harder than dropping. A rendition that failed
once is likely to fail again, so the one we left is barred for a while: the cost
of staying a step low is a smaller picture, and the cost of oscillating is a
stutter every few minutes.
"""

from __future__ import annotations

from collections import deque

# How much of real time the window must gain to count as keeping up. Segments
# arrive in lumps of a few seconds, so a little under one is normal even on a
# healthy stream; well under it for the best part of a minute is not.
BEHIND_RATIO = 0.9
BEHIND_SECONDS = 45.0

# What it takes to earn a rendition back: near-perfect pacing sustained for long
# enough that it is a state rather than a lull.
STEADY_RATIO = 0.99
STEADY_SECONDS = 300.0

# How long a rendition stays barred after failing to keep up.
BARRED_SECONDS = 600.0

# Long enough to answer both questions above, and no longer.
HISTORY_SECONDS = STEADY_SECONDS + 60.0

# Segment names only have to be remembered until they leave the window; this is
# far more than a window ever holds.
REMEMBER = 512


class Pace:
    """How much media the channel has published, against how much time passed."""

    def __init__(self) -> None:
        """Start with no history, which reads as "not known yet"."""
        self._seen: deque[str] = deque(maxlen=REMEMBER)
        self._known: set[str] = set()
        self._produced = 0.0
        self._samples: deque[tuple[float, float]] = deque()

    def note(self, clock: float, entries: list[tuple[str, float]]) -> None:
        """Take in the playlist as it stands, and record what is new in it.

        Counted by name rather than by what the window holds, because the window
        is a fixed length: it says nothing about whether it is being refilled.
        """
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
        """Return media produced per second of real time, or None if too early.

        None means the question cannot be answered yet rather than that the
        answer is bad, and the two must not be confused: treating a stream that
        started twenty seconds ago as failing would drop a rendition before the
        first one had a chance.
        """
        oldest = next((sample for sample in self._samples if clock - sample[0] >= over), None)
        if oldest is None:
            return None
        elapsed = clock - oldest[0]
        if elapsed <= 0:
            return None
        return (self._produced - oldest[1]) / elapsed

    def reset(self) -> None:
        """Forget the history, keeping what has been published.

        Called when the rendition changes: what the last one managed says
        nothing about the next, and carrying it over would have the new stream
        judged on the old one's shortfall.
        """
        self._samples.clear()


class Ladder:
    """The renditions a master playlist offers, and which one is in use."""

    def __init__(self, offered: list[tuple[int, str, str]]) -> None:
        """Order the renditions by bandwidth, and start at the top."""
        self.offered = offered
        # Positions in the playlist's own order, which is what ffmpeg numbers
        # its programs by, sorted so that rung 0 is the best on offer.
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
