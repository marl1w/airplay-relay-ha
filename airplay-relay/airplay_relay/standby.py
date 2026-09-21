"""Keep something on the channel while there is nothing to relay.

A channel that publishes nothing between streams is one a player cannot be
pointed at in advance: you tune in, get an empty playlist, and the player gives
up -- so the television has to be woken after the phone rather than before it,
which is the wrong way round for a room full of people waiting.

So the channel always carries something. A few seconds of a still card are
rendered once, then published on a loop with the numbering the real stream will
carry on from, which makes the change from card to programme an ordinary
discontinuity in a stream the player is already following.

Rendered rather than shipped, for the same reason the icon is drawn rather than
committed: it is a still picture of a logo on a dark background, and generating
it costs a second of ffmpeg at startup instead of a binary in the repository.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .logo import draw as draw_logo

_LOGGER = logging.getLogger(__name__)

# Deliberately small and slow: nobody watches the card, and every byte of it
# sits in the same RAM the live window uses. Five frames a second of a still
# image costs almost nothing to encode and nothing at all to decode.
WIDTH = 1280
HEIGHT = 720
FRAME_RATE = 5
SECONDS = 2.0
SEGMENTS = 3

# Silence rather than no audio at all. A player handed a stream that gains an
# audio track partway through has to rebuild its pipeline, and some simply stop.
SAMPLE_RATE = 48000


async def render(directory: Path) -> list[bytes]:
    """Return a few seconds of standby card, as transport-stream segments.

    An empty list means it could not be made, which is not fatal: the channel
    then behaves as it did before, and carries nothing between streams.
    """
    logo = directory / "standby-logo.png"
    draw_logo(logo, size=360)
    pattern = directory / "standby_%03d.ts"
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-loop",
        "1",
        "-i",
        str(logo),
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r={SAMPLE_RATE}:cl=stereo",
        "-t",
        str(SECONDS * SEGMENTS),
        "-filter_complex",
        f"color=c=0x18181d:s={WIDTH}x{HEIGHT}:r={FRAME_RATE}[bg];"
        "[bg][0]overlay=(W-w)/2:(H-h)/2,format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        # A keyframe at the start of every segment, or a player joining the loop
        # partway has nothing to decode from.
        "-g",
        str(int(FRAME_RATE * SECONDS)),
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-f",
        "segment",
        "-segment_time",
        str(SECONDS),
        "-segment_format",
        "mpegts",
        str(pattern),
        "-y",
    ]

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, complaint = await process.communicate()
    logo.unlink(missing_ok=True)

    if process.returncode:
        _LOGGER.warning(
            "could not render the standby card, so the channel will carry nothing "
            "between streams: %s",
            complaint.decode(errors="replace").strip()[:200] or f"ffmpeg exit {process.returncode}",
        )
        return []

    made = sorted(directory.glob("standby_*.ts"))
    payloads = [segment.read_bytes() for segment in made]
    for segment in made:
        segment.unlink(missing_ok=True)
    if payloads:
        _LOGGER.info(
            "standby card ready: %d segments of %.0fs, %d kB",
            len(payloads),
            SECONDS,
            sum(len(payload) for payload in payloads) // 1024,
        )
    return payloads
