"""Baseline verification (spec section 9.1): run the pipeline on the unmodified base before any candidate.

Which stages can be trusted as an oracle for the candidates, and what the project already gets wrong,
is decided here. If the build or the unit tests fail at the baseline the project is not upgradeable
until fixed and the run stops. Any other stage that fails is marked ``baseline_failed`` and is not
used as an oracle. The enforcer is the exception: its violations become the reference set, so a
candidate fails only on a violation outside it, and removing violations is a goal (spec 8.5).

A baseline that fails for infrastructure reasons (network, disk, memory) is retried, and if it still
fails it is reported as such: that says nothing about the project.

For a rewind run (spec 5.3) the baseline is the rewound state, and a failure has its own stop reason,
``rewind_baseline_failed``: the base code does not work with the old POM.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from giml.core.interfaces import BuildRunner
from giml.maven.build import StageOutcome
from giml.maven.enforcer import Violation

STAGE_ORDER = ("compile", "unit_test", "enforcer")

PASSED, BASELINE_FAILED, NOT_RUN = "passed", "baseline_failed", "not_run"

STOP_BASELINE_FAILED = "baseline_failed"
STOP_REWIND_BASELINE_FAILED = "rewind_baseline_failed"
STOP_BASELINE_INFRASTRUCTURE = "baseline_infrastructure"

# The stages whose failure makes the project not upgradeable, and how the stop is named.
_HARD_STOP = {"compile": "build_failed", "unit_test": "unit_tests_failed"}


@dataclass(frozen=True)
class StageBaseline:
    stage: str
    status: str  # PASSED, BASELINE_FAILED or NOT_RUN (an earlier stage stopped the baseline)
    attempts: int
    outcome: StageOutcome | None


@dataclass(frozen=True)
class Baseline:
    stages: tuple[StageBaseline, ...]  # in pipeline order
    stop: str | None  # build_failed, unit_tests_failed or infrastructure; None when the project can be upgraded
    rewound: bool

    @property
    def upgradeable(self) -> bool:
        return self.stop is None

    @property
    def stop_reason(self) -> str | None:
        """The run's stop reason (spec 11) when the baseline stops it."""
        if self.stop is None:
            return None
        if self.stop == "infrastructure":
            return STOP_BASELINE_INFRASTRUCTURE
        return STOP_REWIND_BASELINE_FAILED if self.rewound else STOP_BASELINE_FAILED

    def _stage(self, name: str) -> StageBaseline:
        return next(s for s in self.stages if s.stage == name)

    @property
    def enforcer_mode(self) -> str:
        """clean: no violations; reference: violations to compare candidates with; unavailable: no usable report."""
        enforcer = self._stage("enforcer")
        if enforcer.status == PASSED:
            return "clean"
        return "reference" if enforcer.outcome is not None and enforcer.outcome.violations else "unavailable"

    @property
    def reference_violations(self) -> tuple[Violation, ...]:
        enforcer = self._stage("enforcer")
        return enforcer.outcome.violations if enforcer.outcome is not None and enforcer.status == BASELINE_FAILED else ()

    @property
    def oracle_stages(self) -> tuple[str, ...]:
        """The stages a candidate's pass or fail can be judged by: those that passed at the baseline."""
        return tuple(s.stage for s in self.stages if s.status == PASSED)

    @property
    def pit_mutations(self) -> dict[str, int] | None:
        """PIT's mutation outcomes at the baseline, when the stage ran and passed."""
        pit = next((s for s in self.stages if s.stage == "pit" and s.status == PASSED and s.outcome is not None), None)
        return (pit.outcome.details or {}).get("mutations") if pit else None

    @property
    def cache_hits(self) -> int:
        return sum(1 for s in self.stages if s.outcome is not None and s.outcome.cache_hit)

    @property
    def seconds(self) -> float:
        return sum(s.outcome.duration_seconds for s in self.stages if s.outcome is not None)

    def as_dict(self) -> dict[str, Any]:
        return {"upgradeable": self.upgradeable, "stop": self.stop, "stop_reason": self.stop_reason, "rewound": self.rewound,
                "stages": [{"stage": s.stage, "status": s.status, "attempts": s.attempts,
                            "seconds": s.outcome.duration_seconds if s.outcome else 0.0,
                            "cache_hit": bool(s.outcome and s.outcome.cache_hit),
                            "failure_class": s.outcome.failure_class if s.outcome else None,
                            "signature": s.outcome.signature if s.outcome else None} for s in self.stages],
                "enforcer": {"mode": self.enforcer_mode, "violations": [v.to_dict() for v in self.reference_violations]},
                "cache_hits": self.cache_hits, "seconds": self.seconds}  # fmt: skip


def _run(runner: BuildRunner, worktree, stage: str, timeout_seconds: int, retries: int) -> tuple[StageOutcome, int]:
    outcome, attempts = runner.run_stage(worktree, stage, timeout_seconds), 1
    while outcome.retryable and attempts <= retries:
        outcome, attempts = runner.run_stage(worktree, stage, timeout_seconds), attempts + 1
    return outcome, attempts


def verify_baseline(runner: BuildRunner, worktree, timeout_seconds: int, rewound: bool = False, retries: int = 1,
                    stages_to_run: Sequence[str] = STAGE_ORDER) -> Baseline:  # fmt: skip
    """Run the stages on the unmodified worktree, in order, stopping where the spec says the project cannot go on."""
    stages: list[StageBaseline] = []
    stop: str | None = None
    for stage in stages_to_run:
        if stop is not None:
            stages.append(StageBaseline(stage, NOT_RUN, 0, None))
            continue
        outcome, attempts = _run(runner, worktree, stage, timeout_seconds, retries)
        if outcome.passed:
            stages.append(StageBaseline(stage, PASSED, attempts, outcome))
            continue
        stages.append(StageBaseline(stage, BASELINE_FAILED, attempts, outcome))
        stop = "infrastructure" if outcome.retryable else _HARD_STOP.get(stage)
    return Baseline(tuple(stages), stop, rewound)
