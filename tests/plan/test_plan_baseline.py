import datetime
import json
from pathlib import Path

import pytest

from giml.cli import Environment, ExitCode, main
from giml.maven.isolation import InsufficientSpace
from giml.maven.runner import MavenResult
from giml.store.sqlite_store import SqliteStateStore
from tests.git.repo_helpers import fingerprint, git
from tests.plan.test_analysis import NOW, repo  # noqa: F401

LOGS = Path(__file__).resolve().parents[1] / "fixtures" / "logs"
STAGE_OF = {"test-compile": "compile", "test": "unit_test", "validate": "enforcer"}


class Clock:
    """Ticks a second per call, so two runs never share a branch name."""

    def __init__(self):
        self.now = NOW

    def __call__(self):
        self.now += datetime.timedelta(seconds=1)
        return self.now


class StageMaven:
    """Answers each Maven stage from a queue of (kind, log text): kind is ok or fail; the last reply repeats."""

    def __init__(self, **replies):
        self.replies = {stage: list(queue) for stage, queue in replies.items()}
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, project, args, log, timeout, env):
        stage = STAGE_OF[args[0]]
        self.calls.append((stage, env))
        queue = self.replies.get(stage, [("ok", "[INFO] BUILD SUCCESS\n")])
        kind, text = queue.pop(0) if len(queue) > 1 else queue[0]
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(text)
        return MavenResult(tuple(args), 0 if kind == "ok" else 1, 1.5, log, False)

    @property
    def stages(self):
        return [stage for stage, _ in self.calls]


def fixture_log(name: str) -> tuple[str, str]:
    return "fail", (LOGS / f"{name}.log").read_text()


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """One ticking clock per test, shared by every plan() call in it."""
    shared = Clock()
    monkeypatch.setattr("tests.plan.test_plan_baseline.CLOCK", shared, raising=False)
    return shared


CLOCK = None


def plan(tmp_path, repo, maven, *extra):  # noqa: F811
    env = Environment(clock=CLOCK, environ={}, maven=maven)
    return main(["--state-dir", str(tmp_path / "state"), "plan", str(repo), *extra], env)


def runs(tmp_path):
    with SqliteStateStore(tmp_path / "state" / "state.db") as store:
        return store.list_runs()


def baseline_report(tmp_path, run):
    return json.loads((tmp_path / "state" / "reports" / run.id / "baseline.json").read_text())


def test_a_passing_baseline_runs_the_three_stages_and_records_everything(tmp_path, repo, capsys):  # noqa: F811
    maven, before = StageMaven(), fingerprint(repo)
    assert plan(tmp_path, repo, maven) == ExitCode.SUCCESS
    out = capsys.readouterr().out
    assert maven.stages == ["compile", "unit_test", "enforcer"]
    assert "baseline compile: passed (1.5 s)" in out and "baseline enforcer: passed (1.5 s)" in out
    assert "cache: 0 hit(s), 3 miss(es), 0.0 s saved" in out
    assert "stopped after baseline verification: planning arrives in milestone 5" in out
    (run,) = runs(tmp_path)
    assert run.stop_reason == "planning_not_implemented"
    report = baseline_report(tmp_path, run)
    assert report["baseline"]["upgradeable"] is True and report["baseline"]["enforcer"] == {"mode": "clean", "violations": []}
    assert [s["status"] for s in report["baseline"]["stages"]] == ["passed"] * 3
    assert report["rewind_from"] is None and len(report["tooling_added"]) == 3 and report["setup_commit"]
    assert report["jdk"]["source"] == "inherited" and report["cache"]["misses"] == 3
    tmp = tmp_path / "state" / "runs" / run.id / "tmp"
    assert all(env["TMPDIR"] == str(tmp) for _, env in maven.calls) and not tmp.exists()
    assert fingerprint(repo) == before


