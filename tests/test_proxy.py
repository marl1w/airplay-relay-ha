"""Tests for how the proxy releases a sender that goes silent."""

from __future__ import annotations

import asyncio

from airplay_relay.proxy import Message, Proxy, read_message

FEEDBACK = b"POST /feedback RTSP/1.0\r\nCSeq: 1\r\nContent-Length: 0\r\n\r\n"


class FakeUxPlay:
    """Answers every request with a bare 200, and notices when a client leaves."""

    def __init__(self) -> None:
        self.closed = asyncio.Event()
        self.port = 0

    async def start(self) -> asyncio.Server:
        server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = server.sockets[0].getsockname()[1]
        return server

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while await read_message(reader) is not None:
            writer.write(b"RTSP/1.0 200 OK\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        self.closed.set()
        writer.close()


async def _proxy(idle_seconds: float) -> tuple[FakeUxPlay, asyncio.Server, int, list[Message]]:
    uxplay = FakeUxPlay()
    upstream = await uxplay.start()
    answered: list[Message] = []

    async def respond(message: Message) -> bytes | None:
        answered.append(message)
        return None

    proxy = Proxy(0, uxplay.port, respond, idle_seconds=idle_seconds)
    server = await proxy.serve()
    port = server.sockets[0].getsockname()[1]
    return uxplay, upstream, port, answered


def test_a_silent_sender_is_let_go_and_frees_uxplay() -> None:
    async def scenario() -> None:
        uxplay, upstream, port, answered = await _proxy(idle_seconds=0.2)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(FEEDBACK)
        await writer.drain()
        await read_message(reader)

        assert await asyncio.wait_for(reader.read(), timeout=2) == b""
        await asyncio.wait_for(uxplay.closed.wait(), timeout=2)
        # The channel is not told, so a stream it is carrying keeps playing.
        assert answered == []

        writer.close()
        upstream.close()

    asyncio.run(scenario())


def test_a_sender_that_keeps_talking_is_kept() -> None:
    async def scenario() -> None:
        uxplay, upstream, port, _ = await _proxy(idle_seconds=0.3)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)

        # Well past the idle limit in total, never past it between requests.
        for _ in range(8):
            writer.write(FEEDBACK)
            await writer.drain()
            reply = await asyncio.wait_for(read_message(reader), timeout=2)
            assert reply is not None
            assert reply.start_line.startswith("RTSP/1.0 200")
            await asyncio.sleep(0.1)

        assert not uxplay.closed.is_set()
        writer.close()
        upstream.close()

    asyncio.run(scenario())
