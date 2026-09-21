"""Draw the channel's icon.

An IPTV player shows whatever `tvg-logo` points at, and a channel without one
is a grey placeholder in the grid. Rather than ship a binary into the
repository, the icon is drawn here: a rounded tile with a play triangle, in the
few dozen lines it takes to write a PNG by hand.

No imaging library. PNG is a handful of length-prefixed chunks around zlib-
compressed scanlines, and the whole file is small enough to build in memory.
"""

from __future__ import annotations

from pathlib import Path
import struct
import zlib

SIZE = 320
RADIUS = 64

BACKGROUND = (24, 24, 29, 255)
TILE = (37, 99, 235, 255)
MARK = (255, 255, 255, 255)


def _chunk(kind: bytes, data: bytes) -> bytes:
    """Return one PNG chunk: length, type, payload, CRC."""
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _inside_rounded_square(x: int, y: int, size: int, radius: int) -> bool:
    """Whether a pixel falls inside a square with rounded corners."""
    near_x = min(x, size - 1 - x)
    near_y = min(y, size - 1 - y)
    if near_x >= radius or near_y >= radius:
        return True
    dx = radius - near_x
    dy = radius - near_y
    return dx * dx + dy * dy <= radius * radius


def _inside_triangle(x: int, y: int, size: int) -> bool:
    """Whether a pixel falls inside a centred play triangle."""
    left = size * 0.36
    right = size * 0.70
    top = size * 0.28
    bottom = size * 0.72
    if not left <= x <= right:
        return False
    # The triangle narrows linearly from the left edge to the point.
    progress = (x - left) / (right - left)
    half = (bottom - top) / 2 * (1 - progress)
    middle = (top + bottom) / 2
    return middle - half <= y <= middle + half


def draw(path: Path, size: int = SIZE) -> None:
    """Write the channel icon to the given path."""
    radius = max(1, round(RADIUS * size / SIZE))
    inset = round(size * 0.06)
    tile_size = size - inset * 2

    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            colour = BACKGROUND
            tx, ty = x - inset, y - inset
            on_tile = 0 <= tx < tile_size and 0 <= ty < tile_size
            if on_tile and _inside_rounded_square(tx, ty, tile_size, radius):
                colour = MARK if _inside_triangle(x, y, size) else TILE
            row += bytes(colour)
        # Each scanline is preceded by its filter type; 0 means "none".
        rows.append(b"\x00" + bytes(row))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + _chunk(b"IEND", b"")
    )
