import datetime
import json
import sqlite3

import pytest

from giml.core.model import Coordinate, SeverityRating, SeveritySource, SnapshotInfo
from giml.data import osv
from giml.data.osv import Interval, LocalOsvAdvisorySource, build_index, extract_cves, extract_severity, intervals
from giml.data.snapshots import read_manifest
from tests.data.osv_helpers import logback_advisories, write_osv_zip

LOGBACK_CORE = Coordinate("ch.qos.logback", "logback-core")
FETCHED = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=datetime.UTC)


def advisory(advisory_id, package, ranges=None, versions=None, **fields):
    affected = {"package": {"name": package, "ecosystem": "Maven"}}
    if ranges is not None:
        affected["ranges"] = [{"type": "ECOSYSTEM", "events": events} for events in ranges]
    if versions is not None:
        affected["versions"] = versions
    return {"id": advisory_id, "affected": [affected], **fields}


def source_for(tmp_path, advisories, extra=None):
    snapshot_dir = tmp_path / "snap"
    snapshot_dir.mkdir()
    zip_path = write_osv_zip(tmp_path / "all.zip", advisories, extra)
    stats = build_index(zip_path, snapshot_dir / osv.INDEX_FILE)
    info = SnapshotInfo("osv-test", "osv", FETCHED, "0" * 64, snapshot_dir)
    return LocalOsvAdvisorySource(info), stats


@pytest.fixture
def logback(tmp_path):
    source, _ = source_for(tmp_path, logback_advisories())
    yield source
    source.close()


def ids(findings):
    return {f.advisory_id for f in findings}


# --- Ported from RedKite OsvPackageVulnerabilitiesTest --------------------------------------


@pytest.mark.parametrize(
    ("version", "count"),
    [("1.5.9", 6), ("1.5.10", 6), ("1.5.11", 6), ("1.5.12", 6), ("1.5.13", 4), ("1.5.14", 4)],
)
def test_matches_live_observed_counts_for_logback_core(logback, version, count):
    assert len(logback.affecting(LOGBACK_CORE, version)) == count


@pytest.mark.parametrize("version", ["1.5.34", "1.5.38"])
def test_version_past_every_fixed_bound_is_clean(logback, version):
    assert logback.affecting(LOGBACK_CORE, version) == []


def test_old_version_matches_only_inception_scoped_advisories(logback):
    found = ids(logback.affecting(LOGBACK_CORE, "1.0.0"))
    assert len(found) == 9
    assert "GHSA-gm62-rw4g-vrc4" not in found


def test_severity_cve_and_bounds_are_extracted(logback):
    hit = next(f for f in logback.affecting(LOGBACK_CORE, "1.4.13") if f.advisory_id == "GHSA-gm62-rw4g-vrc4")
    assert hit.severity.rating is SeverityRating.HIGH
    assert hit.severity.source is SeveritySource.LABEL
    assert hit.cves == ("CVE-2023-6481",)
    assert (hit.introduced, hit.fixed) == ("1.4.13", "1.4.14")
    assert (hit.coordinate, hit.version) == (LOGBACK_CORE, "1.4.13")


def test_distinct_advisories_for_same_version_are_all_returned(logback):
    found = ids(logback.affecting(LOGBACK_CORE, "1.4.13"))
    assert "GHSA-gm62-rw4g-vrc4" in found and len(found) >= 4


def test_package_name_only_matches_requested_coordinate(tmp_path):
    source, _ = source_for(tmp_path, [advisory("GHSA-x", "com.example:other", [[{"introduced": "0"}, {"fixed": "99"}]])])
    assert source.affecting(Coordinate("com.example", "target"), "1.0.0") == []


def test_explicit_versions_list_is_used_when_ranges_are_absent(tmp_path):
    source, _ = source_for(tmp_path, [advisory("GHSA-x", "com.example:legacy", versions=["1.0", "1.1", "1.2"])])
    target = Coordinate("com.example", "legacy")
    [hit] = source.affecting(target, "1.1")
    assert (hit.introduced, hit.fixed) == (None, None)
    assert source.affecting(target, "1.3") == []


