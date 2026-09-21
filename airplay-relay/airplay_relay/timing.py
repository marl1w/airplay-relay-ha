"""Relay UxPlay's NTP packets, which it sends to loopback behind the proxy, to the phone."""

from __future__ import annotations

import asyncio
import logging

_LOGGER = logging.getLogger(__name__)

LOOPBACK = "127.0.0.1"


class _FromUxPlay(asyncio.DatagramProtocol):
    """Receives on loopback what UxPlay believes it is sending to the phone."""

    def __init__(self, relay: TimingRelay) -> None:
        self._relay = relay

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._relay.from_uxplay(data, addr)


class _FromPhone(asyncio.DatagramProtocol):
    """Receives the phone's replies and hands them back to UxPlay."""

    def __init__(self, relay: TimingRelay) -> None:
        self._relay = relay

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._relay.from_phone(data, addr)


class TimingRelay:
    """The NTP path between UxPlay and one phone."""

    def __init__(self, client_address: str, timing_port: int) -> None:
        """Relay timing packets for one sender."""
        self.client_address = client_address
        self.timing_port = timing_port
        self._loopback: asyncio.DatagramTransport | None = None
        self._network: asyncio.DatagramTransport | None = None
        self._uxplay_address: tuple[str, int] | None = None
        self._forwarded = 0
        self._returned = 0

    async def start(self) -> None:
        """Open both sockets."""
        loop = asyncio.get_running_loop()
        # Two sockets: one bound to loopback cannot send to an address on the network.
        self._loopback, _ = await loop.create_datagram_endpoint(
            lambda: _FromUxPlay(self),
            local_addr=(LOOPBACK, self.timing_port),
        )
        self._network, _ = await loop.create_datagram_endpoint(
            lambda: _FromPhone(self),
            local_addr=("0.0.0.0", 0),
        )
        _LOGGER.info(
            "relaying timing on %s:%d to %s:%d",
            LOOPBACK,
            self.timing_port,
            self.client_address,
            self.timing_port,
        )

    def from_uxplay(self, data: bytes, addr: tuple[str, int]) -> None:
        """Send a packet UxPlay meant for the phone to the phone."""
        self._uxplay_address = addr
        if self._network is not None:
            self._network.sendto(data, (self.client_address, self.timing_port))
            self._forwarded += 1
            if self._forwarded == 1:
                _LOGGER.info("first timing packet forwarded to the phone")

    def from_phone(self, data: bytes, _addr: tuple[str, int]) -> None:
        """Return the phone's reply to UxPlay."""
        if self._loopback is not None and self._uxplay_address is not None:
            self._loopback.sendto(data, self._uxplay_address)
            self._returned += 1
            if self._returned == 1:
                _LOGGER.info("first timing reply returned to uxplay")

    def close(self) -> None:
        """Close both sockets."""
        for transport in (self._loopback, self._network):
            if transport is not None:
                transport.close()
        self._loopback = self._network = None
        _LOGGER.info("timing relay closed after %d out, %d back", self._forwarded, self._returned)
