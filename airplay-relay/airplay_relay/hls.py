"""Fetch an HLS stream directly, stripping any prefix ahead of the MPEG-TS payload."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import logging
import re
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

# A segment starting this far from where the last one ended is a jump, not jitter.
JUMP_SECONDS = 1.0

# A segment from no further back than this, and wholly sent already, is a repeat;
# further back, the source has restarted its clock and it is a jump.
REPEAT_SECONDS = 120.0

# MPEG-TS timestamps count 90kHz ticks in 33 bits, and wrap.
TICKS = 90_000
WRAP = 1 << 33

# Pauses before each further try at a segment that failed: a dropped connection
# or a CDN hiccup usually answers a second later, and a skipped segment is a jump.
SEGMENT_RETRIES = (1.0, 2.0)

# Seconds of a playlist that will not load before the link counts as dead.
REFUSED_SECONDS = 15.0


def first_pts(data: bytes) -> int | None:
    """Return the presentation time of the first timed packet, in 90kHz ticks."""
    for at in range(0, len(data) - PACKET + 1, PACKET):
        packet = data[at : at + PACKET]
        # A packet that starts a payload, and carries one.
        if packet[0] != SYNC or not packet[1] & 0x40 or not packet[3] & 0x10:
            continue
        start = 4 + (1 + packet[4] if packet[3] & 0x20 else 0)
        header = packet[start : start + 14]
        if len(header) < 14 or header[:3] != b"\x00\x00\x01" or not header[7] & 0x80:
            continue
        b = header[9:14]
        return ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1
    return None


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


def variant_uris(playlist: str, base: str) -> list[str]:
    """Return the playlist address of each rendition, in the order `variants` lists them."""
    lines = [line.strip() for line in playlist.splitlines()]
    found: list[str] = []
    for position, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        target = next((text for text in lines[position + 1 :] if text and text[0] != "#"), "")
        found.append(urljoin(base, target) if target else "")
    return found


def pullable(playlist: str) -> bool:
    """Whether a media playlist is a live one of plain segments, which can be pulled directly."""
    return "#EXTM3U" in playlist and not any(
        tag in playlist for tag in ("#EXT-X-KEY", "#EXT-X-MAP", "#EXT-X-ENDLIST")
    )


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


def refusal_advice(failure: str, seconds: float) -> str:
    """Say in plain words what a link that stopped loading means, and what to do."""
    if re.search(r"HTTP Error (401|403|410)", failure):
        return (
            f"the source has refused the link for {seconds:.0f}s ({failure}): it has been "
            "revoked, usually because the stream was opened on another device -- "
            "send it again from the phone and it carries on"
        )
    return (
        f"the source has not answered for {seconds:.0f}s ({failure}); still trying -- "
        "if it does not come back, send the stream again from the phone"
    )


class StrippingPuller:
    """Reads an HLS stream and writes plain MPEG-TS to whatever will take it."""

    def __init__(self, url: str, user_agent: str = "", referer: str = "") -> None:
        """Pull the playlist at this URL, identifying as the given client."""
        self.url = url
        self.user_agent = user_agent
        self.referer = referer
        self.lead = LEAD_SECONDS
        # Plain, one-at-a-time fetching unless told otherwise.
        self.parallel = False
        self._fetcher = Fetcher(user_agent, referer=referer)
        self.segments = 0
        self.stripped_bytes = 0
        # Where the next segment's timestamps should begin, if the source runs on unbroken.
        self.expected: int | None = None
        # Since when (loop clock) every playlist fetch has failed, and the latest reason.
        self.failing_since: float | None = None
        self.failure = ""
        self._said_refused = False
        self._seen: list[str] = []
        self._written = 0.0
        self._started = 0.0

    def rebind(self, url: str, referer: str) -> None:
        """Carry on from a fresh link to the same stream, keeping everything pulled so far.

        Segments already seen are recognised again by their timestamps, so whatever
        the new link lists twice is dropped and a gap it skips is marked.
        """
        self.url = url
        self.referer = referer
        self._fetcher = Fetcher(self.user_agent, referer=referer)
        self.failing_since = None
        self.failure = ""
        self._said_refused = False

    def refused_for(self, now: float) -> float:
        """Seconds the playlist has failed to load without a break, or 0."""
        return 0.0 if self.failing_since is None else now - self.failing_since

    def _note_failure(self, now: float, error: Exception) -> None:
        """Log a failed playlist fetch, and once, plainly, when the link looks dead."""
        if self.failing_since is None:
            self.failing_since = now
        self.failure = str(error)
        if self._said_refused:
            _LOGGER.debug("playlist fetch failed: %s", error)
            return
        _LOGGER.warning("playlist fetch failed: %s", error)
        if self.refused_for(now) >= REFUSED_SECONDS:
            self._said_refused = True
            _LOGGER.warning("%s", refusal_advice(self.failure, self.refused_for(now)))

    def _fetch(self, url: str) -> bytes:
        if self.parallel:
            return self._fetcher.get(url)
        return self._fetcher.get_plain(url)

    def _fetch_segment(self, url: str) -> bytes:
        if self.parallel:
            return self._fetcher.get_split(url)
        return self._fetcher.get_plain(url)

    async def run(self, write: Callable[[bytes, float, bool], object]) -> None:
        """Pass each new segment to `write` in order, until cancelled.

        Polling and downloading go on side by side, so a slow segment never delays
        the discovery of the next, and several transfers share the link at once.
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[asyncio.Future[bytes], float]] = asyncio.Queue()
        one_at_a_time = asyncio.Semaphore(1)
        several = asyncio.Semaphore(PARALLEL)

        async def download(segment: str) -> bytes:
            async with several if self.parallel else one_at_a_time:
                for pause in SEGMENT_RETRIES:
                    try:
                        return await loop.run_in_executor(None, self._fetch_segment, segment)
                    except Exception as error:
                        _LOGGER.info(
                            "segment fetch failed (%s); trying again in %.0fs", error, pause
                        )
                    await asyncio.sleep(pause)
                return await loop.run_in_executor(None, self._fetch_segment, segment)

        async def poll() -> None:
            wait = 4.0
            while True:
                began = loop.time()
                try:
                    raw = await loop.run_in_executor(None, self._fetch, self.url)
                except Exception as error:
                    self._note_failure(began, error)
                    await asyncio.sleep(wait)
                    continue
                if self.failing_since is not None:
                    _LOGGER.info(
                        "the source answers again after %.0fs", loop.time() - self.failing_since
                    )
                    self.failing_since = None
                    self._said_refused = False

                playlist = raw.decode("utf-8", "replace")
                wait = target_duration(playlist) / 3
                # Keyed on the path because the query string can change on every poll.
                for segment, seconds in parse(playlist, self.url):
                    key = segment.split("?")[0]
                    if key in self._seen:
                        continue
                    self._seen.append(key)
                    del self._seen[:-REMEMBER]
                    queue.put_nowait((asyncio.ensure_future(download(segment)), seconds))
                # The wait counts from the start of the poll, not from its end.
                await asyncio.sleep(max(0.5, wait - (loop.time() - began)))

        poller = asyncio.ensure_future(poll())
        missed = False
        try:
            while True:
                download_task, seconds = await queue.get()
                try:
                    data = await download_task
                except Exception as error:
                    _LOGGER.warning(
                        "segment fetch failed after %d tries, skipping it: %s",
                        len(SEGMENT_RETRIES) + 1,
                        error,
                    )
                    missed = True
                    continue

                offset = payload_offset(data)
                if offset and self.segments == 0:
                    _LOGGER.info("stripping %d bytes of prefix from each segment", offset)
                self.segments += 1
                self.stripped_bytes += offset
                if not self._started:
                    self._started = loop.time()
                payload = data[offset:]
                # A source can skip segments without saying so; players must be told.
                begins = first_pts(payload)
                if begins is not None and self.expected is not None:
                    step = (begins - self.expected + WRAP // 2) % WRAP - WRAP // 2
                    # Or list again, under new names, what it already sent: players would
                    # show it twice, so it is dropped here.
                    if (
                        -REPEAT_SECONDS * TICKS < step < -JUMP_SECONDS * TICKS
                        and step + seconds * TICKS <= JUMP_SECONDS * TICKS
                    ):
                        _LOGGER.warning(
                            "the source sent %.0fs of video again, from %.1fs back; "
                            "dropping the repeat",
                            seconds,
                            -step / TICKS,
                        )
                        continue
                    if abs(step) > JUMP_SECONDS * TICKS:
                        if not missed:
                            _LOGGER.warning(
                                "the source's timeline jumps by %+.1fs; marking a discontinuity",
                                step / TICKS,
                            )
                        missed = True
                self.expected = None if begins is None else (begins + round(seconds * TICKS)) % WRAP
                result = write(payload, seconds, missed)
                if asyncio.iscoroutine(result):
                    await result
                missed = False

                # Hold the feed near real time so the live window does not outrun players.
                self._written += seconds
                ahead = self._written - (loop.time() - self._started)
                if ahead > self.lead:
                    await asyncio.sleep(ahead - self.lead)
        finally:
            poller.cancel()
            while not queue.empty():
                queue.get_nowait()[0].cancel()
