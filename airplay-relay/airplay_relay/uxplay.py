"""Run UxPlay behind the proxy, for the one step it alone can answer.

A sender requires the receiver to be Apple-licensed hardware at /fp-setup, and
UxPlay satisfies it. Nothing else about the session is its business: the proxy
keeps /play, so UxPlay is never told what to fetch and never fetches anything.
Its output is echoed to the log, where it is the only account of what the
FairPlay exchange did.

It stays a stock package, unpatched, so it can be updated without carrying a
fork.
"""

from __future__ import annotations

import asyncio
import logging

from .config import Config

_LOGGER = logging.getLogger(__name__)


def build_command(config: Config, hls: bool, debug: bool, extra: str) -> list[str]:
    """Return the UxPlay command line for the given settings."""
    # stdbuf because uxplay block-buffers stdout when it is not a terminal, so
    # its output -- including the reason it is about to give up -- never reaches
    # the log while it is running.
    command = ["stdbuf", "-oL", "-eL", "uxplay", "-n", config.name, "-vs", "0", "-as", "0"]
    if hls:
        command.append("-hls")
    if debug:
        command.append("-d")
    command.extend(extra.split())
    return command


async def run(command: list[str]) -> int:
    """Run UxPlay, echoing its output, and return the status it exits with."""
    _LOGGER.info("starting %s", " ".join(command))
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert process.stdout is not None

    async for raw in process.stdout:
        _LOGGER.info("uxplay | %s", raw.decode(errors="replace").rstrip())

    return await process.wait()
