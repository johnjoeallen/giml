"""Parent and BOM upgrades as one change unit each (spec section 8.2), evaluated by resolution.

A Spring Boot style parent supplies the versions of many dependencies at once, so what an upgrade
is worth cannot be read off one artifact's advisories. Each candidate version is evaluated by
resolving the reactor's trees with that version in place (in the throwaway worktree, never the
developer's checkout) and comparing the CVE exposure with today's. Resolution only reads POMs: nothing
is compiled or tested, so this is analysis, not verification, and every result is a candidate.
"""

from __future__ import annotations

import contextlib
import datetime
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from giml.core.config import PlanningSettings
from giml.core.model import Coordinate, VersionRelease
from giml.maven.declarations import Declarations, Site
from giml.maven.tree import ResolutionError
from giml.plan.candidates import eligible_versions, level, major_of, only_in_test_scope
from giml.plan.exposure import Exposure, TreeExposure

MAX_EVALUATIONS = 40  # resolutions per change unit, nearest versions first

IMPROVES, NO_IMPROVEMENT, BLOCKED_MAJOR = "improves", "no_improvement", "blocked_major"
NO_METADATA, NOT_EVALUATED = "no_metadata", "not_evaluated"

_LEVELS = ("patch", "minor", "major")


@dataclass(frozen=True)
class ChangeUnit:
    kind: str  # "parent" or "bom"
    coordinate: Coordinate
    version: str
    site: Site  # the text that holds the version


@dataclass(frozen=True)
class Evaluation:
    """What the reactor's tree looks like with one candidate version of the unit."""

    version: str
    level: str  # patch, minor or major relative to the unit's current version
    released_at: datetime.datetime | None
    exposure: Exposure | None  # None when the tree could not be resolved
    cleared: tuple[str, ...]  # advisories of today's tree that are gone
    introduced: tuple[str, ...]  # advisories only this version's tree has
    changed: int  # dependencies whose resolved version differs from today's
    blocked: str | None  # major-update gate, applied to the resolved tree (spec 8.7)
    failed: str | None  # why resolution failed


@dataclass(frozen=True)
class Pick:
    kind: str  # e.g. parent_patch, parent_minor, bom_patch, or parent_latest for the aspirational version
    version: str


@dataclass(frozen=True)
class UnitPlan:
    unit: ChangeUnit
    status: str
    current: Exposure
    evaluations: tuple[Evaluation, ...]  # ascending, only versions that were resolved
    picks: tuple[Pick, ...]  # in the order a build would try them
    held_by_cooldown: tuple[str, ...]
    truncated: bool  # more versions exist than the limit allowed to evaluate
    skipped_majors: int  # major versions not evaluated because major updates are not permitted
    note: str = ""


def change_units(declarations: Declarations) -> list[ChangeUnit]:
    """External parents and editable BOM imports: the declarations that move many versions at once."""
    units = [ChangeUnit("parent", p.coordinate, p.version, p.site) for p in declarations.parents]
    units += [ChangeUnit("bom", d.coordinate, d.version, d.site) for d in declarations.declared
              if d.is_bom and d.site is not None and d.version]  # fmt: skip
    return list(dict.fromkeys(units))


@contextlib.contextmanager
def with_version(site: Site, version: str, expected: str | None = None) -> Iterator[None]:
    """Write ``version`` over the site's text and put the original back afterwards, even on failure."""
    original = site.pom.read_text(encoding="utf-8")
    start, end = site.span
    if expected is not None and original[start:end] != expected:
        raise ValueError(f"{site.pom}:{site.line}: the version text does not hold {expected}")
    site.pom.write_text(original[:start] + version + original[end:], encoding="utf-8")
    try:
        yield
    finally:
        site.pom.write_text(original, encoding="utf-8")


def not_evaluated(unit: ChangeUnit, current: Exposure, note: str) -> UnitPlan:
    return UnitPlan(unit, NOT_EVALUATED, current, (), (), (), False, 0, note)


def _advisories(tree: TreeExposure) -> frozenset[str]:
    return frozenset(f.advisory_id for d in tree.dependencies for f in d.findings)


def _versions(tree: TreeExposure) -> dict[Coordinate, frozenset[str]]:
    found: dict[Coordinate, set[str]] = {}
    for dependency in tree.dependencies:
        found.setdefault(dependency.coordinate, set()).add(dependency.version)
    return {c: frozenset(v) for c, v in found.items()}


def _test_only(tree: TreeExposure) -> frozenset[Coordinate]:
    scopes: dict[Coordinate, set[str]] = {}
    for dependency in tree.dependencies:
        scopes.setdefault(dependency.coordinate, set()).update(dependency.scopes)
    return frozenset(c for c, found in scopes.items() if only_in_test_scope(sorted(found)))