def test_the_baseline_runs_on_the_worktree_with_giml_setup_applied(tmp_path, repo, capsys):  # noqa: F811
    plan(tmp_path, repo, StageMaven())
    (run,) = runs(tmp_path)
    assert git(run.worktree_path, "log", "-1", "--format=%s").startswith("[giml-setup]")
    assert git(run.worktree_path, "status", "--porcelain") == ""


def test_an_identical_second_run_is_answered_from_the_cache(tmp_path, repo, capsys):  # noqa: F811
    maven = StageMaven()
    assert plan(tmp_path, repo, maven) == ExitCode.SUCCESS
    capsys.readouterr()
    assert plan(tmp_path, repo, maven) == ExitCode.SUCCESS
    out = capsys.readouterr().out
    assert maven.stages == ["compile", "unit_test", "enforcer"]  # Maven ran only for the first
    assert "baseline compile: passed (1.5 s, cached)" in out and "cache: 3 hit(s), 0 miss(es), 4.5 s saved" in out
    second = sorted(runs(tmp_path), key=lambda r: r.started_at)[-1]
    assert baseline_report(tmp_path, second)["cache"] == {"hits": 3, "misses": 0, "hit_rate": 1.0, "seconds_saved": 4.5,
                                                          "seconds_spent": 0.0, "stages": baseline_report(tmp_path, second)["cache"]["stages"]}  # fmt: skip


def test_a_new_commit_is_not_answered_from_the_cache(tmp_path, repo, capsys):  # noqa: F811
    maven = StageMaven()
    plan(tmp_path, repo, maven)
    (repo / "core" / "pom.xml").write_text((repo / "core" / "pom.xml").read_text() + "<!-- changed -->\n")
    git(repo, "commit", "-qam", "change")
    plan(tmp_path, repo, maven)
    assert maven.stages == ["compile", "unit_test", "enforcer"] * 2


def test_a_failing_build_stops_the_run_with_exit_5(tmp_path, repo, capsys):  # noqa: F811
    maven = StageMaven(compile=[fixture_log("compile-error-a")])
    assert plan(tmp_path, repo, maven) == ExitCode.CONFIGURATION
    captured = capsys.readouterr()
    assert "giml: error: the build fail at the base commit with giml's setup applied; see " in captured.err
    assert "baseline compile: baseline_failed (1.5 s, compile " in captured.out and "baseline unit_test: not_run (not run)" in captured.out
    (run,) = runs(tmp_path)
    assert (run.stop_reason, maven.stages) == ("baseline_failed", ["compile"])
    report = baseline_report(tmp_path, run)
    assert report["baseline"]["stop"] == "build_failed" and report["baseline"]["stages"][0]["failure_class"] == "compile"
    assert run.worktree_path.exists()  # left for inspection; `giml clean` removes it
    assert not (tmp_path / "state" / "runs" / run.id / "tmp").exists()


def test_failing_unit_tests_stop_the_run_before_the_enforcer(tmp_path, repo, capsys):  # noqa: F811
    maven = StageMaven(unit_test=[fixture_log("test-failure-a")])
    assert plan(tmp_path, repo, maven) == ExitCode.CONFIGURATION
    assert "the unit tests fail at the base commit" in capsys.readouterr().err
    assert maven.stages == ["compile", "unit_test"] and runs(tmp_path)[0].stop_reason == "baseline_failed"


def test_enforcer_violations_do_not_stop_the_run_they_become_the_reference(tmp_path, repo, capsys):  # noqa: F811
    maven = StageMaven(enforcer=[fixture_log("convergence-a")])
    assert plan(tmp_path, repo, maven) == ExitCode.SUCCESS
    out = capsys.readouterr().out
    assert "baseline enforcer: baseline_failed (1.5 s, enforcer_convergence " in out
    assert ("enforcer: 1 violation(s) at the baseline become the reference set: "
            "DependencyConvergence:com.fasterxml.jackson.core:jackson-core") in out  # fmt: skip
    (run,) = runs(tmp_path)
    assert run.stop_reason == "planning_not_implemented"
    enforcer = baseline_report(tmp_path, run)["baseline"]["enforcer"]
    assert enforcer["mode"] == "reference" and enforcer["violations"][0]["detail"]["versions"] == ["2.13.0", "2.15.0"]


