import json
from pathlib import Path

import pytest

from giml.maven.build import StageOutcome
from giml.maven.enforcer import Violation
from giml.maven.failures import COMPILE, ENFORCER_CONVERGENCE, INFRASTRUCTURE, RESOLUTION, UNIT_TEST, Failure
from giml.plan.baseline import STAGE_ORDER, verify_baseline

WORKTREE = Path("/wt")
LOG = Path("/logs/x.log")


def ok(stage, seconds=1.0, hit=False, details=None):
    return StageOutcome(stage, True, seconds, LOG, None, cache_hit=hit, details=details)


def bad(stage, failure_class, seconds=2.0, details=None):
    return StageOutcome(stage, False, seconds, LOG, Failure(failure_class, "0123456789abcdef", ("line",)), details=details)


VIOLATION = Violation("DependencyConvergence", "org.x:lib", {"versions": ["1", "2"]})


class Scripted:
    """A BuildRunner that answers each stage from a queue (a stage may be answered several times)."""

    def __init__(self, **answers):
        self.answers = {stage: list(queue if isinstance(queue, list) else [queue]) for stage, queue in answers.items()}
        self.calls = []

    def run_stage(self, worktree, stage, timeout_seconds):
        self.calls.append((worktree, stage, timeout_seconds))
        queue = self.answers[stage]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def everything_passes():
    return Scripted(compile=ok("compile"), unit_test=ok("unit_test"), enforcer=ok("enforcer", details={"violations": []}))


def test_the_stages_run_in_pipeline_order_on_the_unmodified_worktree():
    assert STAGE_ORDER == ("compile", "unit_test", "enforcer")
    runner = everything_passes()
    baseline = verify_baseline(runner, WORKTREE, timeout_seconds=90)
    assert [(w, s, t) for w, s, t in runner.calls] == [(WORKTREE, "compile", 90), (WORKTREE, "unit_test", 90), (WORKTREE, "enforcer", 90)]
    assert baseline.upgradeable and baseline.stop is None and baseline.stop_reason is None
    assert [(s.stage, s.status) for s in baseline.stages] == [("compile", "passed"), ("unit_test", "passed"), ("enforcer", "passed")]
    assert baseline.reference_violations == () and baseline.enforcer_mode == "clean"
    assert baseline.oracle_stages == ("compile", "unit_test", "enforcer")


def test_a_failing_build_stops_the_baseline_and_nothing_after_it_runs():
    runner = Scripted(compile=bad("compile", COMPILE), unit_test=ok("unit_test"), enforcer=ok("enforcer"))
    baseline = verify_baseline(runner, WORKTREE, 60)
    assert (baseline.upgradeable, baseline.stop, baseline.stop_reason) == (False, "build_failed", "baseline_failed")
    assert [(s.stage, s.status) for s in baseline.stages] == [("compile", "baseline_failed"), ("unit_test", "not_run"), ("enforcer", "not_run")]
    assert [call[1] for call in runner.calls] == ["compile"] and baseline.oracle_stages == ()


def test_failing_unit_tests_stop_the_baseline_before_the_enforcer():
    runner = Scripted(compile=ok("compile"), unit_test=bad("unit_test", UNIT_TEST), enforcer=ok("enforcer"))
    baseline = verify_baseline(runner, WORKTREE, 60)
    assert (baseline.stop, baseline.upgradeable) == ("unit_tests_failed", False)
    assert [s.status for s in baseline.stages] == ["passed", "baseline_failed", "not_run"]
    assert [call[1] for call in runner.calls] == ["compile", "unit_test"] and baseline.oracle_stages == ("compile",)


def test_a_failing_baseline_of_a_rewound_run_has_its_own_stop_reason():
    runner = Scripted(compile=bad("compile", COMPILE))
    baseline = verify_baseline(runner, WORKTREE, 60, rewound=True)
    assert (baseline.stop, baseline.stop_reason, baseline.rewound) == ("build_failed", "rewind_baseline_failed", True)
    assert verify_baseline(everything_passes(), WORKTREE, 60, rewound=True).stop_reason is None


