import datetime
import json

import pytest

from giml.cli import Environment, ExitCode, main
from giml.maven.isolation import InsufficientSpace
from giml.store.sqlite_store import SqliteStateStore
from tests.git.repo_helpers import fingerprint, git
from tests.plan.test_analysis import NOW, SOURCES, CORE_POM, FakeMaven, assess_result, repo, snapshot  # noqa: F401


class Clock:
    def __call__(self):
        return NOW


def env(maven=None, sources=SOURCES):
    return Environment(clock=Clock(), environ={}, maven=maven or FakeMaven(), sources=sources)


@pytest.fixture
def state(tmp_path, repo):  # noqa: F811
    directory = tmp_path / "state"
    with SqliteStateStore(directory / "state.db") as store:
        store.record_snapshot(snapshot("osv"))
        store.record_snapshot(snapshot("central"))
        assess_result(store, repo)
    return directory


def plan(state, repo, *extra, environment=None):
    return main(["--state-dir", str(state), "plan", "--dry-run", str(repo), *extra], environment or env())


def test_dry_run_prints_a_summary_and_writes_the_report(state, repo, capsys):
    before = fingerprint(repo)
    assert plan(state, repo) == ExitCode.SUCCESS
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines[0] == "dry run: proj at " + git(repo, "rev-parse", "--short=7", "HEAD") + " (tier A; strategy conservative, scope cve, major updates disallowed)"
    assert any(line.startswith("  o:lib 2.17.1: CVE-2026-1 fixed by a patch bump") for line in lines)
    report_line = next(line for line in lines if line.startswith("report: "))
    path = report_line.removeprefix("report: ")
    assert path.endswith("report.md") and "## CVE-affected dependencies" in open(path).read()
    stored = json.loads(open(path.replace("report.md", "report.json")).read())
    assert stored["kind"] == "dry_run" and stored["planning"]["scope"] == "cve"
    assert captured.err == "" and fingerprint(repo) == before


def test_options_override_the_config(state, repo, capsys):
    assert plan(state, repo, "--strategy", "latest", "--scope", "general", "--major-updates", "allowed") == ExitCode.SUCCESS
    out = capsys.readouterr().out
    assert "strategy latest, scope general, major updates allowed" in out


def test_the_test_scope_option_overrides_the_config(state, repo, capsys):
    assert plan(state, repo, "--major-updates-test-scope", "allowed") == ExitCode.SUCCESS
    assert "major updates disallowed, test-scope majors allowed)" in capsys.readouterr().out


def test_no_assessment_says_so_on_stderr_and_proposes_nothing(tmp_path, repo, capsys):
    state = tmp_path / "state"
    with SqliteStateStore(state / "state.db") as store:
        store.record_snapshot(snapshot("osv"))
        store.record_snapshot(snapshot("central"))
    assert plan(state, repo) == ExitCode.SUCCESS
    captured = capsys.readouterr()
    assert "tier none, report only" in captured.out
    assert "warning: no assessment; run `giml assess <path>`" in captured.err


def test_stale_snapshot_warnings_go_to_stderr(tmp_path, repo, capsys):
    state = tmp_path / "state"
    with SqliteStateStore(state / "state.db") as store:
        store.record_snapshot(snapshot("osv", age_days=30))
        store.record_snapshot(snapshot("central"))
        assess_result(store, repo)
    assert plan(state, repo) == ExitCode.SUCCESS
    assert "warning: osv snapshot osv-1 is 30 days old (limit 7); run `giml sync`" in capsys.readouterr().err


def test_missing_osv_snapshot_is_a_configuration_error(tmp_path, repo, capsys):
    assert plan(tmp_path / "state", repo) == ExitCode.CONFIGURATION
    assert "giml: error: no OSV snapshot; run `giml sync`" in capsys.readouterr().err


def test_failed_resolution_is_a_configuration_error(state, repo, capsys):
    assert plan(state, repo, environment=env(FakeMaven(succeed=False))) == ExitCode.CONFIGURATION
    assert "giml: error: dependency resolution failed; see " in capsys.readouterr().err


def test_a_full_disk_is_an_infrastructure_failure(state, repo, capsys, monkeypatch):
    def full(path):
        raise InsufficientSpace(f"{path}: only 10 inodes free on this filesystem, need 20000")

    monkeypatch.setattr("giml.maven.isolation.check_space", full)
    assert plan(state, repo) == ExitCode.INFRASTRUCTURE
    assert "giml: error: " in capsys.readouterr().err


def test_a_dirty_checkout_is_refused(state, repo, capsys):
    (repo / "core" / "pom.xml").write_text(CORE_POM + "<!-- edit -->\n")
    assert plan(state, repo) == ExitCode.PREFLIGHT_REFUSAL
    assert "giml: refused:" in capsys.readouterr().err


def test_rewind_is_not_yet_combined_with_a_dry_run(state, repo, capsys):
    assert plan(state, repo, "--rewind-to", "HEAD") == ExitCode.CONFIGURATION
    assert "--rewind-to is not supported with --dry-run yet" in capsys.readouterr().err


@pytest.mark.parametrize("flag", [["--strategy", "latest"], ["--scope", "general"], ["--major-updates", "allowed"],
                                  ["--major-updates-test-scope", "allowed"]])  # fmt: skip
def test_planning_options_need_a_dry_run_until_planning_exists(state, repo, capsys, flag):
    assert main(["--state-dir", str(state), "plan", str(repo), *flag], env()) == ExitCode.CONFIGURATION
    assert "apply to planning; use --dry-run" in capsys.readouterr().err


def test_a_bad_gate_config_is_a_configuration_error(state, repo, tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 3\n")
    assert plan(state, repo, "--gate-config", str(bad)) == ExitCode.CONFIGURATION


def test_invalid_option_values_are_refused_by_the_parser(state, repo):
    with pytest.raises(SystemExit):
        plan(state, repo, "--scope", "everything")
