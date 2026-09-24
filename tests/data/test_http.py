import datetime

import pytest

from giml import __version__
from giml.data.http import FetchError, UrlLibFetcher, parse_http_date
from tests.conftest import Route

UTC = datetime.UTC


def test_download_streams_file(http_server, tmp_path):
    body = bytes(range(256)) * 800
    http_server.routes["/all.zip"] = Route(body)
    dest = tmp_path / "all.zip"
    UrlLibFetcher().download(f"{http_server.base_url}/all.zip", dest)
    assert dest.read_bytes() == body


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


def test_truncated_download_is_a_fetch_error(http_server, tmp_path):
    # Server promises 1000 bytes and closes after 10: http.client raises IncompleteRead.
    http_server.routes["/short.zip"] = Route(b"0123456789", headers={"X-Declared-Length": "1000"})
    with pytest.raises(FetchError, match=r"/short.zip: download interrupted"):
        UrlLibFetcher().download(f"{http_server.base_url}/short.zip", tmp_path / "short.zip")


def test_slow_server_times_out(http_server):
    http_server.routes["/slow"] = Route(b"late", delay_seconds=1.0)
    with pytest.raises(FetchError, match=r"/slow: .*timed out"):
        UrlLibFetcher(timeout_seconds=0.2).get_text(f"{http_server.base_url}/slow")


def test_default_timeout_is_sixty_seconds():
    assert UrlLibFetcher().timeout == 60.0


def test_connection_refused_reason_is_reported():
    with pytest.raises(FetchError, match=r"^http://127.0.0.1:9/x: \[Errno 111\] Connection refused$"):
        UrlLibFetcher(timeout_seconds=2).get_text("http://127.0.0.1:9/x")


def test_http_dates_are_normalised_to_utc():
    # "-0000" means "UTC, source unknown" and parses as a naive datetime.
    for value in ("Wed, 07 May 2025 00:29:33 -0000", "Wed, 07 May 2025 02:29:33 +0200"):
        parsed = parse_http_date(value)
        assert parsed.tzinfo is UTC and parsed.isoformat() == "2025-05-07T00:29:33+00:00"
