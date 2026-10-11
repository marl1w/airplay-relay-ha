"""Fetch URLs over connections that stay open between requests."""

from __future__ import annotations

import http.client
import threading
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class Fetcher:
    """Reuses one connection per host and thread, so a segment skips the handshake."""

    def __init__(self, user_agent: str = "", timeout: float = 20.0) -> None:
        """Identify as the given client, giving up on a request after `timeout` seconds."""
        self.user_agent = user_agent
        self.timeout = timeout
        self._local = threading.local()

    def _connection(self, scheme: str, host: str) -> http.client.HTTPConnection:
        """Return this thread's connection to the host, opening one if needed."""
        pool: dict[tuple[str, str], http.client.HTTPConnection] = self._local.__dict__.setdefault(
            "pool", {}
        )
        connection = pool.get((scheme, host))
        if connection is None:
            factory = (
                http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
            )
            connection = pool[(scheme, host)] = factory(host, timeout=self.timeout)
        return connection

    def _drop(self, scheme: str, host: str) -> None:
        pool = self._local.__dict__.get("pool", {})
        if connection := pool.pop((scheme, host), None):
            connection.close()

    def _plain(self, url: str) -> bytes:
        """Fetch with a connection of its own, which follows redirects."""
        headers = {"User-Agent": self.user_agent} if self.user_agent else {}
        with urlopen(Request(url, headers=headers), timeout=self.timeout) as response:
            return response.read()

    def get(self, url: str) -> bytes:
        """Return the body at the URL, over a kept-alive connection where possible."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return self._plain(url)
        target = parts.path or "/"
        if parts.query:
            target += f"?{parts.query}"
        headers = {"User-Agent": self.user_agent} if self.user_agent else {}

        # A second try, as a connection the server closed while idle fails on first use.
        for _ in range(2):
            connection = self._connection(parts.scheme, parts.netloc)
            try:
                connection.request("GET", target, headers=headers)
                response = connection.getresponse()
                body = response.read()
            except (http.client.HTTPException, OSError):
                self._drop(parts.scheme, parts.netloc)
                continue
            if response.status == 200:
                if response.will_close:
                    self._drop(parts.scheme, parts.netloc)
                return body
            self._drop(parts.scheme, parts.netloc)
            break
        # Redirects and errors get urlopen's handling, which raises on a bad status.
        return self._plain(url)
