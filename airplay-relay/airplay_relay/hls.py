"""Fetch an HLS stream directly, stripping any prefix ahead of the MPEG-TS payload."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from urllib.parse import urljoin
from urllib.request import Request, urlopen

_LOGGER = logging.getLogger(__name__)

PACKET = 188
SYNC = 0x47

# Consecutive aligned sync bytes required before a match counts as the start.
CONFIRM = 8

REMEMBER = 256

# Maximum seconds the feed may run ahead of real time.
LEAD_SECONDS = 12.0


def payload_offset(data: bytes) -> int:
    """Return where the transport stream starts, skipping any prefix."""
    limit = min(len(data), 1_000_000)
    for start in range(limit):
        if data[start] != SYNC:
            continue
        if all(
            data[start + n * PACKET] == SYNC
            for n in range(1, CONFIRM)
            if start + n * PACKET < len(data)
        ):
            return start
    return 0


def parse(playlist: str, base: str) -> list[tuple[str, float]]:
    """Return every segment the playlist lists, with how long it runs for."""
    segments: list[tuple[str, float]] = []
    duration = 0.0
    for line in playlist.splitlines():
        text = line.strip()
        if text.startswith("#EXTINF:"):
            try:
                duration = float(text.removeprefix("#EXTINF:").split(",")[0])
            except ValueError:
                duration = 0.0
        elif text and not text.startswith("#"):
            segments.append((urljoin(base, text), duration))
            duration = 0.0
    return segments


def target_duration(playlist: str, default: float = 4.0) -> float:
    """Return how long the playlist says its segments are."""
    for line in playlist.splitlines():
        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                return max(1.0, float(line.split(":", 1)[1]))
            except ValueError:
                break
    return default


class StrippingPuller:
    """Reads an HLS stream and writes plain MPEG-TS to whatever will take it."""

    def __init__(self, url: str, user_agent: str = "") -> None:
        """Pull the playlist at this URL, identifying as the given client."""
        self.url = url
        self.user_agent = user_agent
        self.segments = 0
        self.stripped_bytes = 0
        self._seen: list[str] = []
        self._written = 0.0
        self._started = 0.0

    def _fetch(self, url: str) -> bytes:
        headers = {"User-Agent": self.user_agent} if self.user_agent else {}
        with urlopen(Request(url, headers=headers), timeout=20) as response:
            return response.read()

    async def run(self, write: Callable[[bytes, float, bool], object]) -> None:
        """Poll the playlist and pass each new segment to `write`, until cancelled."""
        loop = asyncio.get_running_loop()
        wait = 4.0
        missed = False
        while True:
            try:
                raw = await loop.run_in_executor(None, self._fetch, self.url)
            except Exception as error:
                _LOGGER.warning("playlist fetch failed: %s", error)
                await asyncio.sleep(wait)
                continue

            playlist = raw.decode("utf-8", "replace")
            wait = target_duration(playlist) / 2
            # Keyed on the path because the query string can change on every poll.
            fresh = [
                (url, seconds)
                for url, seconds in parse(playlist, self.url)
                if url.split("?")[0] not in self._seen
            ]

            for segment, seconds in fresh:
                self._seen.append(segment.split("?")[0])
                del self._seen[:-REMEMBER]
                try:
                    data = await loop.run_in_executor(None, self._fetch, segment)
                except Exception as error:
                    _LOGGER.warning("segment fetch failed: %s", error)
                    missed = True
                    continue

                offset = payload_offset(data)
                if offset and self.segments == 0:
                    _LOGGER.info("stripping %d bytes of prefix from each segment", offset)
                self.segments += 1
                self.stripped_bytes += offset
                if not self._started:
                    self._started = asyncio.get_running_loop().time()
                result = write(data[offset:], seconds, missed)
                if asyncio.iscoroutine(result):
                    await result
                missed = False

                # Hold the feed near real time so the live window does not outrun players.
                self._written += seconds
                loop_now = asyncio.get_running_loop().time()
                ahead = self._written - (loop_now - self._started)
                if ahead > LEAD_SECONDS:
                    await asyncio.sleep(ahead - LEAD_SECONDS)

            await asyncio.sleep(wait)