def test_last_affected_is_inclusive(tmp_path):
    source, _ = source_for(tmp_path, [advisory("GHSA-x", "com.example:old", [[{"introduced": "0"}, {"last_affected": "2.0.0"}]])])
    target = Coordinate("com.example", "old")
    [hit] = source.affecting(target, "2.0.0")
    assert hit.fixed is None
    assert source.affecting(target, "2.0.1") == []


def test_unknown_coordinate_has_no_findings(logback):
    assert logback.affecting(Coordinate("com.example", "nothing"), "1.0") == []


# --- Maven ordering and OSV evaluation rules -------------------------------------------------


def test_ranges_use_maven_ordering_not_string_ordering(tmp_path):
    # RedKite's comparator placed 1.0-sp1 below 1.0; Maven places it above.
    source, _ = source_for(tmp_path, [advisory("GHSA-x", "g:a", [[{"introduced": "0"}, {"fixed": "1.0"}]])])
    target = Coordinate("g", "a")
    assert ids(source.affecting(target, "1.0-rc1")) == {"GHSA-x"}
    assert source.affecting(target, "1.0-sp1") == []
    assert source.affecting(target, "1.0.RELEASE") == []  # equal to 1.0 in Maven


def test_unsorted_events_are_sorted_before_evaluation():
    assert intervals([{"fixed": "2.0"}, {"introduced": "1.0"}]) == [Interval("1.0", "2.0", False)]


def test_multiple_segments_in_one_range():
    events = [{"introduced": "0"}, {"fixed": "1.2.9"}, {"introduced": "1.3.0"}, {"fixed": "1.3.5"}]
    assert intervals(events) == [Interval(None, "1.2.9", False), Interval("1.3.0", "1.3.5", False)]


def test_open_ended_introduced_is_unbounded():
    assert intervals([{"introduced": "3.0"}]) == [Interval("3.0", None, False)]


def test_limit_and_malformed_events_are_ignored():
    events = [{"introduced": "0"}, {"limit": "5.0"}, "junk", {"fixed": "2.0", "extra": 1}, {"fixed": "3.0"}]
    assert intervals(events) == [Interval(None, "3.0", False)]


def test_fixed_without_introduced_opens_nothing():
    assert intervals([{"fixed": "2.0"}]) == []


def test_bounds_combine_across_matching_intervals(tmp_path):
    adv = advisory("GHSA-x", "g:a", [[{"introduced": "1.0"}, {"fixed": "1.5"}], [{"introduced": "0.9"}, {"fixed": "1.8"}]])
    source, _ = source_for(tmp_path, [adv])
    [hit] = source.affecting(Coordinate("g", "a"), "1.2")
    assert (hit.introduced, hit.fixed) == ("0.9", "1.8")


def test_non_ecosystem_ranges_and_other_ecosystems_are_ignored(tmp_path):
    adv = {
        "id": "GHSA-x",
        "affected": [
            {"package": {"name": "g:a", "ecosystem": "Maven"},
             "ranges": [{"type": "GIT", "events": [{"introduced": "0"}]}]},
            {"package": {"name": "g:a", "ecosystem": "PyPI"},
             "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}]}]},
        ],
    }  # fmt: skip
    source, stats = source_for(tmp_path, [adv])
    assert source.affecting(Coordinate("g", "a"), "1.0") == []
    assert stats["affected_entries"] == 1


# --- Severity and CVEs ------------------------------------------------------------------------


def test_cvss_v3_vector_is_preferred_over_label():
    severity = extract_severity({
        "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
        "database_specific": {"severity": "LOW"},
    })  # fmt: skip
    assert (severity.rating, severity.source, severity.score) == (SeverityRating.CRITICAL, SeveritySource.CVSS_V3, 9.8)
    assert severity.vector.startswith("CVSS:3.1/")


def test_worst_of_several_v3_vectors_is_used():
    severity = extract_severity({"severity": [
        {"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H"},
        {"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"},
    ]})  # fmt: skip
    assert severity.score == 9.8


def test_v4_only_advisory_falls_back_to_label():
    severity = extract_severity({
        "severity": [{"type": "CVSS_V4", "score": "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N"}],
        "database_specific": {"severity": "MODERATE"},
    })  # fmt: skip
    assert (severity.rating, severity.source, severity.score) == (SeverityRating.MEDIUM, SeveritySource.LABEL, None)


def test_invalid_vector_falls_back_to_label():
    severity = extract_severity({"severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/garbage"}],
                                 "database_specific": {"severity": "HIGH"}})  # fmt: skip
    assert severity.source is SeveritySource.LABEL


