"""Publish UxPlay's identity as our own, pointing at the proxy.

UxPlay has to register a service or it exits, but its record must not reach the
network: if a sender saw it, the session would go straight to UxPlay and past
everything this add-on wants to do. So UxPlay's avahi is confined to loopback,
and the record it publishes there is copied out here onto the real interface
with the proxy's port in place of UxPlay's.

Copying rather than inventing matters. The TXT record carries UxPlay's public
key and feature bits, and the sender pairs against those -- a record we made up
would advertise capabilities and an identity that the thing doing the pairing
does not have.

It also fixes something UxPlay gets wrong through a reflector: records published
here are withdrawn on exit, where UxPlay's linger as ghosts that collide with
the next run's name.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import socket
from types import TracebackType

from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from .config import SERVICE_TYPE, Config

_LOGGER = logging.getLogger(__name__)

LOOPBACK = "127.0.0.1"

# Often enough to stay inside a reflector's cache lifetime, rarely enough to be
# invisible on the network.
ANNOUNCE_EVERY = 60


async def read_upstream(wait_seconds: float = 30.0) -> tuple[int, dict[bytes, bytes | None]]:
    """Return the port and TXT record UxPlay published, read from avahi.

    The port is discovered rather than assumed. UxPlay's -p option sets a block
    of ports and does not necessarily advertise the first of them, and guessing
    wrong fails in a way that looks like avahi never started. Since its daemon
    is confined to loopback there is only ever one record here to find.
    """
    deadline = asyncio.get_running_loop().time() + wait_seconds
    last_seen = ""
    while asyncio.get_running_loop().time() < deadline:
        process = await asyncio.create_subprocess_exec(
            "avahi-browse",
            "-p",
            "-t",
            "-r",
            SERVICE_TYPE.removesuffix(".local."),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if stderr.strip():
            _LOGGER.warning("avahi-browse: %s", stderr.decode(errors="replace").strip()[:200])
        last_seen = stdout.decode(errors="replace").strip()
        for line in last_seen.splitlines():
            fields = line.split(";")
            if not line.startswith("=") or len(fields) < 10:
                continue
            port = int(fields[8])
            _LOGGER.info("uxplay registered %r on port %d", fields[3], port)
            return port, _parse_txt(fields[9])
        await asyncio.sleep(1)

    _LOGGER.error("avahi knows of no %s; it reported:\n%s", SERVICE_TYPE, last_seen or "(nothing)")
    raise TimeoutError("uxplay registered no service for the proxy to stand in front of")


def _parse_txt(field: str) -> dict[bytes, bytes | None]:
    """Turn avahi's quoted TXT field into the mapping zeroconf publishes."""
    properties: dict[bytes, bytes | None] = {}
    for entry in re.findall(r'"([^"]*)"', field):
        key, separator, value = entry.partition("=")
        properties[key.encode()] = value.encode() if separator else None
    return properties


class Mirror:
    """UxPlay's service record, republished on the real interface as ours."""

    def __init__(self, config: Config, properties: dict[bytes, bytes | None], port: int) -> None:
        """Build the record to publish, keeping UxPlay's own TXT contents."""
        self._config = config
        self._zeroconf: AsyncZeroconf | None = None
        self._announcer: asyncio.Task[None] | None = None
        self._info = ServiceInfo(
            SERVICE_TYPE,
            f"{config.name}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(config.address)],
            port=port,
            properties=properties,
            server=f"{config.avahi_hostname or config.name}.local.",
        )

    async def __aenter__(self) -> Mirror:
        """Publish the record."""
        self._zeroconf = AsyncZeroconf(interfaces=[self._config.address])
        await self._zeroconf.async_register_service(self._info)
        self._announcer = asyncio.create_task(self._keep_announcing())
        _LOGGER.info(
            "advertised %r at %s:%d", self._config.name, self._config.address, self._info.port
        )
        return self

    async def _keep_announcing(self) -> None:
        """Re-announce periodically so the record does not age out.

        Home Assistant and the televisions are on different VLANs, so what a
        phone sees is a copy held by the router's reflector. That copy expires,
        and a receiver that announced itself once simply disappears from the
        picker some minutes later while the add-on runs on none the wiser. This
        is why 'Relay is gone' kept happening with nothing wrong in the log.
        """
        while True:
            await asyncio.sleep(ANNOUNCE_EVERY)
            if self._zeroconf is None:
                return
            try:
                await self._zeroconf.async_update_service(self._info)
                _LOGGER.debug("re-announced %r", self._config.name)
            except Exception as error:
                _LOGGER.warning("could not re-announce: %s", error)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Withdraw the record, so the next run does not collide with it."""
        announcer, self._announcer = self._announcer, None
        if announcer is not None:
            announcer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await announcer
        if self._zeroconf is not None:
            await self._zeroconf.async_unregister_service(self._info)
            await self._zeroconf.async_close()
            self._zeroconf = None
            _LOGGER.info("withdrew %r", self._config.name)