def test_enforcer_violations_at_the_baseline_are_not_a_reason_to_stop_they_become_the_reference():
    outcome = bad("enforcer", ENFORCER_CONVERGENCE, details={"violations": [VIOLATION.to_dict()]})
    baseline = verify_baseline(Scripted(compile=ok("compile"), unit_test=ok("unit_test"), enforcer=outcome), WORKTREE, 60)
    assert baseline.upgradeable and baseline.stop is None
    assert baseline.stages[2].status == "baseline_failed"
    assert baseline.reference_violations == (VIOLATION,) and baseline.enforcer_mode == "reference"
    assert baseline.oracle_stages == ("compile", "unit_test")  # the enforcer is judged against the reference, not as pass/fail


def test_an_enforcer_failure_that_is_not_a_violation_is_still_a_baseline_failure_without_a_reference():
    outcome = bad("enforcer", RESOLUTION, details={"violations": []})
    baseline = verify_baseline(Scripted(compile=ok("compile"), unit_test=ok("unit_test"), enforcer=outcome), WORKTREE, 60)
    assert (baseline.upgradeable, baseline.stages[2].status, baseline.reference_violations) == (True, "baseline_failed", ())
    assert baseline.enforcer_mode == "unavailable" and baseline.stages[2].outcome.failure_class == RESOLUTION


def test_an_infrastructure_failure_is_retried_once():
    runner = Scripted(compile=[bad("compile", INFRASTRUCTURE), ok("compile")], unit_test=ok("unit_test"), enforcer=ok("enforcer"))
    baseline = verify_baseline(runner, WORKTREE, 60)
    assert baseline.upgradeable and [c[1] for c in runner.calls] == ["compile", "compile", "unit_test", "enforcer"]
    assert baseline.stages[0].attempts == 2 and baseline.stages[1].attempts == 1


def test_a_persistent_infrastructure_failure_is_not_blamed_on_the_project():
    runner = Scripted(compile=[bad("compile", INFRASTRUCTURE), bad("compile", INFRASTRUCTURE)])
    baseline = verify_baseline(runner, WORKTREE, 60)
    assert (baseline.upgradeable, baseline.stop, baseline.stop_reason) == (False, "infrastructure", "baseline_infrastructure")
    assert baseline.stages[0].attempts == 2 and len(runner.calls) == 2


def test_retries_can_be_switched_off():
    runner = Scripted(compile=[bad("compile", INFRASTRUCTURE), ok("compile")])
    assert verify_baseline(runner, WORKTREE, 60, retries=0).stop == "infrastructure" and len(runner.calls) == 1


def test_cache_hits_and_timings_are_kept():
    runner = Scripted(compile=ok("compile", 5.0, hit=True), unit_test=ok("unit_test", 12.0), enforcer=ok("enforcer", 3.0, hit=True))
    baseline = verify_baseline(runner, WORKTREE, 60)
    assert baseline.cache_hits == 2 and baseline.seconds == 20.0


def test_as_dict_is_plain_json_and_complete():
    outcome = bad("enforcer", ENFORCER_CONVERGENCE, details={"violations": [VIOLATION.to_dict()]})
    data = verify_baseline(Scripted(compile=ok("compile"), unit_test=ok("unit_test", hit=True), enforcer=outcome), WORKTREE, 60).as_dict()
    json.dumps(data)
    assert (data["upgradeable"], data["stop"], data["stop_reason"], data["rewound"]) == (True, None, None, False)
    assert data["enforcer"] == {"mode": "reference", "violations": [VIOLATION.to_dict()]}
    assert data["stages"][1] == {"stage": "unit_test", "status": "passed", "attempts": 1, "seconds": 1.0, "cache_hit": True,
                                 "failure_class": None, "signature": None}  # fmt: skip
    assert data["stages"][2]["failure_class"] == ENFORCER_CONVERGENCE and data["stages"][2]["signature"] == "0123456789abcdef"
    assert data["cache_hits"] == 1 and data["seconds"] == 4.0


def test_a_runner_error_is_not_swallowed():
    class Broken:
        def run_stage(self, worktree, stage, timeout_seconds):
            raise RuntimeError("mvn vanished")

    with pytest.raises(RuntimeError, match="mvn vanished"):
        verify_baseline(Broken(), WORKTREE, 60)


