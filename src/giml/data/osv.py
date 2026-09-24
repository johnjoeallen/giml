"""OSV advisories for the Maven ecosystem: bulk sync, local index and version matching.

Range matching is ported from RedKite's ``OsvPackageVulnerabilities`` and follows the OSV schema's
evaluation rule (events sorted by version; ``introduced`` opens, ``fixed`` closes exclusively,
``last_affected`` closes inclusively), using Maven version ordering throughout.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from giml.core.model import Coordinate, Finding, Severity, SeverityRating, SeveritySource, SnapshotInfo
from giml.data.cvss import CvssError, base_score
from giml.data.http import Fetcher
from giml.data.snapshots import SnapshotWriter
from giml.maven.version import ComparableVersion

SOURCE = "osv"
OSV_MAVEN_URL = "https://osv-vulnerabilities.storage.googleapis.com/Maven/all.zip"
RAW_FILE = "all.zip"
INDEX_FILE = "index.sqlite"
_ECOSYSTEM = "Maven"

_SCHEMA = """
CREATE TABLE advisory (
    id               TEXT PRIMARY KEY,
    cves             TEXT NOT NULL,  -- JSON array
    modified         TEXT,
    severity_rating  TEXT NOT NULL,
    severity_source  TEXT NOT NULL,
    severity_score   REAL,
    severity_vector  TEXT
);
CREATE TABLE affected (
    advisory_id TEXT NOT NULL REFERENCES advisory (id),
    package     TEXT NOT NULL,  -- groupId:artifactId
    ranges      TEXT NOT NULL,  -- JSON: list of ECOSYSTEM event lists
    versions    TEXT NOT NULL   -- JSON array of explicitly affected versions
);
CREATE INDEX affected_package ON affected (package);
"""


# ---------------------------------------------------------------------------------------------
# Severity


def extract_severity(advisory: dict) -> Severity:
    """Severity per decision Q11: CVSS v3.x vector score, else the database label, else unknown."""
    scores = []
    for entry in advisory.get("severity") or []:
        if entry.get("type") == "CVSS_V3":
            try:
                scores.append(base_score(entry.get("score", "")))
            except CvssError:
                continue
    if scores:
        worst = max(scores, key=lambda s: s.base_score)
        return Severity(SeverityRating.from_score(worst.base_score), SeveritySource.CVSS_V3,
                        worst.base_score, worst.vector)  # fmt: skip
    label = (advisory.get("database_specific") or {}).get("severity")
    if isinstance(label, str) and SeverityRating.from_label(label) is not SeverityRating.UNKNOWN:
        return Severity(SeverityRating.from_label(label), SeveritySource.LABEL)
    return Severity(SeverityRating.UNKNOWN, SeveritySource.NONE)


def extract_cves(advisory: dict) -> list[str]:
    ids = [advisory.get("id", "")] + list(advisory.get("aliases") or [])
    return sorted({i for i in ids if isinstance(i, str) and i.startswith("CVE-")})


# ---------------------------------------------------------------------------------------------
# Range matching


@dataclass(frozen=True)
class Interval:
    introduced: str | None  # None: from the first version
    upper: str | None  # None: unbounded
    upper_inclusive: bool  # True for last_affected

    def contains(self, version: ComparableVersion) -> bool:
        if self.introduced is not None and version < ComparableVersion(self.introduced):
            return False
        if self.upper is None:
            return True
        upper = ComparableVersion(self.upper)
        return version <= upper if self.upper_inclusive else version < upper


def _event_key(event: dict) -> tuple:
    kind, value = next(iter(event.items()))
    if kind == "introduced" and value == "0":
        return (0, ComparableVersion(""), 0)
    order = {"introduced": 0, "fixed": 1, "last_affected": 1}.get(kind, 2)
    return (1, ComparableVersion(str(value)), order)


def intervals(events: list[dict]) -> list[Interval]:
    """Affected intervals of one ECOSYSTEM range. Events are sorted as the OSV schema requires."""
    usable = [e for e in events if isinstance(e, dict) and len(e) == 1
              and next(iter(e)) in ("introduced", "fixed", "last_affected")]  # fmt: skip
    result: list[Interval] = []
    start: str | None = None
    is_open = False
    for event in sorted(usable, key=_event_key):
        kind, value = next(iter(event.items()))
        value = str(value)
        if kind == "introduced":
            if not is_open:
                start, is_open = (None if value == "0" else value), True
        elif is_open:
            result.append(Interval(start, value, kind == "last_affected"))
            is_open = False
    if is_open:
        result.append(Interval(start, None, False))
    return result


@dataclass(frozen=True)
class AffectedEntry:
    ranges: tuple[tuple[Interval, ...], ...]
    versions: frozenset[str]


def match(entries: Iterable[AffectedEntry], version: str) -> tuple[bool, str | None, str | None]:
    """Whether any entry affects ``version``, with the lowest introduced and highest fixed bound
    of the matching intervals (fixed is None if any matching interval is unbounded)."""
    target = ComparableVersion(version)
    matched: list[Interval] = []
    listed = False
    for entry in entries:
        listed = listed or version in entry.versions
        for range_intervals in entry.ranges:
            matched.extend(i for i in range_intervals if i.contains(target))
    if not matched:
        return listed, None, None
    introduced_bounds = [i.introduced for i in matched]
    introduced = None if None in introduced_bounds else min(introduced_bounds, key=ComparableVersion)
    if any(i.upper is None or i.upper_inclusive for i in matched):
        fixed = None
    else:
        fixed = max((i.upper for i in matched), key=ComparableVersion)
    return True, introduced, fixed


# ---------------------------------------------------------------------------------------------
# Ingest


def _maven_ranges(affected: dict) -> list[list[dict]]:
    return [r.get("events") or [] for r in affected.get("ranges") or [] if r.get("type") == "ECOSYSTEM"]


def build_index(zip_path: Path, index_path: Path) -> dict:
    """Parse every advisory in an OSV bulk zip into a SQLite index. Returns ingest statistics."""
    stats = {"advisories": 0, "affected_entries": 0, "withdrawn": 0, "malformed": 0, "malformed_files": []}
    conn = sqlite3.connect(index_path)
    try:
        conn.executescript(_SCHEMA)
        with zipfile.ZipFile(zip_path) as archive:
            for name in sorted(archive.namelist()):
                if not name.endswith(".json"):
                    continue
                try:
                    advisory = json.loads(archive.read(name))
                    if not isinstance(advisory, dict) or not isinstance(advisory.get("id"), str):
                        raise ValueError("not an OSV advisory object")
                except ValueError:
                    stats["malformed"] += 1
                    stats["malformed_files"].append(name)
                    continue
                if advisory.get("withdrawn"):
                    stats["withdrawn"] += 1
                    continue
                _insert_advisory(conn, advisory, stats)
        conn.commit()
    finally:
        conn.close()
    return stats


def _insert_advisory(conn: sqlite3.Connection, advisory: dict, stats: dict) -> None:
    severity = extract_severity(advisory)
    conn.execute(
        "INSERT INTO advisory VALUES (?, ?, ?, ?, ?, ?, ?)",
        (advisory["id"], json.dumps(extract_cves(advisory)), advisory.get("modified"),
         severity.rating.name, severity.source.value, severity.score, severity.vector),
    )  # fmt: skip
    stats["advisories"] += 1
    for affected in advisory.get("affected") or []:
        package = affected.get("package") or {}
        if package.get("ecosystem") != _ECOSYSTEM or not package.get("name"):
            continue
        versions = sorted(v for v in affected.get("versions") or [] if isinstance(v, str))
        conn.execute(
            "INSERT INTO affected VALUES (?, ?, ?, ?)",
            (advisory["id"], package["name"], json.dumps(_maven_ranges(affected)), json.dumps(versions)),
        )
        stats["affected_entries"] += 1


def sync(
    state_dir: Path,
    fetcher: Fetcher,
    clock: Callable[[], datetime.datetime],
    url: str = OSV_MAVEN_URL,
) -> SnapshotInfo:
    """Download the OSV Maven bulk dump and publish it as a new snapshot."""
    fetched_at = clock()
    with SnapshotWriter(state_dir, SOURCE) as writer:
        raw = writer.add_raw(RAW_FILE)
        fetcher.download(url, raw)
        stats = build_index(raw, writer.path / INDEX_FILE)
        return writer.commit(fetched_at, [url], stats)


# ---------------------------------------------------------------------------------------------
# Query


class LocalOsvAdvisorySource:
    """AdvisorySource over one OSV snapshot's index. Read-only."""

    def __init__(self, snapshot: SnapshotInfo) -> None:
        self._snapshot = snapshot
        index = snapshot.path / INDEX_FILE
        if not index.is_file():
            raise FileNotFoundError(f"OSV snapshot {snapshot.id} has no index at {index}")
        self._conn = sqlite3.connect(f"{index.as_uri()}?mode=ro", uri=True)
        self._cache: dict[str, tuple] = {}

    @property
    def snapshot_id(self) -> str:
        return self._snapshot.id

    def close(self) -> None:
        self._conn.close()

    def _advisories(self, package: str) -> tuple:
        if package not in self._cache:
            self._cache[package] = self._load(package)
        return self._cache[package]

    def _load(self, package: str) -> tuple:
        rows = self._conn.execute(
            "SELECT a.id, a.cves, a.severity_rating, a.severity_source, a.severity_score, "
            "a.severity_vector, f.ranges, f.versions FROM affected f JOIN advisory a ON a.id = f.advisory_id "
            "WHERE f.package = ? ORDER BY a.id",
            (package,),
        ).fetchall()
        grouped: dict[str, list] = {}
        for advisory_id, cves, rating, source, score, vector, ranges, versions in rows:
            entry = AffectedEntry(
                tuple(tuple(intervals(events)) for events in json.loads(ranges)),
                frozenset(json.loads(versions)),
            )
            if advisory_id not in grouped:
                severity = Severity(SeverityRating[rating], SeveritySource(source), score, vector)
                grouped[advisory_id] = [tuple(json.loads(cves)), severity, []]
            grouped[advisory_id][2].append(entry)
        return tuple((aid, cves, sev, tuple(entries)) for aid, (cves, sev, entries) in grouped.items())

    def affecting(self, coordinate: Coordinate, version: str) -> list[Finding]:
        findings = []
        for advisory_id, cves, severity, entries in self._advisories(str(coordinate)):
            affected, introduced, fixed = match(entries, version)
            if affected:
                findings.append(Finding(advisory_id, cves, severity, coordinate, version, introduced, fixed))
        return findings
