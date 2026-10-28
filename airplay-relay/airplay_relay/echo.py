"""Learn the Referer a sender's player uses, by having the sender fetch a URL from us.

Some sources answer only requests that carry the page embedding their player as
Referer. AirPlay never passes that page on, but a sender asked over the reverse
channel to fetch a URL does so as that page's player, Referer included. Pointed at
this server, the request tells us what the source wants to see.

iOS fetches media only over HTTPS with a certificate it trusts, so this serves the
box's own address under a wildcard-DNS domain whose certificate is published for
this use (`192-168-1-10.<domain>` resolves to 192.168.1.10). The certificate is
fetched again every day, so the domain's renewals are picked up without a restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
import secrets
import ssl
from urllib.request import urlopen

_LOGGER = logging.getLogger(__name__)

# How often the published certificate is fetched again.
REFRESH_SECONDS = 24 * 3600

# The body answered to the sender: an empty but valid playlist, so its player is not upset.
PLAYLIST = b"#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-ENDLIST\n"


def hostname(address: str, domain: str) -> str:
    """Return the name under the wildcard domain that resolves to this IPv4 address."""
    return f"{address.replace('.', '-')}.{domain}"


class Echo:
    """An HTTPS server that records the headers of each request it is sent."""

    def __init__(self, address: str, port: int, domain: str, directory: str) -> None:
        """Serve `address` as a name under `domain`, keeping the certificate in `directory`."""
        self.host = hostname(address, domain)
        self.port = port
        self.domain = domain
        self.directory = Path(directory)
        self._context: ssl.SSLContext | None = None
        self._server: asyncio.Server | None = None
        self._refresher: asyncio.Task | None = None
        self._waiting: dict[str, asyncio.Future[dict[str, str]]] = {}

    def url(self, token: str) -> str:
        """Return the address a sender is asked to fetch, carrying the token it answers to."""
        return f"https://{self.host}:{self.port}/{token}/master.m3u8"

    async def start(self) -> bool:
        """Fetch the certificate and start serving. False, with a warning, if that fails."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._fetch_certificate)
        if not self._load():
            _LOGGER.warning(
                "no certificate for *.%s; senders cannot be asked for their Referer", self.domain
            )
            return False
        try:
            self._server = await asyncio.start_server(
                self._answer, host="0.0.0.0", port=self.port, ssl=self._context
            )
        except OSError as error:
            _LOGGER.warning("echo cannot listen on %d: %s", self.port, error)
            return False
        self._refresher = asyncio.create_task(self._refresh())
        _LOGGER.info("asking senders for their Referer at https://%s:%d", self.host, self.port)
        return True

    async def learn(self, ask, wait_seconds: float = 4.0) -> dict[str, str]:
        """Have `ask(url)` send a sender to us and return the headers it arrived with.

        Returns nothing if the echo is not running or the sender never comes.
        """
        if self._server is None:
            return {}
        token = secrets.token_hex(8)
        waiting = asyncio.get_running_loop().create_future()
        self._waiting[token] = waiting
        try:
            if not await ask(self.url(token)):
                return {}
            return await asyncio.wait_for(waiting, wait_seconds)
        except TimeoutError:
            _LOGGER.info("the sender did not fetch the echo within %.0fs", wait_seconds)
            return {}
        finally:
            self._waiting.pop(token, None)

    async def close(self) -> None:
        """Stop serving."""
        if self._refresher is not None:
            self._refresher.cancel()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

    async def _answer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Record one request's headers against its token, and answer with an empty playlist."""
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            lines = head.decode("latin-1").split("\r\n")
            parts = lines[0].split(" ")
            token = parts[1].strip("/").split("/")[0] if len(parts) > 1 else ""
            headers: dict[str, str] = {}
            for line in lines[1:]:
                name, separator, value = line.partition(":")
                if separator:
                    headers[name.strip().lower()] = value.strip()
            if (waiting := self._waiting.get(token)) is not None and not waiting.done():
                waiting.set_result(headers)
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/vnd.apple.mpegurl\r\n"
                + f"Content-Length: {len(PLAYLIST)}\r\nConnection: close\r\n\r\n".encode()
                + PLAYLIST
            )
            await writer.drain()
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass
        finally:
            writer.close()

    def _fetch_certificate(self) -> None:
        """Download the domain's published certificate and key, keeping the old pair on failure."""
        self.directory.mkdir(parents=True, exist_ok=True)
        fetched = {}
        try:
            for name in ("server.pem", "server.key"):
                with urlopen(f"https://{self.domain}/{name}", timeout=20) as response:
                    fetched[name] = response.read()
            check = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            staged = {name: self.directory / f"{name}.new" for name in fetched}
            for name, body in fetched.items():
                staged[name].write_bytes(body)
            check.load_cert_chain(staged["server.pem"], staged["server.key"])
            for name, path in staged.items():
                path.replace(self.directory / name)
        except Exception as error:  # any failure keeps the pair we have
            _LOGGER.warning("could not fetch the certificate for *.%s: %s", self.domain, error)

    def _load(self) -> bool:
        """Put the stored pair into the server's context; new connections use it at once."""
        certificate, key = self.directory / "server.pem", self.directory / "server.key"
        if not certificate.exists() or not key.exists():
            return False
        try:
            if self._context is None:
                self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self._context.load_cert_chain(certificate, key)
        except (OSError, ssl.SSLError) as error:
            _LOGGER.warning("the stored certificate for *.%s is unusable: %s", self.domain, error)
            return False
        return True

    async def _refresh(self) -> None:
        """Fetch the certificate again every day, so its renewals arrive by themselves."""
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(REFRESH_SECONDS)
            await loop.run_in_executor(None, self._fetch_certificate)
            self._load()
