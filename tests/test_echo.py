"""Tests for learning a sender's Referer through the echo."""

from __future__ import annotations

import asyncio
from urllib.request import Request, urlopen

from airplay_relay.echo import Echo, hostname


def test_the_name_is_the_address_under_the_domain() -> None:
    assert hostname("192.168.1.10", "local-ip.sh") == "192-168-1-10.local-ip.sh"


def test_the_headers_the_sender_arrives_with_are_learned(tmp_path) -> None:
    async def scenario() -> dict[str, str]:
        echo = Echo("127.0.0.1", 0, "example.invalid", str(tmp_path))
        # Plain HTTP stands in for TLS here; the request handling is the same.
        echo._server = await asyncio.start_server(echo._answer, "127.0.0.1", 0)
        port = echo._server.sockets[0].getsockname()[1]

        async def sender(url: str) -> bool:
            path = url.split("/", 3)[3]

            def fetch() -> None:
                request = Request(
                    f"http://127.0.0.1:{port}/{path}",
                    headers={"Referer": "https://page.example/", "User-Agent": "phone"},
                )
                with urlopen(request, timeout=5) as response:
                    assert response.read().startswith(b"#EXTM3U")

            asyncio.get_running_loop().run_in_executor(None, fetch)
            return True

        try:
            return await echo.learn(sender)
        finally:
            await echo.close()

    headers = asyncio.run(scenario())
    assert headers["referer"] == "https://page.example/"


def test_nothing_is_learned_when_the_sender_never_comes(tmp_path) -> None:
    async def scenario() -> dict[str, str]:
        echo = Echo("127.0.0.1", 0, "example.invalid", str(tmp_path))
        echo._server = await asyncio.start_server(echo._answer, "127.0.0.1", 0)

        async def absent(url: str) -> bool:
            return True

        try:
            return await echo.learn(absent, wait_seconds=0.2)
        finally:
            await echo.close()

    assert asyncio.run(scenario()) == {}
