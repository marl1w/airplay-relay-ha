"""Sit in front of UxPlay and answer the requests we care about ourselves.

UxPlay is kept for one reason: it satisfies /fp-setup, the step where a sender
requires the receiver to be Apple-licensed hardware, which nothing we write can
do. Everything else is better handled here, where the answers can reflect what
this add-on is actually doing.

So the sender talks to this proxy. Pairing, FairPlay and anything unrecognised
are forwarded to UxPlay untouched and its replies passed straight back. The
handful of requests that describe playback are answered from our own channel
instead, which is what lets the phone be told the truth about a stream UxPlay
is not involved in.

The control channel is plain HTTP even after pairing -- our own receiver read
/fp-setup in clear text once paired -- so no keys are needed to do this.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
import plistlib
import pprint
from typing import Any

from .timing import TimingRelay

_LOGGER = logging.getLogger(__name__)

_HEADER_END = b"\r\n\r\n"

# Answered here rather than forwarded. Everything else -- pair-setup,
# pair-verify, fp-setup, /info, /server-info -- belongs to UxPlay.
INTERCEPTED = ("/play", "/playback-info", "/scrub", "/rate", "/stop", "/action")

# Polled about twice a second for as long as a sender is connected. Logging each
# one buries everything worth reading -- a few minutes of it fills the whole
# window the log API will return.
CHATTER = ("/playback-info", "/feedback", "/scrub")


class Message:
    """One parsed HTTP message, kept with the bytes needed to replay it."""

    def __init__(self, head: bytes, headers: dict[str, str], body: bytes) -> None:
        """Hold the raw head, its parsed headers and the body."""
        self.head = head
        self.headers = headers
        self.body = body

    @property
    def start_line(self) -> str:
        """Return the request or status line."""
        return self.head.split(b"\r\n", 1)[0].decode("utf-8", "replace")

    @property
    def path(self) -> str:
        """Return the request path, without any query."""
        parts = self.start_line.split(" ")
        return parts[1].split("?")[0] if len(parts) > 1 else ""

    @property
    def method(self) -> str:
        """Return the request method."""
        return self.start_line.split(" ")[0]

    def plist(self) -> Any | None:
        """Return the body decoded as a plist, or None."""
        if not self.body:
            return None
        try:
            return plistlib.loads(self.body)
        except (plistlib.InvalidFileException, ValueError, EOFError):
            return None

    def raw(self) -> bytes:
        """Return the message exactly as it arrived."""
        return self.head + _HEADER_END + self.body

    def rate_from_query(self) -> float | None:
        """Return the rate from the query string, where senders often put it.

        /rate is sent either as a plist body or as `?value=0.000000`, depending
        on the sender and what it is doing, so both have to be read.
        """
        _, _, query = self.start_line.partition("?")
        for pair in query.split(" ")[0].split("&"):
            key, separator, value = pair.partition("=")
            if separator and key == "value":
                try:
                    return float(value)
                except ValueError:
                    return None
        return None

    def timing_port(self) -> int | None:
        """Return the port the sender wants its timing packets sent to.

        Announced in SETUP as `timingPort`, alongside `timingProtocol: NTP`.
        UxPlay will send there, but at the address our connection came from,
        which is loopback -- hence the relay.
        """
        payload = self.plist()
        if not isinstance(payload, dict):
            return None
        port = payload.get("timingPort")
        return int(port) if isinstance(port, int) and port > 0 else None


async def read_message(reader: asyncio.StreamReader) -> Message | None:
    """Read one HTTP message, or None when the peer has finished."""
    try:
        head = await reader.readuntil(_HEADER_END)
    except (asyncio.IncompleteReadError, ConnectionResetError):
        return None
    head = head[: -len(_HEADER_END)]

    headers: dict[str, str] = {}
    for line in head.decode("utf-8", "replace").split("\r\n")[1:]:
        name, separator, value = line.partition(":")
        if separator:
            headers[name.strip().lower()] = value.strip()

    length = int(headers.get("content-length", "0") or "0")
    body = await reader.readexactly(length) if length else b""
    return Message(head, headers, body)


def describe(message: Message, direction: str) -> None:
    """Write one whole message to the log, headers and body included.

    At debug level this is the entire conversation with the sender, in order,
    which is the only way to tell an unimplemented endpoint from a refusal --
    and the thing whose absence has made every failure so far look identical.
    """
    if not _LOGGER.isEnabledFor(logging.DEBUG):
        return

    _LOGGER.debug("%s %s", direction, message.start_line)
    for name, value in message.headers.items():
        _LOGGER.debug("%s     %s: %s", direction, name, value)

    if not message.body:
        return

    payload = message.plist()
    if payload is not None:
        for line in pprint.pformat(payload, width=90, compact=True).splitlines():
            _LOGGER.debug("%s     %s", direction, line)
        return

    text = message.body.decode("utf-8", "replace")
    printable = sum(character.isprintable() or character.isspace() for character in text)
    if printable > len(text) * 0.9:
        _LOGGER.debug("%s     %s", direction, text.strip()[:600])
    else:
        _LOGGER.debug(
            "%s     %d bytes, hex %s", direction, len(message.body), message.body[:96].hex()
        )


Responder = Callable[[Message], Awaitable[bytes | None]]


class Proxy:
    """Forwards a sender's session to UxPlay, keeping some requests for itself."""

    def __init__(
        self,
        listen_port: int,
        upstream_port: int,
        responder: Responder,
        observer: Callable[[Message], None] | None = None,
    ) -> None:
        """Listen on one port and forward to UxPlay on another.

        The observer sees every request, including the ones forwarded
        untouched -- SETUP says who the sender is, and it is not ours to answer.
        """
        self.listen_port = listen_port
        self.upstream_port = upstream_port
        self._responder = responder
        self._observer = observer

    async def serve(self) -> asyncio.Server:
        """Start accepting sessions."""
        server = await asyncio.start_server(self._session, host="0.0.0.0", port=self.listen_port)
        _LOGGER.info("proxying :%d to uxplay on 127.0.0.1:%d", self.listen_port, self.upstream_port)
        return server

    async def _session(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        """Handle one connection from a sender."""
        peer = client_writer.get_extra_info("peername")
        label = f"{peer[0]}:{peer[1]}" if peer else "?"
        _LOGGER.info("session from %s", label)
        upstream_reader = upstream_writer = None
        timing: TimingRelay | None = None
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                "127.0.0.1", self.upstream_port
            )
            while True:
                request = await read_message(client_reader)
                if request is None:
                    break

                if self._observer is not None:
                    self._observer(request)

                quiet = request.path in CHATTER
                if not quiet:
                    describe(request, "<--")
                if request.path in INTERCEPTED:
                    if not quiet:
                        _LOGGER.info("%s %s (answered here)", request.method, request.path)
                    reply = await self._responder(request)
                    client_writer.write(reply if reply is not None else _empty_ok(request))
                    await client_writer.drain()
                    continue

                port = request.timing_port() if request.method == "SETUP" else None
                if port is not None and timing is None and peer:
                    timing = TimingRelay(peer[0], port)
                    await timing.start()

                if not quiet:
                    _LOGGER.info("%s %s (to uxplay)", request.method, request.path)
                upstream_writer.write(request.raw())
                await upstream_writer.drain()

                response = await read_message(upstream_reader)
                if response is None:
                    _LOGGER.warning("uxplay closed the connection during %s", request.path)
                    break
                if not quiet:
                    describe(response, "-->")
                client_writer.write(response.raw())
                await client_writer.drain()

                # /reverse upgrades into a channel the receiver pushes events
                # down. Once that happens there are no more request/response
                # pairs to parse, only bytes to carry in both directions.
                if response.start_line.startswith("HTTP/1.1 101"):
                    _LOGGER.debug("%s upgraded to a reverse channel", label)
                    await _shuttle(client_reader, upstream_writer, upstream_reader, client_writer)
                    break
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            _LOGGER.debug("%s disconnected mid-message", label)
        except OSError as error:
            _LOGGER.error("could not reach uxplay on %d: %s", self.upstream_port, error)
        finally:
            if timing is not None:
                timing.close()
            _LOGGER.info("session with %s ended", label)
            for writer in (upstream_writer, client_writer):
                if writer is not None:
                    writer.close()


def _empty_ok(request: Message) -> bytes:
    """Return a bare 200 for a request we chose not to answer with a body."""
    head = "HTTP/1.1 200 OK\r\nContent-Length: 0\r\n"
    if (cseq := request.headers.get("cseq")) is not None:
        head += f"CSeq: {cseq}\r\n"
    return (head + "\r\n").encode()


def plist_response(request: Message, payload: Any) -> bytes:
    """Return an XML plist response to one request."""
    body = plistlib.dumps(payload, fmt=plistlib.FMT_XML)
    head = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/x-apple-plist+xml\r\n"
        f"Content-Length: {len(body)}\r\n"
    )
    if (cseq := request.headers.get("cseq")) is not None:
        head += f"CSeq: {cseq}\r\n"
    return (head + "\r\n").encode() + body


async def _shuttle(
    client_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
) -> None:
    """Carry raw bytes both ways until either side stops."""

    async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while chunk := await reader.read(65536):
                writer.write(chunk)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass

    await asyncio.wait(
        {
            asyncio.create_task(pump(client_reader, upstream_writer)),
            asyncio.create_task(pump(upstream_reader, client_writer)),
        },
        return_when=asyncio.FIRST_COMPLETED,
    )
