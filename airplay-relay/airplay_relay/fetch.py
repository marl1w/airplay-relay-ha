"""Fetch URLs over connections that stay open between requests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import http.client
import re
import threading
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

# The first slice asked for; a segment no larger than this arrives in one request.
SLICE = 256 * 1024

# Connections one segment may be split across.
SPLIT = 4


class Fetcher:
    """Reuses one connection per host and thread, so a segment skips the handshake."""

    def __init__(self, user_agent: str = "", timeout: float = 20.0) -> None:
        """Identify as the given client, giving up on a request after `timeout` seconds."""
        self.user_agent = user_agent
        self.timeout = timeout
        self._local = threading.local()
        self._slices = ThreadPoolExecutor(max_workers=SPLIT * 3, thread_name_prefix="slice")

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

    def get_plain(self, url: str) -> bytes:
        """Fetch over a connection of its own, as a browser would."""
        return self._plain(url)

    def _plain(self, url: str) -> bytes:
        """Fetch with a connection of its own, which follows redirects."""
        headers = {"User-Agent": self.user_agent} if self.user_agent else {}
        with urlopen(Request(url, headers=headers), timeout=self.timeout) as response:
            return response.read()

    def get(self, url: str, first: tuple[int, int] | None = None) -> bytes:
        """Return the body at the URL, over a kept-alive connection where possible.

        With `first`, ask for that byte range only and return whatever the server sends.
        """
        return self._request(url, first)[0]

    def _request(self, url: str, span: tuple[int, int] | None = None) -> tuple[bytes, int, str]:
        """Return the body, the HTTP status and the Content-Range of one request."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return self._plain(url), 200, ""
        target = parts.path or "/"
        if parts.query:
            target += f"?{parts.query}"
        headers = {"User-Agent": self.user_agent} if self.user_agent else {}
        if span:
            headers["Range"] = f"bytes={span[0]}-{span[1]}"

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
            if response.status == 200 or (span and response.status == 206):
                if response.will_close:
                    self._drop(parts.scheme, parts.netloc)
                return body, response.status, response.getheader("Content-Range") or ""
            self._drop(parts.scheme, parts.netloc)
            break
        # Redirects and errors get urlopen's handling, which raises on a bad status.
        return self._plain(url), 200, ""

    def get_split(self, url: str) -> bytes:
        """Return the body, its tail fetched over several connections at once.

        Falls back to a plain fetch when the server will not serve byte ranges.
        """
        try:
            body, status, content_range = self._request(url, (0, SLICE - 1))
            total = re.search(r"/(\d+)$", content_range)
            if status != 206 or total is None:
                return body if status == 200 else self.get(url)
            size = int(total.group(1))
            if size <= len(body):
                return body[:size]
            remaining = size - SLICE
            pieces = min(SPLIT, max(1, -(-remaining // SLICE)))
            step = -(-remaining // pieces)
            spans = [
                (SLICE + n * step, min(size, SLICE + (n + 1) * step) - 1) for n in range(pieces)
            ]
            parts = list(self._slices.map(lambda span: self._slice(url, span), spans))
            whole = b"".join([body, *parts])
        except (http.client.HTTPException, OSError, ValueError):
            return self.get(url)
        return whole if len(whole) == size else self.get(url)

    def _slice(self, url: str, span: tuple[int, int]) -> bytes:
        """Return one byte range, insisting on exactly what was asked for."""
        body, status, _ = self._request(url, span)
        if status != 206 or len(body) != span[1] - span[0] + 1:
            raise ValueError("the server did not honour the range")
        return body
