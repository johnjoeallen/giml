"""Run the conservative plan on a result worktree (spec sections 8.3, 8.4 and 9).

Parent and BOM units go first: they change what every dependency resolves to, so once one is
committed the tree is analysed again and the dependency ladders are built from the new state. Each
phase is one search (``plan.search``) whose verification is a trial worktree (``plan.trial``). Only
what the search accepted is written to the result worktree, and one commit per accepted step, each
holding exactly the state the search verified (the steps up to and including it). Everything else is
a deferral with a reason and the triggers that would justify trying again.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from giml.core.model import DeferralRecord
from giml.git.worktrees import commit_all
from giml.maven.pom_change import Change, apply_changes, describe
from giml.plan.analysis import Analysis
from giml.plan.exposure import Exposure
from giml.plan.search import Budget, Outcome, Step, Verdict, search
from giml.plan.steps import Proposal, dependency_proposals, enforcer_proposals, unit_proposals
from giml.plan.trial import TrialResult, TrialRunner

Reanalyse = Callable[[str, bool], Analysis]  # (log name, evaluate parent/BOM units) -> the worktree's current analysis
_TRIGGERS = ("new_release", "new_advisory", "pom_change")


@dataclass(frozen=True)
class Committed:
    key: str
    label: str
    kind: str
    sha: str
    clears: tuple[str, ...]
    edits: tuple[str, ...]  # what each change did, for the report


@dataclass(frozen=True)
class Left:
    """A dependency (or unit) that was not moved, and why."""

    key: str
    members: tuple[str, ...]
    reason: str
    tried: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class Naive:
    """What bumping everything at once would have done: every ladder's first step, in one build (spec 14)."""

    steps: tuple[str, ...]
    passed: bool
    reason: str


@dataclass(frozen=True)
class PlanOutcome:
    committed: tuple[Committed, ...]
    left: tuple[Left, ...]
    builds: int
    stop_reason: str  # complete, budget_builds, budget_time or inconclusive
    stop_detail: str
    exposure_before: Exposure
    exposure_after: Exposure
    held: dict[str, str]  # the version each coordinate was at when it was planned
    naive: Naive | None = None


def verdict_of(result: TrialResult, required: frozenset[str] = frozenset()) -> Verdict:
    """The search's view of a trial: a failure's reason names its stage and class, a build counts unless every stage was cached.

    ``required`` are enforcer violations the candidate must have removed: passing while they remain is a failure.
    """
    missing = sorted(required - {v.identity for v in result.resolved_violations}) if result.passed else []
    if missing:
        return Verdict(False, False, f"did not resolve {', '.join(missing)}", 0 if result.cache_hits == len(result.outcomes) else 1)
    if result.passed or result.failure is None:
        reason = "passed" if result.passed else "did not pass"
    else:
        detail = f": {result.failure.key_lines[0]}" if result.failure.key_lines else ""
        reason = f"{result.failed_stage} {result.failure.failure_class}{detail}"
    cost = 0 if result.outcomes and result.cache_hits == len(result.outcomes) else 1
    return Verdict(result.passed, result.inconclusive, reason, cost)


def _changes_of(proposals: Mapping[str, Proposal], chosen: Mapping[str, Step]) -> list[Change]:
    return [change for key, step in chosen.items() for change in proposals[key].moves[step.rank].changes]


def _commit_message(proposal: Proposal, step: Step, clears: tuple[str, ...], tier: str, run_id: str) -> str:
    lines = [step.label, ""]
    if clears:
        lines.append(f"Clears: {', '.join(clears)}")
    lines.append(f"Verified: compile, enforcer, unit tests. Tier {tier}. Run {run_id}.")
    return "\n".join(lines)


def _commit_steps(accepted: Mapping[str, Step], proposals: Mapping[str, Proposal], root: Path, tier: str, run_id: str) -> list[Committed]:
    """One commit per accepted step. Every commit is rebuilt from the original files with the steps so far applied
    together, because that is exactly what the search verified and it keeps the recorded text spans valid."""
    touched = sorted({_path(change) for key, step in accepted.items() for change in proposals[key].moves[step.rank].changes})
    originals = {path: path.read_bytes() for path in touched}
    done: dict[str, Step] = {}
    committed = []
    for key, step in accepted.items():
        done[key] = step
        for path, data in originals.items():
            path.write_bytes(data)
        apply_changes(_changes_of(proposals, done))
        move = proposals[key].moves[step.rank]
        sha = commit_all(root, _commit_message(proposals[key], step, move.clears, tier, run_id), touched)
        committed.append(Committed(key, step.label, step.kind, sha, move.clears, tuple(describe(c, root) for c in move.changes)))
    return committed


