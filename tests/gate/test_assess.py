"""Assessment orchestration with Maven and Java faked by writing the real report fixtures.

The real tools run in tests/gate/test_assess_maven.py (slow).
"""

import datetime
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from giml.cli import Environment, ExitCode, main
from giml.core.config import ConfigError, default_gate_config_path, load_gate_config
from giml.gate.assess import PrerequisiteError, UnknownTierError, assess, run_java
from giml.maven.isolation import InsufficientSpace
from giml.maven.runner import MavenResult
from giml.store.sqlite_store import SqliteStateStore
from tests.git.repo_helpers import fingerprint, git, make_repo
from tests.maven.test_jdk import make_catalog, make_jdk

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
REPORTS = FIXTURES / "reports"
T0 = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=datetime.UTC)
CONFIG = load_gate_config(default_gate_config_path())


class FakeMaven:
    """Writes the reports each Maven step would produce, from the real fixture reports."""

    def __init__(self, fail_on=(), flaky=False, enforcer_log=None, pit_truncated=False):
        self.fail_on, self.flaky, self.enforcer_log = set(fail_on), flaky, enforcer_log
        self.pit_truncated = pit_truncated
        self.calls: list[list[str]] = []
        self.envs: list = []

    def __call__(self, project: Path, args: list[str], log: Path, timeout: float, env) -> MavenResult:
        self.calls.append(args)
        self.envs.append(env)
        log.parent.mkdir(parents=True, exist_ok=True)
        step = "pit" if any("pitest" in a for a in args) else "copy" if any(":copy" in a for a in args) else args[0]
        ok = step not in self.fail_on
        text = f"fake mvn {' '.join(args)}\n"
        if step == "test" and ok:
            core = project / "core" / "target"
            (core / "surefire-reports").mkdir(parents=True, exist_ok=True)
            report = (REPORTS / "TEST-CalculatorTest.xml").read_text()
            if self.flaky and len([c for c in self.calls if c[0] == "test"]) == 2:
                report = report.replace('name="divides" classname="org.giml.fixture.core.CalculatorTest" time="0.001"/>',
                                        'name="divides" classname="org.giml.fixture.core.CalculatorTest"><failure/></testcase>')  # fmt: skip
            (core / "surefire-reports" / "TEST-CalculatorTest.xml").write_text(report)
            if "-Djacoco.skip=true" not in args:
                (core / "jacoco.exec").write_bytes(b"exec")
                (core / "site" / "jacoco").mkdir(parents=True, exist_ok=True)
                shutil.copy(REPORTS / "jacoco-core.xml", core / "site" / "jacoco" / "jacoco.xml")
            for module, cls in (("core", "core/Calculator"), ("app", "app/Greeter")):
                target = project / module / "target" / "classes" / "org" / "giml" / "fixture" / f"{cls}.class"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"class")
        elif step == "pit" and (ok or self.pit_truncated):
            reports = project / "core" / "target" / "pit-reports"
            reports.mkdir(parents=True, exist_ok=True)
            report = (REPORTS / "mutations-core.xml").read_text()
            (reports / "mutations.xml").write_text(report[: len(report) // 2] if self.pit_truncated else report)
        elif step == "copy" and ok:
            directory = next(a for a in args if a.startswith("-DoutputDirectory=")).split("=", 1)[1]
            Path(directory).mkdir(parents=True, exist_ok=True)
            (Path(directory) / "org.jacoco.cli-0.8.15-nodeps.jar").write_bytes(b"jar")
        elif step == "validate" and self.enforcer_log:
            text += self.enforcer_log
            ok = False
        log.write_text(text)
        return MavenResult(tuple(args), 0 if ok else 1, 0.1, log, False)


def fake_java(args, timeout, env):
    fake_java.envs.append(env)
    out = Path(args[args.index("--xml") + 1])
    shutil.copy(REPORTS / "jacoco-aggregate.xml", out)
    return subprocess.CompletedProcess(args, 0, "", "")


fake_java.envs = []


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "mini"
    shutil.copytree(FIXTURES / "maven" / "mini-reactor", repo)
    make_repo(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    return repo


def run(repo, tmp_path, maven=None, declared="B", java=fake_java, jdks=None, environ=None):
    with SqliteStateStore(tmp_path / "state" / "state.db") as store:
        outcome = assess(repo, tmp_path / "state", store, lambda: T0, CONFIG, declared, 60, maven or FakeMaven(), java,
                         jdks or make_catalog(tmp_path), environ or {})  # fmt: skip
        return outcome, store.list_runs(), store.latest_gate_result(store.list_projects()[0].id)


def test_full_assessment_of_the_mini_reactor(repo, tmp_path):
    before = fingerprint(repo)
    maven = FakeMaven()
    outcome, runs, stored = run(repo, tmp_path, maven)
    result = outcome.result
    assert result["measured"] == {"unit_line_coverage": 50.0, "unit_branch_coverage": 16.67,
                                  "pit_test_strength": 100.0, "pit_mutation_coverage": 62.5}  # fmt: skip
    assert result["untested_modules"] == ["app"]
    assert result["passed_tier"] is None and result["autonomy"] is None
    missed = {m["metric"] for m in result["failed_for_declared"]}
    assert missed == {"unit_line_coverage", "unit_branch_coverage", "pit_mutation_coverage", "untested_modules"}
    assert result["mutations"] == {"KILLED": 5, "NO_COVERAGE": 3}
    assert result["flaky_tests"] == [] and result["excluded_share"] == 0.0
    assert result["enforcer"]["status"] == "passed" and result["startup_check"] == "not_configured"
    assert result["integration_tests"] == {"count": 0, "failsafe_declared": False}
    assert len(result["tooling_added"]) == 3 and result["setup_commit"]
    assert result["config_version"] == 3 and result["declared_tier"] == "B"
    assert result["tools"]["jdk"] == {"version": None, "home": None, "source": "inherited"}
    run_id = outcome.run_id
    tmp = tmp_path / "state" / "runs" / run_id / "tmp"
    assert maven.envs and all(env["TMPDIR"] == str(tmp) and env["JAVA_TOOL_OPTIONS"] == f"-Djava.io.tmpdir={tmp}"
                              for env in maven.envs)  # fmt: skip
    assert not tmp.exists()  # removed when the run ended
    assert (tmp_path / "state" / "runs" / run_id / "logs" / "temp.log").read_text().startswith(f"removed {tmp}: ")
    assert result["expires"] == "2026-10-24T12:00:00+00:00"

    assert outcome.branch == f"giml/assess/{git(repo, 'rev-parse', 'HEAD')[:7]}/20260924T120000Z"
    assert git(outcome.worktree, "log", "-1", "--format=%s") == "[giml-setup] add quality tooling for assessment"
    assert json.loads(outcome.report_path.read_text()) == result
    assert [(r.kind, r.stop_reason) for r in runs] == [("assess", "assessed")]
    assert stored.earned_tier is None and json.loads(stored.json) == result
    test_runs = [c for c in maven.calls if c[0] == "test"]
    assert len(test_runs) == CONFIG.shared.flake_check_runs
    assert all("-Denforcer.skip=true" in c for c in test_runs)
    assert fingerprint(repo) == before


def test_a_full_disk_stops_the_assessment_before_anything_is_created(repo, tmp_path, monkeypatch):
    def full(path):
        raise InsufficientSpace(f"{path}: only 10 inodes free on this filesystem, need 20000")

    monkeypatch.setattr("giml.maven.isolation.check_space", full)
    with pytest.raises(InsufficientSpace, match="only 10 inodes free"):
        run(repo, tmp_path)
    with SqliteStateStore(tmp_path / "state" / "state.db") as store:
        assert [(r.kind, r.stop_reason) for r in store.list_runs()] == [("assess", "setup_failed")]
    assert git(repo, "worktree", "list").count("\n") == 0 and not list((tmp_path / "state" / "runs").glob("*/tmp"))


def test_the_run_temp_is_removed_when_the_assessment_fails(repo, tmp_path):
    with pytest.raises(PrerequisiteError):
        run(repo, tmp_path, FakeMaven(fail_on={"test"}))
    assert not list((tmp_path / "state" / "runs").glob("*/tmp"))


def test_flaky_test_is_reported(repo, tmp_path):
    outcome, _, _ = run(repo, tmp_path, FakeMaven(flaky=True))
    assert outcome.result["flaky_tests"] == ["org.giml.fixture.core.CalculatorTest#divides"]


def test_failing_tests_at_base_stop_the_assessment(repo, tmp_path):
    with pytest.raises(PrerequisiteError, match="fail at the base commit"):
        run(repo, tmp_path, FakeMaven(fail_on={"test"}))
    with SqliteStateStore(tmp_path / "state" / "state.db") as store:
        assert [r.stop_reason for r in store.list_runs()] == ["tests_failed"]


def test_pit_failure_makes_pit_metrics_unavailable(repo, tmp_path):
    outcome, _, _ = run(repo, tmp_path, FakeMaven(fail_on={"pit"}))
    result = outcome.result
    assert result["measured"]["pit_test_strength"] is None
    assert result["unavailable"]["pit_test_strength"].startswith("PIT failed; see ")
    assert result["mutations"] == {}


def test_pit_killed_mid_run_leaves_a_partial_report_that_is_not_read(repo, tmp_path):
    outcome, _, _ = run(repo, tmp_path, FakeMaven(fail_on={"pit"}, pit_truncated=True))
    assert outcome.result["measured"]["pit_mutation_coverage"] is None
    assert outcome.result["unavailable"]["pit_mutation_coverage"].startswith("PIT failed; see ")
    assert outcome.result["mutations"] == {}


def test_enforcer_baseline_failure_is_recorded_not_fatal(repo, tmp_path):
    log = ("[ERROR] Rule 0: org.apache.maven.enforcer.rules.dependency.DependencyConvergence failed with message:\n"
           "[ERROR] Rule 1: org.codehaus.mojo.extraenforcer.dependencies.BanDuplicateClasses failed with message:\n")  # fmt: skip
    outcome, _, _ = run(repo, tmp_path, FakeMaven(enforcer_log=log))
    enforcer = outcome.result["enforcer"]
    assert enforcer["status"] == "baseline_failed"
    assert enforcer["failed_rules"] == ["BanDuplicateClasses", "DependencyConvergence"]


def test_jacoco_cli_failure_makes_coverage_unavailable(repo, tmp_path):
    failing = lambda args, timeout, env: subprocess.CompletedProcess(args, 1, "", "boom")  # noqa: E731
    outcome, _, _ = run(repo, tmp_path, java=failing)
    assert outcome.result["measured"]["unit_line_coverage"] is None
    assert outcome.result["unavailable"]["unit_line_coverage"] == "jacoco cli failed: boom"


def test_jacoco_cli_is_fetched_once(repo, tmp_path):
    maven = FakeMaven()
    run(repo, tmp_path, maven)
    git(repo, "commit", "-q", "--allow-empty", "-m", "again")
    run(repo, tmp_path, maven)
    assert sum(1 for c in maven.calls if any(":copy" in a for a in c)) == 1


def test_unknown_declared_tier(repo, tmp_path):
    with pytest.raises(UnknownTierError, match="--declared-tier Z: not a tier"):
        run(repo, tmp_path, declared="Z")


def env_with(maven):
    return Environment(clock=lambda: T0, environ={}, maven=maven, java=fake_java)


def test_cli_assess_prints_json_and_exits_zero(repo, tmp_path, capsys):
    code = main(["--state-dir", str(tmp_path / "s"), "assess", str(repo)], env_with(FakeMaven()))
    captured = capsys.readouterr()
    assert code == ExitCode.SUCCESS
    assert json.loads(captured.out)["untested_modules"] == ["app"]
    assert "earned tier: none (propose nothing)" in captured.err
    assert "giml added quality tooling in commit" in captured.err


def test_cli_assess_failure_exit_codes(repo, tmp_path, capsys):
    env = env_with(FakeMaven(fail_on={"test"}))
    assert main(["--state-dir", str(tmp_path / "s"), "assess", str(repo)], env) == ExitCode.CONFIGURATION
    assert "fail at the base commit" in capsys.readouterr().err
    assert main(["--state-dir", str(tmp_path / "s2"), "assess", str(repo), "--declared-tier", "Q"], env) == ExitCode.CONFIGURATION
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 3\n")
    assert main(["--state-dir", str(tmp_path / "s3"), "assess", str(repo), "--gate-config", str(bad)], env) == ExitCode.CONFIGURATION


def commit_settings(repo, text):
    (repo / ".giml").mkdir()
    (repo / ".giml" / "settings.yml").write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "giml settings")


def test_project_settings_choose_the_jdk_for_maven_and_java(repo, tmp_path):
    home = make_jdk(tmp_path / "jdks" / "17", "17.0.16")
    make_jdk(tmp_path / "jdks" / "21", "21.0.9")
    commit_settings(repo, "jdk: 17\n")
    maven, fake_java.envs[:] = FakeMaven(), []
    outcome, _, _ = run(repo, tmp_path, maven, jdks=make_catalog(tmp_path, [tmp_path / "jdks" / "21", home]),
                        environ={"PATH": "/usr/bin", "JAVA_HOME": "/elsewhere"})  # fmt: skip
    assert outcome.result["tools"]["jdk"] == {"version": "17.0.16", "home": str(home), "source": "global config"}
    assert maven.envs and all(env["PATH"] == f"{home / 'bin'}:/usr/bin" and env["JAVA_HOME"] == str(home) for env in maven.envs)
    (java_env,) = fake_java.envs
    assert (java_env["PATH"], java_env["JAVA_HOME"]) == (f"{home / 'bin'}:/usr/bin", str(home)) and "TMPDIR" in java_env


def test_settings_are_read_from_the_base_commit_not_the_checkout(repo, tmp_path):
    commit_settings(repo, "java_home: /nowhere/jdk\n")
    # Delete it from the checkout, hidden from preflight's dirty check: giml must still see the committed file.
    git(repo, "update-index", "--assume-unchanged", ".giml/settings.yml")
    (repo / ".giml" / "settings.yml").unlink()
    with pytest.raises(ConfigError, match=r"java_home /nowhere/jdk is not a JDK"):
        run(repo, tmp_path)


def test_unknown_jdk_version_is_a_configuration_error(repo, tmp_path, capsys):
    commit_settings(repo, "jdk: 11\n")
    code = main(["--state-dir", str(tmp_path / "s"), "--config", str(tmp_path / "none.yml"), "assess", str(repo)],
                env_with(FakeMaven()))  # fmt: skip
    assert code == ExitCode.CONFIGURATION
    assert "jdk 11: no JDK 11 in" in capsys.readouterr().err
    with SqliteStateStore(tmp_path / "s" / "state.db") as store:
        assert [r.stop_reason for r in store.list_runs()] == ["setup_failed"]


def test_cli_config_flag_supplies_the_jdk_list(repo, tmp_path, capsys):
    home = make_jdk(tmp_path / "jdk", "21.0.9")
    config = tmp_path / "global.yml"
    config.write_text(f"jdks:\n  - {home}\n")
    commit_settings(repo, "jdk: '21'\n")
    code = main(["--state-dir", str(tmp_path / "s"), "--config", str(config), "assess", str(repo)], env_with(FakeMaven()))
    assert code == ExitCode.SUCCESS
    assert json.loads(capsys.readouterr().out)["tools"]["jdk"]["home"] == str(home)


def test_run_java_uses_the_java_on_the_given_path(tmp_path):
    bin_dir = tmp_path / "jdk" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "java").write_text('#!/bin/sh\necho "java $JAVA_HOME $*"\n')
    (bin_dir / "java").chmod(0o755)
    result = run_java(["-version"], 10, {"PATH": str(bin_dir), "JAVA_HOME": "/j"})
    assert (result.returncode, result.stdout) == (0, "java /j -version\n")
    assert result.args[0] == str(bin_dir / "java")
