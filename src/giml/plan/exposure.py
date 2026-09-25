"""CVE exposure of a resolved dependency tree (spec sections 8.2 and 8.5).

Every dependency in the reactor's resolved trees, at the version it resolves to, is matched against
one OSV snapshot. Nothing here fetches data: the advisory source is a local snapshot (section 7.3).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from giml.core.interfaces import AdvisorySource
from giml.core.model import Coordinate, Finding, SeverityRating
from giml.maven.tree import ModuleTree
from giml.maven.version import ComparableVersion


@dataclass(frozen=True)
class Exposure:
    """What is left open: the worst severity, how many vulnerabilities have it, and how many in all.

    A vulnerability is an advisory, counted once however many artifacts or versions it affects.
    ``key`` orders exposures as spec 8.5 criterion 1 does (lower is better); a clean tree beats
    everything, and an advisory of unknown severity sorts below LOW because it cannot be rated.
    """

    max_severity: SeverityRating | None  # None when nothing is open
    at_max: int
    total: int

    @property
    def key(self) -> tuple[int, int, int]:
        if self.max_severity is None:
            return (-1, 0, 0)
        return (int(self.max_severity), self.at_max, self.total)


def exposure_of(findings: Iterable[Finding]) -> Exposure:
    worst: dict[str, SeverityRating] = {}
    for finding in findings:
        rating = finding.severity.rating
        worst[finding.advisory_id] = max(worst.get(finding.advisory_id, rating), rating)
    if not worst:
        return Exposure(None, 0, 0)
    top = max(worst.values())
    return Exposure(top, sum(1 for rating in worst.values() if rating == top), len(worst))


@dataclass(frozen=True)
class ResolvedDependency:
    """One artifact at one resolved version, across every module of the reactor."""

    coordinate: Coordinate
    version: str
    modules: tuple[Coordinate, ...]  # the modules whose trees contain it
    scopes: tuple[str, ...]
    direct: bool  # a direct dependency of at least one module
    findings: tuple[Finding, ...]  # advisories affecting this version


@dataclass(frozen=True)
class TreeExposure:
    dependencies: tuple[ResolvedDependency, ...]  # by coordinate, then Maven version order
    exposure: Exposure
    snapshot_id: str  # the OSV snapshot the findings come from (section 7.3)

    @property
    def vulnerable(self) -> tuple[ResolvedDependency, ...]:
        return tuple(d for d in self.dependencies if d.findings)


def resolve_exposure(trees: Sequence[ModuleTree], advisories: AdvisorySource) -> TreeExposure:
    """Match every resolved dependency of the reactor against the advisory snapshot.

    The reactor's own modules are not dependencies giml can upgrade, so they are left out.
    """
    own_modules = {tree.coordinate for tree in trees}
    seen: dict[tuple[Coordinate, str], dict] = {}
    for tree in trees:
        for dependency in tree.dependencies:
            if dependency.coordinate in own_modules:
                continue
            entry = seen.setdefault((dependency.coordinate, dependency.version),
                                    {"modules": set(), "scopes": set(), "direct": False})  # fmt: skip
            entry["modules"].add(tree.coordinate)
            entry["direct"] = entry["direct"] or dependency.direct
            if dependency.scope:
                entry["scopes"].add(dependency.scope)
    resolved = []
    for (coordinate, version), entry in sorted(seen.items(), key=lambda item: _order(*item[0])):
        findings = tuple(advisories.affecting(coordinate, version))
        resolved.append(ResolvedDependency(coordinate, version, tuple(sorted(entry["modules"])),
                                           tuple(sorted(entry["scopes"])), entry["direct"], findings))  # fmt: skip
    return TreeExposure(tuple(resolved), exposure_of(f for d in resolved for f in d.findings), advisories.snapshot_id)


def _order(coordinate: Coordinate, version: str) -> tuple:
    return (str(coordinate), ComparableVersion(version), version)
