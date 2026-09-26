"""Verify one candidate change set in a throwaway trial worktree (spec sections 5.2, 9 and 9.1).

A trial starts from the result branch's tip (so accepted steps are in it), applies the candidate's
changes there, runs the verification stages through the (caching) runner and is removed again: a
failed attempt leaves nothing behind, and the developer's checkout and the result worktree are never
touched. Only stages that passed at the baseline are oracles. The enforcer is judged against the
baseline's reference set: a candidate fails on a violation the baseline did not have, tolerates the
ones it had, and the ones it removes are reported as resolved (a goal, spec 8.5).

Stage order is a vulnerability check on the resolved tree (no build), compile, enforcer, unit tests, integration tests
(where BDD suites usually run) and PIT: a stage is only run for a candidate that passed the cheaper ones before it, and
PIT must keep the mutation scores the project earned (or the baseline's, when lower).
"""

from __future__ import annotations

import hashlib
import os
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable, Sequence

from giml.core.interfaces import BuildRunner
from giml.git.runner import Git
from giml.git.worktrees import WorktreeManager
from giml.maven.build import StageOutcome
from giml.maven.enforcer import Violation, new_violations, resolved_violations
from giml.gate.reports import Mutations
from giml.maven.failures import (DUPLICATE_CLASSES, ENFORCER_CONVERGENCE, MUTATION_DROPPED, RESOLUTION, VULNERABILITY_WORSE, Failure)
from giml.maven.pom_change import Change, apply_changes, rebase
from giml.maven.tree import ResolutionError
from giml.plan.baseline import Baseline
from giml.plan.exposure import TreeExposure, worse_exposure

TRIAL_ORDER = ("compile", "enforcer", "unit_test", "integration", "pit")  # cheap and decisive first; PIT is the slowest
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


ResolveExposure = Callable[[Path], TreeExposure]  # a project directory to the CVE exposure of its resolved trees


def _digest(failure_class: str, lines: Sequence[str]) -> Failure:
    return Failure(failure_class, hashlib.sha256("\n".join((failure_class, *lines)).encode("utf-8")).hexdigest()[:16], tuple(lines))


class TrialRunner:
    def __init__(self, manager: WorktreeManager, result_worktree: Path, project_subdir: str, runner: BuildRunner,
                 baseline: Baseline, timeout_seconds: int, retries: int = 1,
                 resolve_exposure: ResolveExposure | None = None) -> None:  # fmt: skip
        self.manager, self.result_worktree, self.subdir = manager, result_worktree, project_subdir
        self.runner, self.baseline, self.timeout, self.retries = runner, baseline, timeout_seconds, retries
        self._trials = 0
        self.resolve_exposure = resolve_exposure
        self.pit_floors: tuple[float, float] | None = None  # (mutation coverage, test strength) a PIT run must reach
        self.exposure_reference: TreeExposure | None = None  # the tip's exposure: a candidate may not be worse than this
        self.reference = baseline.reference_violations  # what a candidate may keep; shrinks as commits resolve violations

    def _stages(self, pit: bool) -> list[str]:
        oracle = self.baseline.oracle_stages
        return [s for s in TRIAL_ORDER if (pit or s != "pit")
                and (s in oracle if s != "enforcer" else self.baseline.enforcer_mode in ("clean", "reference"))]  # fmt: skip

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

    def verify(self, changes: Sequence[Change], pit: bool = True) -> TrialResult:
        """Apply ``changes`` to a trial worktree at the result branch's tip and run the stages there.

        ``pit=False`` leaves out PIT, the slowest stage: the search verifies its candidates without it and runs it
        only on the state it means to commit (a stage whose result is already cached costs nothing to repeat).
        """
        self._trials += 1
        tip = Git(self.result_worktree).out("rev-parse", "HEAD")
        trial = self.manager.create_trial(tip, self._trials)
        try:
            apply_changes([rebase(change, self.result_worktree, trial) for change in changes])
            return self._stages_in(trial / self.subdir if self.subdir else trial, pit)
        finally:
            self.manager.remove(trial)

    def _pit_shortfall(self, outcome: StageOutcome) -> tuple[str, ...]:
        """Why a PIT run that finished is still a failure: too few mutants, or a score below its floor."""
        if self.pit_floors is None:
            return ()
        mutations = Mutations(Counter((outcome.details or {}).get("mutations", {})))
        scores = (("mutation coverage", mutations.mutation_coverage, self.pit_floors[0]),
                  ("test strength", mutations.test_strength, self.pit_floors[1]))  # fmt: skip
        if mutations.total == 0:
            return ("PIT produced no mutations",)
        return tuple(f"{name} {score:.1f} is below {floor:.1f}" for name, score, floor in scores if score is not None and score < floor)

    def _exposure_stage(self, project: Path) -> StageOutcome | None:
        """A candidate that makes the vulnerabilities worse fails like a broken build, without one being run."""
        if self.resolve_exposure is None or self.exposure_reference is None:
            return None
        started = time.monotonic()
        try:
            lines, failure_class = worse_exposure(self.exposure_reference, self.resolve_exposure(project)), VULNERABILITY_WORSE
        except ResolutionError as exc:
            lines, failure_class = (str(exc).replace(str(project), "<project>"),), RESOLUTION
        failure = _digest(failure_class, lines) if lines else None
        return StageOutcome("exposure", failure is None, time.monotonic() - started, Path(os.devnull), failure)

    def _stages_in(self, project: Path, pit: bool) -> TrialResult:
        outcomes: list[StageOutcome] = []
        resolved: tuple[Violation, ...] = ()
        early = self._exposure_stage(project)
        if early is not None:
            outcomes.append(early)
            if not early.passed:
                return TrialResult(False, False, "exposure", early.failure, tuple(outcomes), (), ())
        for stage in self._stages(pit):
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
            elif stage == "pit" and (lines := self._pit_shortfall(outcome)):
                return TrialResult(False, False, stage, _digest(MUTATION_DROPPED, lines), tuple(outcomes), (), resolved)
        return TrialResult(True, False, None, None, tuple(outcomes), (), resolved)
