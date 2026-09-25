"""Verify one candidate change set in a throwaway trial worktree (spec sections 5.2, 9 and 9.1).

A trial starts from the result branch's tip (so accepted steps are in it), applies the candidate's
changes there, runs the verification stages through the (caching) runner and is removed again: a
failed attempt leaves nothing behind, and the developer's checkout and the result worktree are never
touched. Only stages that passed at the baseline are oracles. The enforcer is judged against the
baseline's reference set: a candidate fails on a violation the baseline did not have, tolerates the
ones it had, and the ones it removes are reported as resolved (a goal, spec 8.5).

Stage order is compile, enforcer, unit tests: the enforcer is cheap and deterministic, so a candidate
that adds a violation is stopped before minutes of tests are spent on it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from giml.core.interfaces import BuildRunner
from giml.git.runner import Git
from giml.git.worktrees import WorktreeManager
from giml.maven.build import StageOutcome
from giml.maven.enforcer import Violation, new_violations, resolved_violations
from giml.maven.failures import DUPLICATE_CLASSES, ENFORCER_CONVERGENCE, Failure
from giml.maven.pom_change import Change, apply_changes, rebase
from giml.plan.baseline import Baseline

TRIAL_ORDER = ("compile", "enforcer", "unit_test")
_CONVERGENCE_RULES = frozenset({"DependencyConvergence", "BanDuplicatePomDependencyVersions"})
_MAX_LINES = 8


@dataclass(frozen=True)
class TrialResult:
    passed: bool
    inconclusive: bool  # infrastructure failed even after a retry: says nothing about the candidate
    failed_stage: str | None
    failure: Failure | None
    outcomes: tuple[StageOutcome, ...]  # the stages that ran, in order
    new_violations: tuple[Violation, ...]  # enforcer violations the baseline did not have
    resolved_violations: tuple[Violation, ...]  # baseline violations the candidate no longer has

    @property
    def seconds(self) -> float:
        return sum(o.duration_seconds for o in self.outcomes)

    @property
    def cache_hits(self) -> int:
        return sum(1 for o in self.outcomes if o.cache_hit)


def _enforcer_failure(outcome: StageOutcome, new: tuple[Violation, ...]) -> Failure:
    """The failure of a candidate that adds violations: its class and signature come from the new ones only."""
    rules = {v.rule for v in new}
    if "BanDuplicateClasses" in rules:
        failure_class = DUPLICATE_CLASSES
    elif rules & _CONVERGENCE_RULES:
        failure_class = ENFORCER_CONVERGENCE
    else:
        failure_class = outcome.failure_class or ENFORCER_CONVERGENCE
    lines = tuple(v.identity for v in new)[:_MAX_LINES]
    digest = hashlib.sha256("\n".join((failure_class, *lines)).encode("utf-8")).hexdigest()
    return Failure(failure_class, digest[:16], lines)


class TrialRunner:
    def __init__(self, manager: WorktreeManager, result_worktree: Path, project_subdir: str, runner: BuildRunner,
                 baseline: Baseline, timeout_seconds: int, retries: int = 1) -> None:  # fmt: skip
        self.manager, self.result_worktree, self.subdir = manager, result_worktree, project_subdir
        self.runner, self.baseline, self.timeout, self.retries = runner, baseline, timeout_seconds, retries
        self._trials = 0
        self.reference = baseline.reference_violations  # what a candidate may keep; shrinks as commits resolve violations

    def _stages(self) -> list[str]:
        oracle = self.baseline.oracle_stages
        return [s for s in TRIAL_ORDER if (s in oracle if s != "enforcer" else self.baseline.enforcer_mode in ("clean", "reference"))]

    def _run(self, project: Path, stage: str) -> StageOutcome:
        outcome, attempts = self.runner.run_stage(project, stage, self.timeout), 1
        while outcome.retryable and attempts <= self.retries:
            outcome, attempts = self.runner.run_stage(project, stage, self.timeout), attempts + 1
        return outcome

    def refresh_reference(self) -> tuple[Violation, ...]:
        """After commits: the result worktree's own violations become the reference (a resolved one may not come back)."""
        if self.baseline.enforcer_mode not in ("clean", "reference"):
            return self.reference
        project = self.result_worktree / self.subdir if self.subdir else self.result_worktree
        outcome = self._run(project, "enforcer")
        if not outcome.retryable and (outcome.passed or outcome.violations):
            self.reference = outcome.violations
        return self.reference

    def verify(self, changes: Sequence[Change]) -> TrialResult:
        """Apply ``changes`` to a trial worktree at the result branch's tip and run the stages there."""
        self._trials += 1
        tip = Git(self.result_worktree).out("rev-parse", "HEAD")
        trial = self.manager.create_trial(tip, self._trials)
        try:
            apply_changes([rebase(change, self.result_worktree, trial) for change in changes])
            return self._stages_in(trial / self.subdir if self.subdir else trial)
        finally:
            self.manager.remove(trial)

    def _stages_in(self, project: Path) -> TrialResult:
        outcomes: list[StageOutcome] = []
        resolved: tuple[Violation, ...] = ()
        for stage in self._stages():
            outcome = self._run(project, stage)
            outcomes.append(outcome)
            if outcome.retryable:
                return TrialResult(False, True, stage, outcome.failure, tuple(outcomes), (), ())
            if stage == "enforcer":
                reference = self.reference
                new = new_violations(reference, outcome.violations)
                resolved = resolved_violations(reference, outcome.violations)
                if new:
                    return TrialResult(False, False, stage, _enforcer_failure(outcome, new), tuple(outcomes), new, resolved)
                if not outcome.passed and not outcome.violations:  # failed for a reason that is not a violation we can compare
                    return TrialResult(False, False, stage, outcome.failure, tuple(outcomes), (), ())
            elif not outcome.passed:
                return TrialResult(False, False, stage, outcome.failure, tuple(outcomes), (), resolved)
        return TrialResult(True, False, None, None, tuple(outcomes), (), resolved)
