"""HTTP access for ``giml sync``. Everything goes through the Fetcher protocol so tests use fakes."""

from __future__ import annotations

import datetime
import email.utils
import http.client
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol

from giml import __version__

_CHUNK = 1 << 16


class FetchError(RuntimeError):
    """A request failed. The message always names the URL."""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"{url}: {reason}")
        self.url = url


class Fetcher(Protocol):
    def download(self, url: str, dest: Path) -> None:
        """Stream a URL to a file."""

    def get_text(self, url: str) -> str | None:
        """Fetch a text resource, or None if it does not exist (404)."""

    def last_modified(self, url: str) -> datetime.datetime | None:
        """The Last-Modified time from a HEAD request, or None if absent or 404."""


class UrlLibFetcher:
    def __init__(self, timeout_seconds: float = 60.0) -> None:
        self.timeout = timeout_seconds
        self.headers = {"User-Agent": f"giml/{__version__}"}

    def _open(self, url: str, method: str = "GET"):
        request = urllib.request.Request(url, headers=self.headers, method=method)
        try:
            return urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise FetchError(url, f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FetchError(url, str(getattr(exc, "reason", exc))) from exc

    def download(self, url: str, dest: Path) -> None:
        response = self._open(url)
        if response is None:
            raise FetchError(url, "HTTP 404")
        declared = response.headers.get("Content-Length")
        received = 0
        try:
            with response, dest.open("wb") as out:
                while chunk := response.read(_CHUNK):
                    out.write(chunk)
                    received += len(chunk)
        except (OSError, http.client.HTTPException) as exc:
            raise FetchError(url, f"download interrupted: {exc!r}") from exc
        # http.client does not raise when a body ends early while reading in chunks.
        if declared is not None and declared.isdigit() and received != int(declared):
            raise FetchError(url, f"download interrupted: received {received} of {declared} bytes")

    def get_text(self, url: str) -> str | None:
        response = self._open(url)
        if response is None:
            return None
        with response:
            return response.read().decode("utf-8")

    def last_modified(self, url: str) -> datetime.datetime | None:
        response = self._open(url, method="HEAD")
        if response is None:
            return None
        with response:
            return parse_http_date(response.headers.get("Last-Modified"))


def parse_http_date(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed.astimezone(datetime.UTC)
