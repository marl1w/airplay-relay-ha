"""Sit in front of UxPlay and answer the playback requests from our own channel."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
import logging
import plistlib
import pprint
import socket
from typing import Any

from .timing import TimingRelay

_LOGGER = logging.getLogger(__name__)

_HEADER_END = b"\r\n\r\n"

INTERCEPTED = ("/play", "/playback-info", "/scrub", "/rate", "/stop", "/action")

# Polled about twice a second, so not logged.
CHATTER = ("/playback-info", "/feedback", "/scrub")

# UxPlay takes one client at a time, so a silent session would lock out every sender.
IDLE_SECONDS = 60.0

# Detects a vanished sender on the reverse channel, where silence is normal.
KEEPALIVE_IDLE = 30
KEEPALIVE_INTERVAL = 10
KEEPALIVE_COUNT = 3


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
        """Return the rate from the query string, where senders often put it."""
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
        """Return the port the sender wants its timing packets sent to."""
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
    """Write one whole message to the log, headers and body included."""
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
        idle_seconds: float = IDLE_SECONDS,
    ) -> None:
        """Listen on one port and forward to UxPlay on another."""
        self.listen_port = listen_port
        self.upstream_port = upstream_port
        self._responder = responder
        self._observer = observer
        self._idle_seconds = idle_seconds
        # The sender's reverse channel, over which a receiver may ask it to fetch a URL.
        self.reverse: asyncio.StreamWriter | None = None

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
        _keep_alive(client_writer)
        upstream_reader = upstream_writer = None
        timing: TimingRelay | None = None
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                "127.0.0.1", self.upstream_port
            )
            while True:
                try:
                    request = await asyncio.wait_for(
                        read_message(client_reader), timeout=self._idle_seconds
                    )
                except TimeoutError:
                    # Only the session ends; the channel keeps playing without it.
                    _LOGGER.warning(
                        "nothing from %s for %.0fs; letting the session go so the "
                        "next sender is accepted -- the stream carries on",
                        label,
                        self._idle_seconds,
                    )
                    break
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

                # /reverse upgrades to a raw byte channel with no more messages to parse.
                if response.start_line.startswith("HTTP/1.1 101"):
                    _LOGGER.debug("%s upgraded to a reverse channel", label)
                    self.reverse = client_writer
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


async def ask_sender(
    reverse: asyncio.StreamWriter | None, url: str, session: str, request_id: int
) -> bool:
    """Ask the sender to fetch a URL itself (FCUP); its answer comes back as POST /action."""
    if reverse is None or reverse.is_closing():
        return False
    body = plistlib.dumps(
        {
            "sessionID": 1,
            "type": "unhandledURLRequest",
            "request": {
                "FCUP_Response_ClientInfo": 1,
                "FCUP_Response_ClientRef": 40030004,
                "FCUP_Response_RequestID": request_id,
                "FCUP_Response_URL": url,
                "sessionID": 1,
                "FCUP_Response_Headers": {"X-Playback-Session-Id": session},
            },
        },
        fmt=plistlib.FMT_XML,
    )
    head = (
        "POST /event HTTP/1.1\r\n"
        f"X-Apple-Session-ID: {session}\r\n"
        "Content-Type: text/x-apple-plist+xml\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    )
    reverse.write(head.encode() + body)
    await reverse.drain()
    return True


def _keep_alive(writer: asyncio.StreamWriter) -> None:
    """Have the kernel probe a sender's connection that has gone quiet."""
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for option, value in (
        ("TCP_KEEPIDLE", KEEPALIVE_IDLE),
        ("TCP_KEEPINTVL", KEEPALIVE_INTERVAL),
        ("TCP_KEEPCNT", KEEPALIVE_COUNT),
    ):
        if hasattr(socket, option):
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, option), value)


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
        # OSError, not just resets: an unanswered keepalive ends the read with ETIMEDOUT.
        with contextlib.suppress(OSError):
            while chunk := await reader.read(65536):
                writer.write(chunk)
                await writer.drain()

    await asyncio.wait(
        {
            asyncio.create_task(pump(client_reader, upstream_writer)),
            asyncio.create_task(pump(upstream_reader, client_writer)),
        },
        return_when=asyncio.FIRST_COMPLETED,
    )
