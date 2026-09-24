import datetime
import threading

import pytest

from giml.core.model import Coordinate
from giml.data import central
from giml.data.central import LocalCentralMetadataSource, MetadataError, parse_metadata
from giml.data.http import FetchError
from giml.data.snapshots import read_manifest

UTC = datetime.UTC
BASE = "https://repo.example"
DATABIND = Coordinate("com.fasterxml.jackson.core", "jackson-databind")
MISSING = Coordinate("com.example", "missing")


def metadata(*versions, release=None, latest=None):
    listed = "".join(f"<version>{v}</version>" for v in versions)
    extra = (f"<release>{release}</release>" if release else "") + (f"<latest>{latest}</latest>" if latest else "")
    return f"<metadata><groupId>g</groupId><versioning>{extra}<versions>{listed}</versions></versioning></metadata>"


class FakeFetcher:
    """Serves metadata and POM dates from dicts keyed by URL; records every request."""

    def __init__(self, texts=None, dates=None):
        self.texts = texts or {}
        self.dates = dates or {}
        self.requests: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def get_text(self, url):
        with self.lock:
            self.requests.append(("GET", url))
        return self.texts.get(url)

    def last_modified(self, url):
        with self.lock:
            self.requests.append(("HEAD", url))
        if url in self.dates and isinstance(self.dates[url], Exception):
            raise self.dates[url]
        return self.dates.get(url)


def pom(coordinate, version):
    return central.pom_url(BASE, coordinate, version)


def clock_at(hour):
    return lambda: datetime.datetime(2026, 9, 24, hour, 0, tzinfo=UTC)


def test_urls_follow_repository_layout():
    assert central.metadata_url(BASE, DATABIND) == (
        "https://repo.example/com/fasterxml/jackson/core/jackson-databind/maven-metadata.xml"
    )
    assert pom(DATABIND, "2.18.4") == (
        "https://repo.example/com/fasterxml/jackson/core/jackson-databind/2.18.4/jackson-databind-2.18.4.pom"
    )


def test_parse_metadata_lists_versions_and_adds_release_and_latest():
    assert parse_metadata(metadata("1.0", " 1.1 ", "", release="1.2", latest="1.3-SNAPSHOT")) == [
        "1.0", "1.1", "1.2", "1.3-SNAPSHOT",
    ]  # fmt: skip
    assert parse_metadata(metadata("1.0", release="1.0", latest="1.0")) == ["1.0"]


def test_parse_metadata_without_versioning_is_empty():
    assert parse_metadata("<metadata><groupId>g</groupId></metadata>") == []


def test_parse_metadata_rejects_invalid_xml():
    with pytest.raises(MetadataError, match="invalid maven-metadata.xml"):
        parse_metadata("<metadata>")


def test_sync_fetches_versions_and_release_dates(tmp_path):
    fetcher = FakeFetcher(
        texts={central.metadata_url(BASE, DATABIND): metadata("2.18.4", "2.9.0", "2.10.0")},
        dates={
            pom(DATABIND, "2.18.4"): datetime.datetime(2025, 5, 7, 0, 29, 33, tzinfo=UTC),
            pom(DATABIND, "2.9.0"): datetime.datetime(2017, 7, 30, tzinfo=UTC),
        },
    )
    info = central.sync(tmp_path, fetcher, clock_at(1), [DATABIND, MISSING], None, base_url=BASE)
    source = LocalCentralMetadataSource(info)
    releases = source.versions(DATABIND)
    assert [r.version for r in releases] == ["2.9.0", "2.10.0", "2.18.4"]  # Maven order
    assert releases[2].released_at == datetime.datetime(2025, 5, 7, 0, 29, 33, tzinfo=UTC)
    assert releases[1].released_at is None  # no Last-Modified: recorded as unknown
    assert source.versions(MISSING) is None
    assert source.versions(Coordinate("never", "synced")) is None
    assert source.snapshot_id == info.id
    stats = read_manifest(info.path)["stats"]
    assert stats == {"coordinates": 2, "not_found": 1, "versions": 3, "release_dates_fetched": 2,
                     "release_dates_reused": 0, "release_dates_missing": 1}  # fmt: skip


def test_second_sync_reuses_dates_and_keeps_earlier_coordinates(tmp_path):
    other = Coordinate("org.example", "lib")
    first_fetcher = FakeFetcher(
        texts={central.metadata_url(BASE, DATABIND): metadata("1.0")},
        dates={pom(DATABIND, "1.0"): datetime.datetime(2020, 1, 1, tzinfo=UTC)},
    )
    first = central.sync(tmp_path, first_fetcher, clock_at(1), [DATABIND], None, base_url=BASE)

    second_fetcher = FakeFetcher(
        texts={
            central.metadata_url(BASE, DATABIND): metadata("1.0", "1.1"),
            central.metadata_url(BASE, other): metadata("3.0"),
        },
        dates={
            pom(DATABIND, "1.1"): datetime.datetime(2021, 1, 1, tzinfo=UTC),
            pom(other, "3.0"): datetime.datetime(2022, 1, 1, tzinfo=UTC),
        },
    )
    second = central.sync(tmp_path, second_fetcher, clock_at(2), [other], first, base_url=BASE)

    heads = sorted(url for method, url in second_fetcher.requests if method == "HEAD")
    assert heads == sorted([pom(DATABIND, "1.1"), pom(other, "3.0")])  # 1.0's date was reused
    source = LocalCentralMetadataSource(second)
    assert [r.version for r in source.versions(DATABIND)] == ["1.0", "1.1"]
    assert source.versions(DATABIND)[0].released_at == datetime.datetime(2020, 1, 1, tzinfo=UTC)
    assert [r.version for r in source.versions(other)] == ["3.0"]
    assert read_manifest(second.path)["stats"]["release_dates_reused"] == 1


def test_missing_dates_are_retried_on_the_next_sync(tmp_path):
    texts = {central.metadata_url(BASE, DATABIND): metadata("1.0")}
    first = central.sync(tmp_path, FakeFetcher(texts), clock_at(1), [DATABIND], None, base_url=BASE)
    retry = FakeFetcher(texts, {pom(DATABIND, "1.0"): datetime.datetime(2020, 1, 1, tzinfo=UTC)})
    second = central.sync(tmp_path, retry, clock_at(2), [], first, base_url=BASE)
    assert ("HEAD", pom(DATABIND, "1.0")) in retry.requests
    assert LocalCentralMetadataSource(second).versions(DATABIND)[0].released_at is not None


def test_same_inputs_give_same_content_hash(tmp_path):
    def run(directory):
        fetcher = FakeFetcher(
            texts={central.metadata_url(BASE, DATABIND): metadata(*[f"1.{i}" for i in range(40)])},
            dates={pom(DATABIND, f"1.{i}"): datetime.datetime(2020, 1, 1 + i % 28, tzinfo=UTC) for i in range(40)},
        )
        return central.sync(directory, fetcher, clock_at(1), [DATABIND], None, base_url=BASE, workers=8)

    assert run(tmp_path / "a").content_hash == run(tmp_path / "b").content_hash


def test_fetch_failure_aborts_without_snapshot(tmp_path):
    fetcher = FakeFetcher(
        texts={central.metadata_url(BASE, DATABIND): metadata("1.0")},
        dates={pom(DATABIND, "1.0"): FetchError(pom(DATABIND, "1.0"), "HTTP 503")},
    )
    with pytest.raises(FetchError, match="HTTP 503"):
        central.sync(tmp_path, fetcher, clock_at(1), [DATABIND], None, base_url=BASE)
    assert not (tmp_path / "snapshots").exists()
