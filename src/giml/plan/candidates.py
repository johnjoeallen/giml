"""Upgrade candidates for one dependency (spec sections 8.2 and 8.7).

Pure planning: from the resolved dependency, the versions Central has, the advisory snapshot and the
run's strategy, scope and major-update mode, list what a build could try, in the order it would be
tried. Nothing is built or edited here. A candidate that a gate stops (a major update, section 8.7)
is still listed, with the reason, so the report can say what was held back and why.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Sequence
from dataclasses import dataclass

from giml.core.config import PlanningSettings
from giml.core.interfaces import AdvisorySource
from giml.core.model import Coordinate, VersionRelease
from giml.maven.declarations import EXTERNAL, Declaration, Site
from giml.maven.version import ComparableVersion
from giml.plan.exposure import ResolvedDependency

# Candidate kinds (section 8.2), in the order they are named when several land on one version.
LATEST, LATEST_IN_MAJOR = "latest", "latest_in_major"
CVE_MAJOR, CVE_MINOR, CVE_PATCH = "cve_major", "cve_minor", "cve_patch"
NEXT_MINOR, NEXT_PATCH = "next_minor", "next_patch"
_KIND_RANK = {k: i for i, k in enumerate((LATEST, LATEST_IN_MAJOR, CVE_MAJOR, CVE_MINOR, CVE_PATCH, NEXT_MINOR, NEXT_PATCH))}

# What the report says about a dependency.
FIX_AVAILABLE, FIX_BLOCKED_MAJOR, NO_FIX = "fix_available", "fix_blocked_major", "no_fix"
UPDATE_AVAILABLE, UP_TO_DATE, UNCHANGED_SCOPE, NO_METADATA = "update_available", "up_to_date", "unchanged_scope", "no_metadata"

# How a change would be made.
EDIT, PIN, UNSUPPORTED = "edit", "pin", "unsupported"

_NUMERIC_PREFIX = re.compile(r"\d+(?:\.\d+)*")


def _parts(version: str) -> tuple[int, ...]:
    match = _NUMERIC_PREFIX.match(version)
    return tuple(int(p) for p in match.group().split(".")) if match else ()


def level(current: str, candidate: str) -> str:
    """"patch", "minor" or "major": the first numeric component that differs (unparseable counts as major)."""
    before, after = _parts(current), _parts(candidate)
    if not before or not after:
        return "major"
    padded = zip(before + (0, 0), after + (0, 0), strict=False)
    first, second = next(padded), next(padded)
    if first[0] != first[1]:
        return "major"
    return "minor" if second[0] != second[1] else "patch"


def is_prerelease(version: str) -> bool:
    """Below its own numeric release in Maven ordering (1.0-rc1, 2.0-M1, 1.0-SNAPSHOT)."""
    match = _NUMERIC_PREFIX.match(version)
    return match is not None and ComparableVersion(version) < ComparableVersion(match.group())


@dataclass(frozen=True)
class Candidate:
    kinds: tuple[str, ...]  # every candidate kind that landed on this version
    version: str
    released_at: datetime.datetime | None
    date_unknown: bool  # no release date on record, so the cooldown could not be checked
    clears: bool | None  # fixes every advisory of the current version; None for a dependency without one
    remaining: tuple[str, ...]  # advisories of the current version that still affect this one
    new_advisories: tuple[str, ...]  # advisories that affect this version but not the current one
    blocked: str | None  # why a build will not try it (section 8.7); None when it may be tried


@dataclass(frozen=True)
class DependencyPlan:
    coordinate: Coordinate
    version: str  # the resolved version
    cve_affected: bool
    advisories: tuple[str, ...]  # advisory ids affecting the resolved version
    status: str
    change: str  # EDIT (a declaration to edit), PIN (a dependencyManagement pin), UNSUPPORTED
    sites: tuple[Site, ...]  # what an edit would replace
    candidates: tuple[Candidate, ...]  # ladder order: conservative climbs, latest is demoted from the newest
    held_by_cooldown: tuple[str, ...]  # newer versions skipped because they are younger than the cooldown


def _change(declarations: Sequence[Declaration]) -> tuple[str, tuple[Site, ...]]:
    sites = tuple(dict.fromkeys(d.site for d in declarations if d.site is not None))
    if sites:
        return EDIT, sites
    origins = {d.origin for d in declarations}
    if not declarations or origins == {EXTERNAL}:
        return PIN, ()
    return UNSUPPORTED, ()


class _Ladder:
    """The versions newer than the current one that a build may try, oldest first."""

    def __init__(self, dependency: ResolvedDependency, releases: Sequence[VersionRelease],
                 advisories: AdvisorySource, options: PlanningSettings, now: datetime.datetime) -> None:  # fmt: skip
        self.current, self.advisories, self.options = dependency.version, advisories, options
        self.coordinate = dependency.coordinate
        self.current_ids = frozenset(f.advisory_id for f in dependency.findings)
        self.released: dict[str, datetime.datetime | None] = {}
        self.held: list[str] = []
        cooldown = datetime.timedelta(days=options.release_cooldown_days)
        current_key, current_pre = ComparableVersion(self.current), is_prerelease(self.current)
        self.pool: list[str] = []
        for release in sorted(releases, key=lambda r: (ComparableVersion(r.version), r.version)):
            if ComparableVersion(release.version) <= current_key or (is_prerelease(release.version) and not current_pre):
                continue
            if release.released_at is not None and now - release.released_at < cooldown:
                self.held.append(release.version)
                continue
            self.pool.append(release.version)
            self.released[release.version] = release.released_at
        self._found: dict[str, tuple[frozenset[str], frozenset[str]]] = {}

    def advisories_of(self, version: str) -> tuple[frozenset[str], frozenset[str]]:
        """(current advisories still affecting it, advisories that affect only it)."""
        if version not in self._found:
            ids = frozenset(f.advisory_id for f in self.advisories.affecting(self.coordinate, version))
            self._found[version] = (ids & self.current_ids, ids - self.current_ids)
        return self._found[version]

    def clears(self, version: str) -> bool:
        return not self.advisories_of(version)[0]

    def at(self, wanted: str) -> list[str]:
        return [v for v in self.pool if level(self.current, v) == wanted]

    def lowest_clearing(self, wanted: str) -> str | None:
        return next((v for v in self.at(wanted) if self.clears(v)), None)

    def newest(self, in_major_only: bool) -> str | None:
        versions = [v for v in self.pool if not in_major_only or level(self.current, v) != "major"]
        return versions[-1] if versions else None

    def next_step(self) -> tuple[str, str] | None:
        for kind, wanted in ((NEXT_PATCH, "patch"), (NEXT_MINOR, "minor")):
            if (found := self.at(wanted)):
                return kind, found[0]
        return None


def _major_block(mode: str) -> str:
    return f"major update: major_updates is {mode}" + (" and there is no ML evidence" if mode == "ml" else "")


def plan_dependency(
    dependency: ResolvedDependency,
    declarations: Sequence[Declaration],
    releases: Sequence[VersionRelease] | None,
    advisories: AdvisorySource,
    options: PlanningSettings,
    now: datetime.datetime,
) -> DependencyPlan:
    """The candidates for one resolved dependency, in the order a build would try them.

    Under ``conservative`` they climb from the current version (patch, then minor, then major for a
    CVE fix; the next patch or minor for a general update) and a build stops at the first that
    passes, so a later step is tried only when an earlier one fails. Under ``latest`` they run from
    the newest version down to the lowest CVE fix. A dependency without a CVE has candidates only
    under ``scope: general``; under ``scope: cve`` it moves only when forced (spec 8.2).
    """
    cve_affected = bool(dependency.findings)
    change, sites = _change(declarations)
    ids = tuple(sorted({f.advisory_id for f in dependency.findings}))
    base = {"coordinate": dependency.coordinate, "version": dependency.version, "cve_affected": cve_affected,
            "advisories": ids, "change": change, "sites": sites}  # fmt: skip
    in_scope = cve_affected or options.scope == "general"
    if releases is None:
        status = NO_METADATA if in_scope else UNCHANGED_SCOPE  # out of scope, its versions are never needed
        return DependencyPlan(**base, status=status, candidates=(), held_by_cooldown=())
    ladder = _Ladder(dependency, releases, advisories, options, now)
    kinds: dict[str, list[str]] = {}

    def add(kind: str, version: str | None) -> None:
        if version is not None:
            kinds.setdefault(version, []).append(kind)

    if in_scope and options.strategy == "latest":
        add(LATEST, ladder.newest(False))
        add(LATEST_IN_MAJOR, ladder.newest(True))
    if cve_affected:
        for kind, wanted in ((CVE_PATCH, "patch"), (CVE_MINOR, "minor"), (CVE_MAJOR, "major")):
            add(kind, ladder.lowest_clearing(wanted))
    elif in_scope and options.strategy == "conservative" and (step := ladder.next_step()) is not None:
        add(*step)

    candidates = []
    for version in sorted(kinds, key=ComparableVersion, reverse=options.strategy == "latest"):
        remaining, new = ladder.advisories_of(version) if cve_affected else (frozenset(), frozenset())
        blocked = _major_block(options.major_updates) \
            if level(dependency.version, version) == "major" and options.major_updates != "allowed" else None  # fmt: skip
        candidates.append(Candidate(tuple(sorted(kinds[version], key=_KIND_RANK.__getitem__)), version,
                                    ladder.released[version], ladder.released[version] is None,
                                    (not remaining) if cve_affected else None, tuple(sorted(remaining)),
                                    tuple(sorted(new)), blocked))  # fmt: skip
    return DependencyPlan(**base, status=_status(cve_affected, in_scope, candidates), candidates=tuple(candidates),
                          held_by_cooldown=tuple(ladder.held))  # fmt: skip


def _status(cve_affected: bool, in_scope: bool, candidates: list[Candidate]) -> str:
    if cve_affected:
        fixes = [c for c in candidates if c.clears]
        if any(c.blocked is None for c in fixes):
            return FIX_AVAILABLE
        return FIX_BLOCKED_MAJOR if fixes else NO_FIX
    if not in_scope:
        return UNCHANGED_SCOPE
    return UPDATE_AVAILABLE if candidates else UP_TO_DATE