def _path(change: Change) -> Path:
    return change.site.pom if hasattr(change, "site") else change.pom


def _lefts(outcome: Outcome, proposals: Mapping[str, Proposal]) -> list[Left]:
    return [Left(key, proposals[key].members, deferral.reason, deferral.tried) for key, deferral in outcome.deferred.items()]


def execute(root: Path, run_id: str, tier: str, analysis: Analysis, reanalyse: Reanalyse, trial: TrialRunner,
            options, clock: Callable[[], datetime.datetime], root_pom: Path) -> PlanOutcome:  # fmt: skip
    """Search and commit the parent/BOM phase, then the dependency phase; ``root`` is the result worktree."""
    started = clock()
    committed: list[Committed] = []
    left: list[Left] = []
    builds, stop, detail = 0, "complete", ""
    before = analysis.exposure.exposure
    held: dict[str, str] = {}

    naive: list[Naive | None] = []

    def phase(proposals: list[Proposal], kind: str = "unit") -> bool:
        nonlocal builds, stop, detail
        held.update(held_versions(analysis))
        by_key = {p.ladder.key: p for p in proposals}
        budget = Budget(max(options.max_builds - builds, 0), options.max_wall_minutes, clock, started)

        def verify(chosen: Mapping[str, Step]) -> Verdict:
            required = frozenset(i for key in chosen for i in by_key[key].resolves)
            return verdict_of(trial.verify(_changes_of(by_key, chosen)), required)

        outcome = search([p.ladder for p in proposals], verify, budget)
        builds += outcome.builds
        if proposals and kind == "dependency":
            naive.append(_naive(proposals, trial))
        committed.extend(_commit_steps(outcome.accepted, by_key, root, tier, run_id))
        if outcome.accepted:
            trial.refresh_reference()
        left.extend(_lefts(outcome, by_key))
        if outcome.stop_reason != "complete":
            stop, detail = outcome.stop_reason, outcome.stop_detail
        return bool(outcome.accepted)

    if phase(unit_proposals(analysis)):
        analysis = reanalyse("02-tree.log", True)
    if stop != "inconclusive":
        if phase(_dependency_phase(analysis, options, root_pom, trial.reference, clock()), "dependency"):
            analysis = reanalyse("03-tree.log", False)
    return PlanOutcome(tuple(committed), tuple(left), builds, stop, detail, before, analysis.exposure.exposure, held, next(iter(naive), None))


def _dependency_phase(analysis: Analysis, options, root_pom: Path, violations, now: datetime.datetime) -> list[Proposal]:
    """CVE ladders first, then the enforcer alignments, then general updates; ordered as one list for the search."""
    proposals = dependency_proposals(analysis, options, root_pom, now)
    cve = [p for p in proposals if p.ladder.key.startswith("dep:")]
    rest = [p for p in proposals if not p.ladder.key.startswith("dep:")]
    ordered = [*cve, *enforcer_proposals(violations, analysis, root_pom), *rest]
    return [dataclasses.replace(p, ladder=dataclasses.replace(p.ladder, order=index)) for index, p in enumerate(ordered)]


def _naive(proposals: list[Proposal], trial: TrialRunner) -> Naive | None:
    """Verify every ladder's first step together, before anything is committed; None when no ladder has a step."""
    firsts = {p.ladder.key: p for p in proposals if p.ladder.steps}
    if not firsts:
        return None
    changes = [change for p in firsts.values() for change in p.moves[0].changes]
    verdict = verdict_of(trial.verify(changes))
    return Naive(tuple(p.ladder.steps[0].label for p in firsts.values()), verdict.passed, verdict.reason)


def deferral_records(outcome: PlanOutcome, project_id: str, run_id: str, now: datetime.datetime) -> list[DeferralRecord]:  # fmt: skip
    """One record per coordinate that was left where it was (a ladder over several coordinates records each)."""
    records = []
    for entry in outcome.left:
        trigger = json.dumps({"on": list(_TRIGGERS), "coordinates": list(entry.members)}, sort_keys=True)
        for coordinate in entry.members:
            records.append(DeferralRecord(f"{run_id}:{coordinate}", project_id, coordinate, outcome.held.get(coordinate, ""),
                                          entry.reason, trigger, now))  # fmt: skip
    return records


def held_versions(analysis: Analysis) -> dict[str, str]:
    held = {str(d.coordinate): d.version for d in analysis.exposure.dependencies}
    held.update({str(u.unit.coordinate): u.unit.version for u in analysis.unit_plans})
    return held
