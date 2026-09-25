"""Run one verification stage with Maven and classify how it ended (spec sections 4.2 and 9).

This is the local implementation of the ``BuildRunner`` interface. A stage runs in the worktree it is
given, with the environment it was given (the chosen JDK and the run's temp directory, see
``isolation``), writes a numbered log, and comes back as a ``StageOutcome`` that says whether it
passed and, if not, what kind of failure it was.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from giml.gate.modules import module_facts
from giml.gate.reports import read_pit
from giml.maven.enforcer import Violation, parse_enforcer
from giml.maven.failures import INFRASTRUCTURE, Failure, classify
from giml.maven.project import discover_reactor
from giml.maven.runner import Env, MavenRunner

# The enforcer is skipped where it is not the stage under test, so a candidate is not blamed for a
# violation the baseline already had (spec 9.1); the enforcer stage runs it alone.
STAGES: dict[str, tuple[str, ...]] = {
    "compile": ("test-compile", "-Denforcer.skip=true"),
    "unit_test": ("test", "-Denforcer.skip=true"),
    "enforcer": ("validate",),
    # Failsafe-style tests (BDD suites usually run here): no unit tests again, so the stage isolates the integration ones.
    "integration": ("verify", "-Denforcer.skip=true", "-Dtest=NoSuchTest", "-Dsurefire.failIfNoSpecifiedTests=false",
                    "-DfailIfNoTests=false"),
    "pit": ("test-compile", "org.pitest:pitest-maven:mutationCoverage", "-Denforcer.skip=true", "-DtimestampedReports=false"),
}


@dataclass(frozen=True)
class StageOutcome:
    stage: str
    passed: bool
    duration_seconds: float
    log_path: Path
    failure: Failure | None  # None when the stage passed
    cache_hit: bool = False  # answered from the result cache, with the original timing (spec 10)
    details: dict[str, Any] | None = None  # JSON-able extras that must survive the cache: the enforcer's violations
    cache_key: str | None = None  # the content-addressed key the result was looked up by, when it went through the cache

    @property
    def violations(self) -> tuple[Violation, ...]:
        """The enforcer stage's violations (spec 9.1); empty for every other stage."""
        return tuple(Violation.from_dict(v) for v in (self.details or {}).get("violations", []))

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


def pit_mutations(project: Path) -> dict[str, int]:
    """PIT's mutation outcomes (status to count) summed over every module's report of the reactor."""
    total: Counter = Counter()
    for pom in discover_reactor(project):
        report = module_facts(pom).pit_report
        if report.is_file():
            total += read_pit(report).statuses
    return dict(total)


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
            details = {"violations": []} if stage == "enforcer" else {"mutations": pit_mutations(worktree)} if stage == "pit" else None
            return StageOutcome(stage, True, result.duration_seconds, result.log_path, None, details=details)
        text = result.log_path.read_text(encoding="utf-8", errors="replace")
        details = {"violations": [v.to_dict() for v in parse_enforcer(text)]} if stage == "enforcer" else None
        return StageOutcome(stage, False, result.duration_seconds, result.log_path, classify(text, result.timed_out),
                            details=details)  # fmt: skip
