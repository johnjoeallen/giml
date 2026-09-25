"""Run one verification stage with Maven and classify how it ended (spec sections 4.2 and 9).

This is the local implementation of the ``BuildRunner`` interface. A stage runs in the worktree it is
given, with the environment it was given (the chosen JDK and the run's temp directory, see
``isolation``), writes a numbered log, and comes back as a ``StageOutcome`` that says whether it
passed and, if not, what kind of failure it was.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from giml.maven.failures import INFRASTRUCTURE, Failure, classify
from giml.maven.runner import Env, MavenRunner

# The enforcer is skipped where it is not the stage under test, so a candidate is not blamed for a
# violation the baseline already had (spec 9.1); the enforcer stage runs it alone.
STAGES: dict[str, tuple[str, ...]] = {
    "compile": ("test-compile", "-Denforcer.skip=true"),
    "unit_test": ("test", "-Denforcer.skip=true"),
    "enforcer": ("validate",),
}


@dataclass(frozen=True)
class StageOutcome:
    stage: str
    passed: bool
    duration_seconds: float
    log_path: Path
    failure: Failure | None  # None when the stage passed
    cache_hit: bool = False  # answered from the result cache, with the original timing (spec 10)

    @property
    def failure_class(self) -> str | None:
        return self.failure.failure_class if self.failure else None

    @property
    def signature(self) -> str | None:
        return self.failure.signature if self.failure else None

    @property
    def retryable(self) -> bool:
        """An infrastructure failure is not the candidate's fault: try again or ignore it (spec 9)."""
        return self.failure_class == INFRASTRUCTURE


class MavenBuildRunner:
    def __init__(self, maven: MavenRunner, env: Env, logs_dir: Path) -> None:
        self.maven, self.env, self.logs_dir = maven, env, logs_dir
        self._runs = 0

    def run_stage(self, worktree: Path, stage: str, timeout_seconds: float) -> StageOutcome:
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}; known: {', '.join(sorted(STAGES))}")
        self._runs += 1
        log = self.logs_dir / f"{self._runs:02d}-{stage}.log"
        result = self.maven(worktree, list(STAGES[stage]), log, timeout_seconds, self.env)
        if result.succeeded:
            return StageOutcome(stage, True, result.duration_seconds, result.log_path, None)
        failure = classify(result.log_path.read_text(encoding="utf-8", errors="replace"), result.timed_out)
        return StageOutcome(stage, False, result.duration_seconds, result.log_path, failure)