def _major_change(before: dict[Coordinate, frozenset[str]], after: dict[Coordinate, frozenset[str]],
                  options: PlanningSettings, test_only: frozenset[Coordinate]) -> str | None:  # fmt: skip
    """The first dependency whose major version differs between the trees, worded as a gate reason.

    A dependency that is test-only in both trees passes when ``major_updates_test_scope`` allows it;
    otherwise the reason says it is test-only, so the option is easy to find.
    """
    exempt = test_only if options.major_updates_test_scope == "allowed" else frozenset()
    for coordinate in sorted(before.keys() & after.keys()):
        old, new = {major_of(v) for v in before[coordinate]}, {major_of(v) for v in after[coordinate]}
        if old != new and coordinate not in exempt:
            side = lambda majors: "/".join(f"{m}.x" for m in sorted(majors))  # noqa: E731
            text = f"major update: changes {coordinate} from {side(old)} to {side(new)}; major_updates is {options.major_updates}"
            text += " and there is no ML evidence" if options.major_updates == "ml" else ""
            return text + (" (test scope only; `major_updates_test_scope` is `disallowed`)" if coordinate in test_only else "")
    return None


def _evaluate(version: str, unit: ChangeUnit, current: TreeExposure, resolve: Callable[[str], TreeExposure],
              released: dict, options: PlanningSettings) -> Evaluation:  # fmt: skip
    base = {"version": version, "level": level(unit.version, version), "released_at": released[version]}
    try:
        tree = resolve(version)
    except ResolutionError as exc:
        return Evaluation(**base, exposure=None, cleared=(), introduced=(), changed=0, blocked=None, failed=str(exc))
    before, after = _versions(current), _versions(tree)
    blocked = None if options.major_updates == "allowed" else \
        _major_change(before, after, options, _test_only(current) & _test_only(tree))  # fmt: skip
    return Evaluation(**base, exposure=tree.exposure, cleared=tuple(sorted(_advisories(current) - _advisories(tree))),
                      introduced=tuple(sorted(_advisories(tree) - _advisories(current))),
                      changed=sum(1 for c in before.keys() & after.keys() if before[c] != after[c]),
                      blocked=blocked, failed=None)  # fmt: skip


def plan_change_unit(
    unit: ChangeUnit,
    releases: Sequence[VersionRelease] | None,
    current: TreeExposure,
    resolve: Callable[[str], TreeExposure],
    options: PlanningSettings,
    now: datetime.datetime,
    limit: int = MAX_EVALUATIONS,
) -> UnitPlan:
    """Evaluate the unit's newer versions and pick what a build should try.

    ``resolve`` returns the reactor's resolved, advisory-matched tree with the given version in place.
    A change is worth trying when the exposure (spec 8.5, criterion 1) gets lower. ``conservative``
    picks the smallest step that reaches the best exposure any usable version delivers: the lowest
    such version of the lowest level (patch, minor, major) that has one. ``latest`` also aims for the
    newest usable version and keeps that pick as the version to demote to.
    """
    now_exposure = current.exposure
    if now_exposure.total == 0:
        return not_evaluated(unit, now_exposure, "no CVE to fix")
    if releases is None:
        return UnitPlan(unit, NO_METADATA, now_exposure, (), (), (), False, 0)
    pool, released, held = eligible_versions(unit.version, releases, options.release_cooldown_days, now)
    gated = options.major_updates != "allowed"
    candidates = [v for v in pool if not (gated and level(unit.version, v) == "major")]
    todo = candidates[:limit]
    evaluations = tuple(_evaluate(v, unit, current, resolve, released, options) for v in todo)
    usable = [e for e in evaluations if e.exposure is not None and e.blocked is None]
    better = [e for e in usable if e.exposure.key < now_exposure.key]
    picks: tuple[Pick, ...] = ()
    if better:
        best = min(e.exposure.key for e in better)
        floor = next(e for lvl in _LEVELS for e in usable if e.level == lvl and e.exposure.key == best)
        picks = (Pick(f"{unit.kind}_{floor.level}", floor.version),)
        if options.strategy == "latest" and usable[-1].version != floor.version:
            picks = (Pick(f"{unit.kind}_latest", usable[-1].version), *picks)
        status = IMPROVES
    elif any(e.blocked and e.exposure is not None and e.exposure.key < now_exposure.key for e in evaluations):
        status = BLOCKED_MAJOR
    else:
        status = NO_IMPROVEMENT
    return UnitPlan(unit, status, now_exposure, evaluations, picks, tuple(held), len(candidates) > limit,
                    len(pool) - len(candidates))  # fmt: skip
