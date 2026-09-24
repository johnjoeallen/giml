import datetime
import hashlib

import pytest

from giml import __version__
from giml.data.http import FetchError, UrlLibFetcher, parse_http_date
from tests.conftest import Route

UTC = datetime.UTC


def test_download_streams_file_and_returns_sha256(http_server, tmp_path):
    body = b"x" * 200_000
    http_server.routes["/all.zip"] = Route(body)
    dest = tmp_path / "all.zip"
    digest = UrlLibFetcher().download(f"{http_server.base_url}/all.zip", dest)
    assert dest.read_bytes() == body
    assert digest == hashlib.sha256(body).hexdigest()


def test_download_of_missing_resource_is_an_error(http_server, tmp_path):
    with pytest.raises(FetchError, match=r"/nope: HTTP 404"):
        UrlLibFetcher().download(f"{http_server.base_url}/nope", tmp_path / "x")


def test_server_error_names_url_and_status(http_server):
    http_server.routes["/boom"] = Route(status=500)
    with pytest.raises(FetchError, match=r"/boom: HTTP 500") as exc:
        UrlLibFetcher().get_text(f"{http_server.base_url}/boom")
    assert exc.value.url.endswith("/boom")


def test_connection_failure_is_a_fetch_error():
    # Port 9 (discard) on loopback is closed on any normal machine; nothing leaves the host.
    with pytest.raises(FetchError, match="127.0.0.1:9"):
        UrlLibFetcher(timeout_seconds=2).get_text("http://127.0.0.1:9/x")


def test_get_text_returns_body_or_none(http_server):
    http_server.routes["/meta.xml"] = Route("<metadata>é</metadata>".encode())
    fetcher = UrlLibFetcher()
    assert fetcher.get_text(f"{http_server.base_url}/meta.xml") == "<metadata>é</metadata>"
    assert fetcher.get_text(f"{http_server.base_url}/missing.xml") is None


def test_last_modified_uses_head_and_parses_date(http_server):
    http_server.routes["/a.pom"] = Route(headers={"Last-Modified": "Wed, 07 May 2025 00:29:33 GMT"})
    fetcher = UrlLibFetcher()
    assert fetcher.last_modified(f"{http_server.base_url}/a.pom") == datetime.datetime(
        2025, 5, 7, 0, 29, 33, tzinfo=UTC
    )
    assert fetcher.last_modified(f"{http_server.base_url}/gone.pom") is None
    assert ("HEAD", "/a.pom", f"giml/{__version__}") in http_server.requests


def test_requests_identify_giml(http_server):
    http_server.routes["/ua"] = Route(b"ok")
    UrlLibFetcher().get_text(f"{http_server.base_url}/ua")
    assert http_server.requests == [("GET", "/ua", f"giml/{__version__}")]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Wed, 07 May 2025 00:29:33 GMT", datetime.datetime(2025, 5, 7, 0, 29, 33, tzinfo=UTC)),
        ("Wed, 07 May 2025 02:29:33 +0200", datetime.datetime(2025, 5, 7, 0, 29, 33, tzinfo=UTC)),
        (None, None),
        ("", None),
        ("not a date", None),
    ],
)
def test_parse_http_date(value, expected):
    assert parse_http_date(value) == expected
