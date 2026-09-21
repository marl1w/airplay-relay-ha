"""Run UxPlay behind the proxy, for the FairPlay step it alone can answer."""

from __future__ import annotations

import asyncio
import logging

from .config import Config

_LOGGER = logging.getLogger(__name__)


def build_command(config: Config, hls: bool, debug: bool, extra: str) -> list[str]:
    """Return the UxPlay command line for the given settings."""
    # stdbuf: uxplay block-buffers stdout when piped, which holds its output back.
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