@pytest.mark.parametrize("advisory_data", [{}, {"database_specific": {"severity": "weird"}}, {"database_specific": {"severity": 3}}])
def test_no_usable_severity_is_unknown(advisory_data):
    severity = extract_severity(advisory_data)
    assert (severity.rating, severity.source) == (SeverityRating.UNKNOWN, SeveritySource.NONE)


def test_cves_come_from_id_and_aliases():
    assert extract_cves({"id": "CVE-2021-1", "aliases": ["GHSA-a", "CVE-2020-2", "CVE-2021-1"]}) == ["CVE-2020-2", "CVE-2021-1"]
    assert extract_cves({"id": "GHSA-a"}) == []


# --- Ingest -----------------------------------------------------------------------------------


def test_ingest_counts_and_skips_withdrawn_and_malformed(tmp_path):
    advisories = logback_advisories() + [advisory("GHSA-gone", "g:a", [[{"introduced": "0"}]], withdrawn="2025-01-01T00:00:00Z")]
    extra = {"broken.json": b"{not json", "array.json": b"[1]", "README.txt": b"ignored"}
    source, stats = source_for(tmp_path, advisories, extra)
    assert stats["advisories"] == 10
    assert stats["withdrawn"] == 1
    assert stats["malformed"] == 2
    assert stats["malformed_files"] == ["array.json", "broken.json"]
    assert source.affecting(Coordinate("g", "a"), "1.0") == []


def test_index_is_read_only(logback):
    with pytest.raises(sqlite3.OperationalError):
        logback._conn.execute("DELETE FROM advisory")


def test_missing_index_is_reported(tmp_path):
    info = SnapshotInfo("osv-x", "osv", FETCHED, "0" * 64, tmp_path)
    with pytest.raises(FileNotFoundError, match="has no index"):
        LocalOsvAdvisorySource(info)


def test_repeated_queries_use_the_per_package_cache(logback):
    first = logback.affecting(LOGBACK_CORE, "1.5.9")
    logback._conn.close()  # a second query must not touch the database
    assert logback.affecting(LOGBACK_CORE, "1.5.9") == first


class FakeFetcher:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.urls: list[str] = []

    def download(self, url, dest):
        self.urls.append(url)
        dest.write_bytes(self.payload)


def test_sync_publishes_snapshot_with_index_and_manifest(tmp_path):
    payload = write_osv_zip(tmp_path / "src.zip", logback_advisories()).read_bytes()
    fetcher = FakeFetcher(payload)
    info = osv.sync(tmp_path / "state", fetcher, lambda: FETCHED)
    assert fetcher.urls == [osv.OSV_MAVEN_URL]
    assert info.source == "osv" and info.fetched_at == FETCHED
    manifest = read_manifest(info.path)
    assert manifest["stats"]["advisories"] == 10
    assert manifest["sources"] == [osv.OSV_MAVEN_URL]
    source = LocalOsvAdvisorySource(info)
    assert len(source.affecting(LOGBACK_CORE, "1.5.9")) == 6
    assert source.snapshot_id == info.id
    source.close()


def test_sync_is_deterministic_for_same_input(tmp_path):
    payload = write_osv_zip(tmp_path / "src.zip", logback_advisories()).read_bytes()
    first = osv.sync(tmp_path / "a", FakeFetcher(payload), lambda: FETCHED)
    second = osv.sync(tmp_path / "b", FakeFetcher(payload), lambda: FETCHED)
    assert first.id == second.id and first.content_hash == second.content_hash
    assert json.loads((first.path / "manifest.json").read_text())["stats"] == json.loads(
        (second.path / "manifest.json").read_text()
    )["stats"]


def test_sync_failure_leaves_no_snapshot(tmp_path):
    class Failing:
        def download(self, url, dest):
            dest.write_bytes(b"partial")
            raise RuntimeError("connection reset")

    with pytest.raises(RuntimeError):
        osv.sync(tmp_path, Failing(), lambda: FETCHED)
    assert list((tmp_path / "snapshots" / "osv").iterdir()) == []