def test_a_persistent_infrastructure_failure_is_exit_4_and_not_blamed_on_the_project(tmp_path, repo, capsys):  # noqa: F811
    network = ("fail", "[ERROR] Could not transfer artifact org.x:y:pom:1 from/to central: Connect timed out\n")
    maven = StageMaven(compile=[network])
    assert plan(tmp_path, repo, maven) == ExitCode.INFRASTRUCTURE
    assert "infrastructure failure at the baseline (not the project's fault, try again)" in capsys.readouterr().err
    assert maven.stages == ["compile", "compile"] and runs(tmp_path)[0].stop_reason == "baseline_infrastructure"


def test_a_transient_infrastructure_failure_is_retried(tmp_path, repo, capsys):  # noqa: F811
    network = ("fail", "[ERROR] Could not transfer artifact org.x:y:pom:1 from/to central: Connect timed out\n")
    maven = StageMaven(compile=[network, ("ok", "[INFO] BUILD SUCCESS\n")])
    assert plan(tmp_path, repo, maven) == ExitCode.SUCCESS
    assert maven.stages == ["compile", "compile", "unit_test", "enforcer"]


def test_a_failing_rewound_baseline_has_its_own_stop_reason(tmp_path, repo, capsys):  # noqa: F811
    (repo / "core" / "pom.xml").write_text((repo / "core" / "pom.xml").read_text() + "<!-- newer -->\n")
    git(repo, "commit", "-qam", "newer pom")
    old = git(repo, "rev-parse", "HEAD~1")
    maven = StageMaven(compile=[fixture_log("compile-error-a")])
    assert plan(tmp_path, repo, maven, "--rewind-to", "HEAD~1") == ExitCode.CONFIGURATION
    assert f"rewind point {old[:7]} is unusable: the build fail with its pom.xml files" in capsys.readouterr().err
    (run,) = runs(tmp_path)
    assert (run.stop_reason, run.rewind_from_sha) == ("rewind_baseline_failed", old)
    assert baseline_report(tmp_path, run)["rewind_from"] == old and baseline_report(tmp_path, run)["baseline"]["rewound"] is True


def test_a_passing_rewound_baseline_goes_on(tmp_path, repo, capsys):  # noqa: F811
    (repo / "core" / "pom.xml").write_text((repo / "core" / "pom.xml").read_text() + "<!-- newer -->\n")
    git(repo, "commit", "-qam", "newer pom")
    assert plan(tmp_path, repo, StageMaven(), "--rewind-to", "HEAD~1") == ExitCode.SUCCESS
    (run,) = runs(tmp_path)
    assert run.stop_reason == "planning_not_implemented" and baseline_report(tmp_path, run)["baseline"]["rewound"] is True


def test_a_full_disk_stops_before_the_worktree_exists(tmp_path, repo, capsys, monkeypatch):  # noqa: F811
    def full(path):
        raise InsufficientSpace(f"{path}: only 10 inodes free on this filesystem, need 20000")

    monkeypatch.setattr("giml.maven.isolation.check_space", full)
    assert plan(tmp_path, repo, StageMaven()) == ExitCode.INFRASTRUCTURE
    (run,) = runs(tmp_path)
    assert run.stop_reason == "setup_failed" and not run.worktree_path.exists()


def test_a_dirty_checkout_is_still_refused_before_anything_runs(tmp_path, repo, capsys):  # noqa: F811
    (repo / "core" / "pom.xml").write_text("dirty\n")
    maven = StageMaven()
    assert plan(tmp_path, repo, maven) == ExitCode.PREFLIGHT_REFUSAL and maven.calls == []
