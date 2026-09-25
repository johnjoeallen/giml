"""Turn an analysis into ladders of concrete changes for the search (spec sections 8.2, 8.3 and 8.4).

A dependency with a CVE becomes a ladder whose steps are its candidates that clear it, each with the
exact edits that make the step: a version edited where it is declared, or, when nothing in the reactor
declares the version that resolved, a ``dependencyManagement`` pin in the root POM. Dependencies that
share one declaration (a property used by several artifacts) are one ladder with one edit, and a
step must clear all of them. A parent or BOM change unit becomes a ladder of the versions its
evaluation found best. A ladder with no usable step keeps the reason, so the report can say why.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from giml.core.model import Coordinate, Finding
from giml.maven.declarations import Declarations, Site
from giml.maven.pom_change import AddPin, Change, SetVersion
from giml.maven.version import ComparableVersion
from giml.plan.analysis import Analysis
from giml.plan.candidates import EDIT, NEXT_MINOR, NEXT_PATCH, PIN, UPDATE_AVAILABLE, DependencyPlan, level
from giml.plan.search import Ladder, Step

_LEVELS = ("patch", "minor", "major")
_CVE_KINDS = {"cve_patch": "patch", "cve_minor": "minor", "cve_major": "major"}


@dataclass(frozen=True)
class Move:
    """What a step does: the edits, and the advisories it is expected to clear."""

    changes: tuple[Change, ...]
    clears: tuple[str, ...]


@dataclass(frozen=True)
class Proposal:
    ladder: Ladder
    moves: tuple[Move, ...]  # aligned with ladder.steps
    members: tuple[str, ...]  # the coordinates the ladder is about
    kind: str  # "dependency" or "unit"
    blocked: tuple[str, ...] = field(default=())  # reasons some candidates were left out
    resolves: tuple[str, ...] = field(default=())  # enforcer violations (identities) a step must remove to count as passing


def site_text(site: Site) -> str:
    """The text currently at a site, read raw so its span is exact."""
    with site.pom.open(encoding="utf-8", newline="") as handle:
        return handle.read()[site.span[0] : site.span[1]]


def _edit_sites(plan: DependencyPlan, declarations: Declarations) -> tuple[Site, ...]:
    """The declarations to edit: those whose effective version is the one that resolved."""
    found: dict[tuple, Site] = {}
    for declaration in declarations.for_coordinate(plan.coordinate):
        if declaration.site is not None and declaration.version == plan.version:
            found.setdefault((declaration.site.pom, declaration.site.span), declaration.site)
    return tuple(found.values())


def _labels(findings: Sequence[Finding]) -> str:
    labels = sorted({cve for f in findings for cve in (f.cves or (f.advisory_id,))})
    return ", ".join(labels) if len(labels) <= 3 else f"{', '.join(labels[:3])} and {len(labels) - 3} more"


def _why_no_step(plan: DependencyPlan, mode: str) -> str:
    if plan.status == "fix_blocked_major":
        blocked = min((c for c in plan.candidates if c.clears), key=lambda c: ComparableVersion(c.version))
        return f"the only fix is a major update ({blocked.version}); {blocked.blocked}"
    if plan.status == "no_metadata":
        return f"no Central metadata for {plan.coordinate}; run `giml sync --central --coordinate {plan.coordinate}`"
    if plan.held_by_cooldown:
        return f"no usable version clears it; {', '.join(plan.held_by_cooldown)} are inside the release cooldown"
    return "no version that clears it is available"


def _group_steps(members: list[DependencyPlan]) -> list[tuple[str, str]]:
    """(kind, version) per level: the lowest version that clears every member.

    A member's advisories are fixed from some version on, so the highest of the members' own lowest
    fixes at a level clears them all; the real build and the final exposure check confirm it.
    """
    usable = [{c.version: c for c in plan.candidates if c.blocked is None and c.clears} for plan in members]
    if any(not u for u in usable):
        return []
    steps: list[tuple[str, str]] = []
    for wanted in _LEVELS:
        per_member = [[c for c in u.values() if _CVE_KINDS.get(c.kinds[0]) in _LEVELS[: _LEVELS.index(wanted) + 1]] for u in usable]
        if any(not m for m in per_member) or not any(_CVE_KINDS.get(c.kinds[0]) == wanted for m in per_member for c in m):
            continue
        version = max((c.version for m in per_member for c in m), key=ComparableVersion)
        if version not in {v for _, v in steps}:
            steps.append((f"cve_{wanted}", version))
    return steps


def _severity(plan: DependencyPlan, analysis: Analysis) -> int:
    resolved = next((d for d in analysis.exposure.dependencies if (d.coordinate, d.version) == (plan.coordinate, plan.version)), None)
    return max((int(f.severity.rating) for f in resolved.findings), default=0) if resolved else 0


def _findings(plan: DependencyPlan, analysis: Analysis) -> tuple[Finding, ...]:
    resolved = next((d for d in analysis.exposure.dependencies if (d.coordinate, d.version) == (plan.coordinate, plan.version)), None)
    return resolved.findings if resolved else ()


def _short(members: list[DependencyPlan]) -> str:
    return str(members[0].coordinate) if len(members) == 1 else "+".join(m.coordinate.artifact_id for m in members)


def _group_proposal(members: list[DependencyPlan], sites: tuple[Site, ...], analysis: Analysis, mode: str, root_pom: Path) -> Proposal:
    members = sorted(members, key=lambda m: str(m.coordinate))
    coordinates = "+".join(str(m.coordinate) for m in members)
    key = f"dep:{coordinates}@{min((m.version for m in members), key=ComparableVersion)}"
    unsupported = [m for m in members if m.change == "unsupported"]
    steps_data = [] if unsupported else _group_steps(members)
    note, blocked = "", tuple(sorted({c.blocked for m in members for c in m.candidates if c.blocked}))
    if unsupported:
        note = "giml cannot edit where this version comes from"
    elif not steps_data:
        note = _why_no_step(next((m for m in members if m.status != "fix_available"), members[0]), mode) if len(members) == 1 else \
            f"no version clears every dependency that shares this declaration ({', '.join(str(m.coordinate) for m in members)})"  # fmt: skip
    findings = tuple(f for m in members for f in _findings(m, analysis))
    advisories = tuple(sorted({a for m in members for a in m.advisories}))
    current = min((m.version for m in members), key=ComparableVersion)
    label = _short(members)
    steps, moves = [], []
    for rank, (kind, version) in enumerate(steps_data):
        steps.append(Step(key, f"{label} {current} → {version} ({kind})", kind, rank))
        if sites:
            changes: tuple[Change, ...] = tuple(SetVersion(site, site_text(site), version) for site in sites)
        else:
            changes = tuple(AddPin(root_pom, m.coordinate, version, f"{_labels(findings)}; re-evaluate on a new release") for m in members[:1])
        moves.append(Move(changes, advisories))
    rating = max((_severity(m, analysis) for m in members), default=0)
    ladder = Ladder(key, tuple(steps), -rating, note)  # the order is fixed up by the caller, worst first
    return Proposal(ladder, tuple(moves), tuple(str(m.coordinate) for m in members), "dependency", blocked)


def _general_proposal(members: list[DependencyPlan], sites: tuple[Site, ...]) -> Proposal | None:
    """A one-step ladder for a general update: the next patch or minor, when every member agrees on it."""
    members = sorted(members, key=lambda m: str(m.coordinate))
    usable = [next((c for c in m.candidates if c.blocked is None and c.kinds[0] in (NEXT_PATCH, NEXT_MINOR)), None) for m in members]
    if any(c is None for c in usable) or len({(c.version, c.kinds[0]) for c in usable}) != 1:
        return None
    version, kind = usable[0].version, usable[0].kinds[0]
    key = f"upd:{'+'.join(str(m.coordinate) for m in members)}@{members[0].version}"
    label = f"{_short(members)} {members[0].version} → {version} ({kind})"
    changes = tuple(SetVersion(site, site_text(site), version) for site in sites)
    return Proposal(Ladder(key, (Step(key, label, kind, 0),), 0), (Move(changes, ()),), tuple(str(m.coordinate) for m in members), "dependency")


def dependency_proposals(analysis: Analysis, options, root_pom: Path) -> list[Proposal]:
    """Ladders for the CVE-affected dependencies, worst first; under ``scope: general`` then one step per other declared dependency."""
    groups: dict[object, list[DependencyPlan]] = {}
    sites_of: dict[object, tuple[Site, ...]] = {}
    general: dict[object, list[DependencyPlan]] = {}
    for plan in analysis.plans:
        sites = _edit_sites(plan, analysis.declarations) if plan.change == EDIT else ()
        if not plan.cve_affected:
            if plan.status == UPDATE_AVAILABLE and sites:
                general.setdefault(frozenset((s.pom, s.span) for s in sites), []).append(plan)
                sites_of.setdefault(frozenset((s.pom, s.span) for s in sites), sites)
            continue
        if plan.change in (EDIT, PIN) and sites:
            group = frozenset((s.pom, s.span) for s in sites)
        else:
            group = ("pin" if plan.change != "unsupported" else "unsupported", plan.coordinate)
        groups.setdefault(group, []).append(plan)
        sites_of[group] = sites
    proposals = [_group_proposal(members, sites_of[group], analysis, options.major_updates, root_pom) for group, members in groups.items()]
    proposals.sort(key=lambda p: (p.ladder.order, p.ladder.key))
    cve_sites = {group for group in groups if isinstance(group, frozenset)}
    extra = [] if options.scope != "general" else [
        p for group, members in sorted(general.items(), key=lambda item: str(item[1][0].coordinate))
        if not any(group & taken for taken in cve_sites) and (p := _general_proposal(members, sites_of[group])) is not None]
    return [Proposal(Ladder(p.ladder.key, p.ladder.steps, index, p.ladder.note), p.moves, p.members, p.kind, p.blocked)
            for index, p in enumerate([*proposals, *extra])]  # fmt: skip


def unit_proposals(analysis: Analysis) -> list[Proposal]:
    """Ladders for the parent and BOM change units that would improve the exposure."""
    proposals = []
    for unit_plan in sorted((u for u in analysis.unit_plans if u.status == "improves"), key=lambda u: str(u.unit.coordinate)):
        unit = unit_plan.unit
        floor = unit_plan.picks[-1]
        floor_eval = next(e for e in unit_plan.evaluations if e.version == floor.version)
        others = sorted((e for e in unit_plan.evaluations if e.blocked is None and e.exposure is not None and e.version != floor.version
                         and e.exposure.key == floor_eval.exposure.key and ComparableVersion(e.version) > ComparableVersion(floor.version)),
                        key=lambda e: ComparableVersion(e.version))  # fmt: skip
        key = f"unit:{unit.coordinate}@{unit.version}"
        steps, moves = [], []
        for rank, (kind, evaluation) in enumerate([(floor.kind, floor_eval), *((f"{unit.kind}_{e.level}", e) for e in others)]):
            steps.append(Step(key, f"{unit.kind} {unit.coordinate} {unit.version} → {evaluation.version} ({kind})", kind, rank))
            moves.append(Move((SetVersion(unit.site, site_text(unit.site), evaluation.version),), evaluation.cleared))
        proposals.append(Proposal(Ladder(key, tuple(steps), len(proposals)), tuple(moves), (str(unit.coordinate),), "unit"))
    return proposals


def enforcer_proposals(violations, analysis: Analysis, root_pom: Path) -> list[Proposal]:
    """One-step ladders that align a dependency-convergence conflict on its highest version (spec 8.4).

    The step edits the declarations that are below that version, or pins the artifact in the root POM when
    nothing declares it. It only counts as passing if the conflict is actually gone.
    """
    proposals = []
    for violation in violations:
        versions = violation.detail.get("versions", []) if violation.rule == "DependencyConvergence" else []
        if len(versions) < 2:
            continue
        coordinate, target = Coordinate.parse(violation.subject), max(versions, key=ComparableVersion)
        below = [d for d in analysis.declarations.for_coordinate(coordinate)
                 if d.site is not None and d.version and ComparableVersion(d.version) < ComparableVersion(target)]  # fmt: skip
        sites = tuple({(d.site.pom, d.site.span): d.site for d in below}.values())
        changes: tuple[Change, ...] = tuple(SetVersion(site, site_text(site), target) for site in sites) \
            or (AddPin(root_pom, coordinate, target, f"aligns {violation.identity}; re-evaluate on a new release"),)  # fmt: skip
        key = f"enf:{violation.subject}"
        label = f"{violation.subject} → {target} (aligns dependency convergence)"
        proposals.append(Proposal(Ladder(key, (Step(key, label, "enforcer_pin", 0),), 0), (Move(changes, ()),),
                                  (violation.subject,), "enforcer", resolves=(violation.identity,)))  # fmt: skip
    return proposals
