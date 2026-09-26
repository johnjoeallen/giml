from pathlib import Path

import pytest

from giml.maven.build import StageOutcome
from giml.maven.failures import COMPILE, UNAVAILABLE, Failure, failure_from_lines
from giml.plan.baseline import STAGE_ORDER, UNAVAILABLE as BASELINE_UNAVAILABLE, verify_baseline
from giml.smoke.stage import StartupAwareRunner
from tests.plan.test_baseline import Scripted, bad, ok

LOG = Path("/logs/x.log")


class Inner:
    def __init__(self, package):
        self.package, self.calls = package, []

    def run_stage(self, worktree, stage, timeout):
        self.calls.append(stage)
        return self.package if stage == "package" else ok(stage)


class FakeSmoke:
    def __init__(self, outcome):
        self.outcome, self.trials = outcome, []

    def run(self, worktree, trial):
        self.trials.append(trial)
        return self.outcome


def test_other_stages_go_straight_to_the_inner_runner():
    inner = Inner(ok("package"))
    assert StartupAwareRunner(inner, None).run_stage(Path("/w"), "compile", 60).stage == "compile" and inner.calls == ["compile"]


def test_startup_packages_first_and_then_boots_numbering_the_trials():
    inner, smoke = Inner(ok("package")), FakeSmoke(StageOutcome("startup", True, 3.0, LOG, None))
    runner = StartupAwareRunner(inner, smoke)
    assert runner.run_stage(Path("/w"), "startup", 60).passed and runner.run_stage(Path("/w"), "startup", 60).passed
    assert inner.calls == ["package", "package"] and smoke.trials == [1, 2]


def test_a_packaging_failure_is_the_startup_stages_failure_with_its_own_class():
    failed = StageOutcome("package", False, 1.0, LOG, Failure(COMPILE, "abc", ("x",)))
    smoke = FakeSmoke(None)
    outcome = StartupAwareRunner(Inner(failed), smoke).run_stage(Path("/w"), "startup", 60)
    assert (outcome.stage, outcome.passed, outcome.failure_class) == ("startup", False, COMPILE) and smoke.trials == []


def test_a_startup_stage_without_settings_is_a_programming_error():
    with pytest.raises(ValueError, match="needs smoke settings"):
        StartupAwareRunner(Inner(ok("package")), None).run_stage(Path("/w"), "startup", 60)


def unavailable():
    return StageOutcome("startup", False, 1.0, LOG, failure_from_lines(UNAVAILABLE, ["the database is unavailable: PG_HOST not set"]))


def test_a_startup_check_that_cannot_run_at_the_baseline_is_unavailable_not_failed_and_not_an_oracle():
    runner = Scripted(compile=ok("compile"), unit_test=ok("unit_test"), enforcer=ok("enforcer", details={"violations": []}),
                      startup=unavailable(), pit=ok("pit"))  # fmt: skip
    baseline = verify_baseline(runner, Path("/w"), 60, stages_to_run=(*STAGE_ORDER, "startup", "pit"))
    assert baseline.upgradeable and baseline.stop is None
    assert [s.status for s in baseline.stages] == ["passed", "passed", "passed", BASELINE_UNAVAILABLE, "passed"]
    assert "startup" not in baseline.oracle_stages and "pit" in baseline.oracle_stages  # the run goes on without it


def test_a_failing_startup_at_the_baseline_is_a_baseline_failure_that_is_not_an_oracle():
    runner = Scripted(compile=ok("compile"), unit_test=ok("unit_test"), enforcer=ok("enforcer", details={"violations": []}),
                      startup=bad("startup", "startup"))  # fmt: skip
    baseline = verify_baseline(runner, Path("/w"), 60, stages_to_run=(*STAGE_ORDER, "startup"))
    assert baseline.upgradeable and baseline.stages[-1].status == "baseline_failed" and "startup" not in baseline.oracle_stages
