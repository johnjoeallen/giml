from pathlib import Path

import pytest

from giml.gate.assess import _startup_check
from giml.maven.build import StageOutcome
from giml.maven.failures import STARTUP, UNAVAILABLE, failure_from_lines
from giml.maven.runner import MavenResult


class Session:
    def __init__(self, project, package_ok=True):
        self.project_dir, self.logs, self.env, self.package_ok, self.goals = project, project / "logs", {"JAVA_HOME": "/jdk"}, package_ok, []

    def mvn(self, name, args):
        self.goals.append((name, args))
        return MavenResult(tuple(args), 0 if self.package_ok else 1, 0.1, self.logs / f"{name}.log", False)


def configure(project, text="smoke:\n  profile: smoke\n"):
    (project / ".giml").mkdir()
    (project / ".giml" / "settings.yml").write_text(text)


def outcome(passed, failure_class=None):
    failure = None if passed else failure_from_lines(failure_class, ["why"])
    return StageOutcome("startup", passed, 1.0, Path("/x.log"), failure)


def check(monkeypatch, project, session, result):
    monkeypatch.setattr("giml.smoke.runner.SmokeRunner.run", lambda self, worktree, trial=0: result)
    return _startup_check(session, "p-c282ac5d", "run-1", {"PATH": "/usr/bin"})


def test_a_project_without_smoke_settings_has_no_startup_check(tmp_path):
    assert _startup_check(Session(tmp_path), "p-c282ac5d", "run-1", {}) == "not_configured"


def test_a_booting_application_is_verified_after_packaging(tmp_path, monkeypatch):
    configure(tmp_path)
    session = Session(tmp_path)
    assert check(monkeypatch, tmp_path, session, outcome(True)) == "verified"
    assert session.goals[0][0] == "package" and session.goals[0][1][0] == "package"


def test_packaging_that_fails_or_an_application_that_does_not_boot_is_a_baseline_failure(tmp_path, monkeypatch):
    configure(tmp_path)
    assert check(monkeypatch, tmp_path, Session(tmp_path, package_ok=False), outcome(True)) == "baseline_failed"
    assert check(monkeypatch, tmp_path, Session(tmp_path), outcome(False, STARTUP)) == "baseline_failed"


def test_a_database_that_cannot_be_provided_makes_the_check_unavailable_not_failed(tmp_path, monkeypatch):
    configure(tmp_path)
    assert check(monkeypatch, tmp_path, Session(tmp_path), outcome(False, UNAVAILABLE)) == "unavailable"
