"""Domain value types shared across giml."""

from __future__ import annotations

import datetime
import enum
import re
from dataclasses import dataclass
from pathlib import Path

_COORDINATE_PART = re.compile(r"^[A-Za-z0-9_.\-]+$")


@dataclass(frozen=True, order=True)
class Coordinate:
    """A Maven ``groupId:artifactId`` (no version)."""

    group_id: str
    artifact_id: str

    def __post_init__(self) -> None:
        for part in (self.group_id, self.artifact_id):
            if not _COORDINATE_PART.match(part):
                raise ValueError(f"invalid Maven coordinate part {part!r}")

    @classmethod
    def parse(cls, text: str) -> Coordinate:
        parts = text.strip().split(":")
        if len(parts) != 2:
            raise ValueError(f"expected groupId:artifactId, got {text!r}")
        return cls(parts[0], parts[1])

    def __str__(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"


class SeverityRating(enum.IntEnum):
    """Ordered so that ``max()`` gives the worst rating."""

    UNKNOWN = 0
    NONE = 1
    LOW = 2
    MEDIUM = 3
    HIGH = 4
    CRITICAL = 5

    @classmethod
    def from_score(cls, score: float) -> SeverityRating:
        # CVSS v3.x qualitative severity scale.
        if score == 0:
            return cls.NONE
        if score < 4.0:
            return cls.LOW
        if score < 7.0:
            return cls.MEDIUM
        if score < 9.0:
            return cls.HIGH
        return cls.CRITICAL

    @classmethod
    def from_label(cls, label: str) -> SeverityRating:
        normalised = label.strip().upper()
        if normalised == "MODERATE":  # GHSA's name for MEDIUM
            return cls.MEDIUM
        return cls.__members__.get(normalised, cls.UNKNOWN)


class SeveritySource(enum.StrEnum):
    CVSS_V3 = "cvss_v3"
    LABEL = "label"
    NONE = "none"


@dataclass(frozen=True)
class Severity:
    rating: SeverityRating
    source: SeveritySource
    score: float | None = None
    vector: str | None = None


@dataclass(frozen=True)
class Finding:
    """One advisory affecting one version of one coordinate."""

    advisory_id: str
    cves: tuple[str, ...]
    severity: Severity
    coordinate: Coordinate
    version: str
    introduced: str | None
    fixed: str | None


@dataclass(frozen=True)
class VersionRelease:
    version: str
    released_at: datetime.datetime | None


@dataclass(frozen=True)
class SnapshotInfo:
    id: str
    source: str
    fetched_at: datetime.datetime
    content_hash: str
    path: Path
