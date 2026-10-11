"""Fetch an HLS stream directly, stripping any prefix ahead of the MPEG-TS payload."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import logging
from urllib.parse import urljoin

from .fetch import Fetcher

_LOGGER = logging.getLogger(__name__)

PACKET = 188
SYNC = 0x47

# Consecutive aligned sync bytes required before a match counts as the start.
CONFIRM = 8

REMEMBER = 256

# Maximum seconds the feed may run ahead of real time.
LEAD_SECONDS = 12.0

# Segments fetched at once, so one slow transfer does not hold up the rest.
PARALLEL = 3


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


def _attributes(line: str, tag: str) -> dict[str, str]:
    """Return one playlist tag's attributes, splitting on commas outside quotes."""
    text = line.removeprefix(tag)
    parts: list[str] = []
    quoted = False
    current = ""
    for character in text:
        if character == '"':
            quoted = not quoted
        if character == "," and not quoted:
            parts.append(current)
            current = ""
            continue
        current += character
    parts.append(current)

    found: dict[str, str] = {}
    for part in parts:
        key, separator, value = part.partition("=")
        if separator:
            found[key.strip()] = value.strip().strip('"')
    return found


def variants(playlist: str) -> list[tuple[int, str, str]]:
    """Return (bandwidth, resolution, audio group) for each rendition, in playlist order."""
    found: list[tuple[int, str, str]] = []
    for line in playlist.splitlines():
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        attributes = _attributes(line, "#EXT-X-STREAM-INF:")
        bandwidth = 0
        with contextlib.suppress(ValueError):
            bandwidth = int(attributes.get("BANDWIDTH", "0"))
        found.append(
            (bandwidth, attributes.get("RESOLUTION") or "unknown", attributes.get("AUDIO", ""))
        )
    return found


def audio_renditions(playlist: str, group: str) -> list[tuple[str, str]]:
    """Return (language, name) for each rendition in one audio group, in playlist order."""
    found: list[tuple[str, str]] = []
    for line in playlist.splitlines():
        if not line.startswith("#EXT-X-MEDIA:"):
            continue
        attributes = _attributes(line, "#EXT-X-MEDIA:")
        if attributes.get("TYPE") != "AUDIO" or attributes.get("GROUP-ID") != group:
            continue
        found.append((attributes.get("LANGUAGE", ""), attributes.get("NAME", "")))
    return found


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
        self.lead = LEAD_SECONDS
        self._fetcher = Fetcher(user_agent)
        self.segments = 0
        self.stripped_bytes = 0
        self._seen: list[str] = []
        self._written = 0.0
        self._started = 0.0

    def _fetch(self, url: str) -> bytes:
        return self._fetcher.get(url)

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

            for segment, _ in fresh:
                self._seen.append(segment.split("?")[0])
            del self._seen[:-REMEMBER]

            # All started now, up to PARALLEL at a time, but written in playlist order.
            gate = asyncio.Semaphore(PARALLEL)

            async def download(segment: str, gate: asyncio.Semaphore = gate) -> bytes:
                async with gate:
                    return await loop.run_in_executor(None, self._fetch, segment)

            downloads = [asyncio.ensure_future(download(segment)) for segment, _ in fresh]
            try:
                for (_segment, seconds), download_task in zip(fresh, downloads, strict=True):
                    try:
                        data = await download_task
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
                        self._started = loop.time()
                    result = write(data[offset:], seconds, missed)
                    if asyncio.iscoroutine(result):
                        await result
                    missed = False

                    # Hold the feed near real time so the live window does not outrun players.
                    self._written += seconds
                    ahead = self._written - (loop.time() - self._started)
                    if ahead > self.lead:
                        await asyncio.sleep(ahead - self.lead)
            finally:
                for download_task in downloads:
                    download_task.cancel()

            await asyncio.sleep(wait)
