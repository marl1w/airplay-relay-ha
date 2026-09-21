"""Render a standby card for the channel to loop while there is nothing to relay."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .logo import draw as draw_logo

_LOGGER = logging.getLogger(__name__)

# Small and slow: the card is a still image held in the same RAM as the live window.
WIDTH = 1280
HEIGHT = 720
FRAME_RATE = 5
SECONDS = 2.0
SEGMENTS = 3

# Silent audio rather than none: some players stop when an audio track appears later.
SAMPLE_RATE = 48000


async def render(directory: Path) -> list[bytes]:
    """Return a few seconds of standby card as TS segments, or an empty list on failure."""
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
        # A keyframe per segment, so a player joining mid-loop can decode.
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
