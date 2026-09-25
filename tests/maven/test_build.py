from pathlib import Path

import pytest

from giml.maven.build import STAGES, MavenBuildRunner, StageOutcome
from giml.maven.failures import COMPILE, DUPLICATE_CLASSES, ENFORCER_CONVERGENCE, INFRASTRUCTURE, RESOLUTION, TIMEOUT, UNIT_TEST
from giml.maven.runner import MavenResult

LOGS = Path(__file__).resolve().parents[1] / "fixtures" / "logs"


class ReplayMaven:
    """Answers each run from a real captured log (or as a success)."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, project, args, log, timeout, env):
        self.calls.append((project, args, log, timeout, env))
        reply = self.replies.pop(0) if self.replies else ("ok", None)
        kind, text = reply
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(text if text is not None else "[INFO] BUILD SUCCESS\n")
        if kind == "timeout":
            return MavenResult(tuple(args), None, 3.0, log, True)
        return MavenResult(tuple(args), 0 if kind == "ok" else 1, 1.5, log, False)


def captured(name: str) -> tuple[str, str]:
    return "fail", (LOGS / f"{name}-a.log").read_text(encoding="utf-8")


def runner(tmp_path, *replies, env=None):
    maven = ReplayMaven(*replies)
    return MavenBuildRunner(maven, env, tmp_path / "logs"), maven


def test_the_stages_map_to_maven_goals(tmp_path):
    assert set(STAGES) == {"compile", "unit_test", "enforcer"}
    run, maven = runner(tmp_path)
    for stage in STAGES:
        assert run.run_stage(tmp_path / "wt", stage, 60).passed
    assert [args for _, args, *_ in maven.calls] == [
        ["test-compile", "-Denforcer.skip=true"], ["test", "-Denforcer.skip=true"], ["validate"]]  # fmt: skip


def test_a_passing_stage(tmp_path):
    run, maven = runner(tmp_path, env={"TMPDIR": "/run/tmp"})
    outcome = run.run_stage(tmp_path / "wt", "unit_test", 90)
    assert outcome == StageOutcome("unit_test", True, 1.5, tmp_path / "logs" / "01-unit_test.log", None)
    project, _, _, timeout, env = maven.calls[0]
    assert (project, timeout, env) == (tmp_path / "wt", 90, {"TMPDIR": "/run/tmp"})
    assert outcome.failure_class is None and outcome.signature is None and not outcome.retryable


@pytest.mark.parametrize(("stage", "log", "failure_class"), [
    ("compile", "compile-error", COMPILE),
    ("unit_test", "test-failure", UNIT_TEST),
    ("compile", "unresolvable", RESOLUTION),
    ("enforcer", "convergence", ENFORCER_CONVERGENCE),
    ("enforcer", "duplicate-classes", DUPLICATE_CLASSES),
])  # fmt: skip
def test_failures_are_classified_from_the_log(tmp_path, stage, log, failure_class):
    run, _ = runner(tmp_path, captured(log))
    outcome = run.run_stage(tmp_path / "wt", stage, 60)
    assert (outcome.passed, outcome.failure_class, outcome.stage) == (False, failure_class, stage)
    assert len(outcome.signature) == 16 and outcome.failure.key_lines and not outcome.retryable


def test_a_timeout_is_a_timeout(tmp_path):
    run, _ = runner(tmp_path, ("timeout", "partial log\n"))
    outcome = run.run_stage(tmp_path / "wt", "unit_test", 60)
    assert (outcome.passed, outcome.failure_class, outcome.duration_seconds) == (False, TIMEOUT, 3.0)


def test_an_infrastructure_failure_is_retryable(tmp_path):
    run, _ = runner(tmp_path, ("fail", "[ERROR] Could not transfer artifact org.x:y:pom:1: Connect timed out\n"))
    outcome = run.run_stage(tmp_path / "wt", "compile", 60)
    assert (outcome.failure_class, outcome.retryable) == (INFRASTRUCTURE, True)


def test_every_stage_run_has_its_own_numbered_log(tmp_path):
    run, maven = runner(tmp_path)
    run.run_stage(tmp_path / "wt", "compile", 60)
    run.run_stage(tmp_path / "wt", "compile", 60)
    run.run_stage(tmp_path / "wt", "unit_test", 60)
    assert [log.name for _, _, log, *_ in maven.calls] == ["01-compile.log", "02-compile.log", "03-unit_test.log"]


def test_an_unknown_stage_is_refused(tmp_path):
    run, maven = runner(tmp_path)
    with pytest.raises(ValueError, match="unknown stage 'startup'; known: compile, enforcer, unit_test"):
        run.run_stage(tmp_path / "wt", "startup", 60)
    assert maven.calls == []
