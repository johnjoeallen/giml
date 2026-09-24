"""Maven Central version metadata and release dates (spec section 7.2).

Versions come from ``maven-metadata.xml`` (parsing ported from RedKite's
``HttpVersionMetadataProvider.parseVersions``). Release dates come from the ``Last-Modified``
header of each version's POM (decision Q10), fetched once and carried forward between snapshots
because a published release never changes.

Each snapshot covers every coordinate synced before plus the newly requested ones, so the latest
snapshot is always complete for planning.
"""

from __future__ import annotations

import datetime
import json
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from giml.core.model import Coordinate, SnapshotInfo, VersionRelease
from giml.data.http import Fetcher
from giml.data.snapshots import SnapshotWriter
from giml.maven.version import ComparableVersion
from giml.store.result_cache import canonical_json

SOURCE = "central"
CENTRAL_URL = "https://repo1.maven.org/maven2"
RAW_FILE = "central.json"
FOUND, NOT_FOUND = "found", "not_found"


class MetadataError(ValueError):
    """A maven-metadata.xml document cannot be parsed."""


def _path(coordinate: Coordinate) -> str:
    return f"{coordinate.group_id.replace('.', '/')}/{coordinate.artifact_id}"


def metadata_url(base: str, coordinate: Coordinate) -> str:
    return f"{base}/{_path(coordinate)}/maven-metadata.xml"


def pom_url(base: str, coordinate: Coordinate, version: str) -> str:
    return f"{base}/{_path(coordinate)}/{version}/{coordinate.artifact_id}-{version}.pom"


def parse_metadata(xml_text: str) -> list[str]:
    """Versions listed in maven-metadata.xml, plus ``release``/``latest`` if missing from the list."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise MetadataError(f"invalid maven-metadata.xml: {exc}") from exc
    versioning = root.find("versioning")
    if versioning is None:
        return []
    versions = [v.text.strip() for v in versioning.iterfind("versions/version") if v.text and v.text.strip()]
    for extra in ("release", "latest"):
        value = versioning.findtext(extra)
        if value and value.strip() and value.strip() not in versions:
            versions.append(value.strip())
    return versions


def _iso(moment: datetime.datetime | None) -> str | None:
    return moment.astimezone(datetime.UTC).isoformat() if moment else None


def load_raw(snapshot: SnapshotInfo | None) -> dict:
    if snapshot is None:
        return {}
    return json.loads((snapshot.path / RAW_FILE).read_text(encoding="utf-8"))


def sync(
    state_dir: Path,
    fetcher: Fetcher,
    clock: Callable[[], datetime.datetime],
    coordinates: Iterable[Coordinate],
    previous: SnapshotInfo | None,
    base_url: str = CENTRAL_URL,
    workers: int = 8,
) -> SnapshotInfo:
    """Fetch metadata and release dates and publish them as a new Central snapshot."""
    fetched_at = clock()
    prior = load_raw(previous)
    wanted = sorted(set(coordinates) | {Coordinate.parse(key) for key in prior})
    entries: dict[str, dict] = {}
    to_date: list[tuple[str, str]] = []
    reused = 0
    for coordinate in wanted:
        key = str(coordinate)
        xml_text = fetcher.get_text(metadata_url(base_url, coordinate))
        if xml_text is None:
            entries[key] = {"status": NOT_FOUND, "versions": {}}
            continue
        known = prior.get(key, {}).get("versions", {})
        versions: dict[str, str | None] = {}
        for version in parse_metadata(xml_text):
            if known.get(version):
                versions[version] = known[version]
                reused += 1
            else:
                versions[version] = None
                to_date.append((key, version))
        entries[key] = {"status": FOUND, "versions": versions}

    def fetch_date(item: tuple[str, str]) -> str | None:
        key, version = item
        return _iso(fetcher.last_modified(pom_url(base_url, Coordinate.parse(key), version)))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for (key, version), released in zip(to_date, pool.map(fetch_date, to_date), strict=True):
            entries[key]["versions"][version] = released

    stats = {
        "coordinates": len(wanted),
        "not_found": sum(1 for e in entries.values() if e["status"] == NOT_FOUND),
        "versions": sum(len(e["versions"]) for e in entries.values()),
        "release_dates_fetched": sum(1 for k, v in to_date if entries[k]["versions"][v]),
        "release_dates_reused": reused,
        "release_dates_missing": sum(1 for e in entries.values() for d in e["versions"].values() if d is None),
    }
    with SnapshotWriter(state_dir, SOURCE) as writer:
        writer.add_raw(RAW_FILE).write_text(canonical_json(entries) + "\n", encoding="utf-8")
        return writer.commit(fetched_at, [base_url], stats)


class LocalCentralMetadataSource:
    """ArtifactMetadataSource over one Central snapshot."""

    def __init__(self, snapshot: SnapshotInfo) -> None:
        self._snapshot = snapshot
        self._entries = load_raw(snapshot)

    @property
    def snapshot_id(self) -> str:
        return self._snapshot.id

    def versions(self, coordinate: Coordinate) -> list[VersionRelease] | None:
        entry = self._entries.get(str(coordinate))
        if entry is None or entry["status"] != FOUND:
            return None
        releases = [
            VersionRelease(version, datetime.datetime.fromisoformat(date) if date else None)
            for version, date in entry["versions"].items()
        ]
        return sorted(releases, key=lambda r: ComparableVersion(r.version))
