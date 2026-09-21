"""What the channel is doing, measured from the segments already held in RAM."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import socket
from typing import Any
from urllib.parse import urlparse

_LOGGER = logging.getLogger(__name__)

_PROBE_FIELDS = (
    "stream=codec_type,codec_name,width,height,r_frame_rate,channels:stream_tags=language,title"
)


def source_host(url: str | None) -> str | None:
    """Return just the host of the source, which is the part worth showing."""
    if not url:
        return None
    try:
        return urlparse(url).hostname
    except ValueError:
        return None


def window(directory: Path) -> tuple[int, float, int]:
    """Return how many segments are held, how many seconds, and how many bytes."""
    segments = sorted(directory.glob("seg_*.ts"))
    total = sum(segment.stat().st_size for segment in segments if segment.exists())
    seconds = _playlist_duration(directory)
    return len(segments), seconds, total


def dns_name(address: str) -> str:
    """Return the name this address is registered under, or nothing."""
    try:
        name, _, _ = socket.gethostbyaddr(address)
    except OSError:
        return ""
    # A bare address back means nothing is registered.
    return "" if name == address else name


def published(directory: Path) -> list[tuple[str, float]]:
    """Return the segments the playlist currently names, with their durations."""
    playlist = directory / "channel.m3u8"
    if not playlist.exists():
        return []
    entries: list[tuple[str, float]] = []
    seconds = 0.0
    for line in playlist.read_text(errors="replace").splitlines():
        text = line.strip()
        if text.startswith("#EXTINF:"):
            try:
                seconds = float(text.removeprefix("#EXTINF:").split(",")[0])
            except ValueError:
                seconds = 0.0
        elif text and not text.startswith("#"):
            entries.append((text, seconds))
            seconds = 0.0
    return entries


def _playlist_duration(directory: Path) -> float:
    """Return the duration the playlist says it holds."""
    playlist = directory / "channel.m3u8"
    if not playlist.exists():
        return 0.0
    total = 0.0
    for line in playlist.read_text(errors="replace").splitlines():
        if line.startswith("#EXTINF:"):
            value = line.removeprefix("#EXTINF:").split(",")[0]
            try:
                total += float(value)
            except ValueError:
                continue
    return total


async def probe(directory: Path) -> dict[str, Any]:
    """Return codec and resolution details, read from a segment we already hold."""
    segments = sorted(directory.glob("seg_*.ts"))
    if not segments:
        return {}
    # Not the newest: ffmpeg may still be writing it.
    target = segments[len(segments) // 2]

    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        _PROBE_FIELDS,
        "-of",
        "json",
        str(target),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate()
    try:
        streams = json.loads(stdout).get("streams", [])
    except ValueError:
        return {}

    details: dict[str, Any] = {}
    for stream in streams:
        if stream.get("codec_type") == "video" and "video_codec" not in details:
            details["video_codec"] = stream.get("codec_name")
            details["width"] = stream.get("width")
            details["height"] = stream.get("height")
            details["frame_rate"] = _rate(stream.get("r_frame_rate"))
        elif stream.get("codec_type") == "audio":
            if "audio_codec" not in details:
                details["audio_codec"] = stream.get("codec_name")
                details["audio_channels"] = stream.get("channels")
            tags = stream.get("tags") or {}
            # "und" means undetermined, so it is not shown as a track name.
            language = tags.get("language")
            details.setdefault("audio_tracks", []).append(
                tags.get("title") or (language if language and language != "und" else None)
            )
    return details


def _rate(value: str | None) -> float | None:
    """Turn ffprobe's "30000/1001" into a number."""
    if not value or "/" not in value:
        return None
    top, _, bottom = value.partition("/")
    try:
        return round(int(top) / int(bottom), 2) if int(bottom) else None
    except (ValueError, ZeroDivisionError):
        return None
