"""`giml plan` verifies the baseline with real Maven (slow). Run with `pytest -m slow`."""

import json
import os
import pwd
import shutil
from pathlib import Path

import pytest

from giml.cli import Environment, ExitCode, main
from giml.maven.runner import run_maven
from tests.git.repo_helpers import fingerprint, git, make_repo

pytestmark = pytest.mark.slow

MINI = Path(__file__).resolve().parents[1] / "fixtures" / "maven" / "mini-reactor"


@pytest.fixture
def real_home(monkeypatch):
    # Maven must use the developer's real ~/.m2 (spec 9.2); the autouse isolated HOME would re-download every plugin.
    monkeypatch.setenv("HOME", str(Path(pwd.getpwuid(os.getuid()).pw_dir)))


class CountingMaven:
    def __init__(self):
        self.runs = 0

    def __call__(self, *args, **kwargs):
        self.runs += 1
        return run_maven(*args, **kwargs)


def project(tmp_path) -> Path:
    repo = tmp_path / "mini"
    shutil.copytree(MINI, repo)
    make_repo(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    return repo


def test_a_repeated_real_baseline_is_answered_entirely_from_the_cache(tmp_path, real_home, capsys):
    if shutil.which("mvn") is None or shutil.which("java") is None:
        pytest.skip("mvn and java are required")
    repo, state, maven = project(tmp_path), tmp_path / "state", CountingMaven()
    before = fingerprint(repo)
    reports = []
    for _ in range(2):
        env = Environment(maven=maven)
        assert main(["--state-dir", str(state), "plan", str(repo)], env) == ExitCode.SUCCESS
        out = capsys.readouterr().out
        report = next(line.split(": ", 1)[1] for line in out.splitlines() if line.startswith("baseline report: "))
        reports.append((out, json.loads(Path(report).read_text())))
    (first_out, first), (second_out, second) = reports
    assert maven.runs == 3  # only the first run started Maven: compile, unit_test, enforcer
    assert (first["cache"]["hits"], first["cache"]["misses"]) == (0, 3)
    assert (second["cache"]["hits"], second["cache"]["misses"], second["cache"]["hit_rate"]) == (3, 0, 1.0)
    assert second["cache"]["seconds_saved"] == pytest.approx(first["cache"]["seconds_spent"])
    assert "baseline compile: passed" in first_out and "(cached)" not in first_out
    assert second_out.count(", cached)") == 3
    assert [s["status"] for s in second["baseline"]["stages"]] == ["passed"] * 3 and second["baseline"]["enforcer"]["mode"] == "clean"
    assert fingerprint(repo) == before
    leftovers = list((state / "runs").glob("*/tmp"))
    assert leftovers == []


def test_a_project_that_does_not_compile_is_not_upgradeable(tmp_path, real_home, capsys):
    if shutil.which("mvn") is None or shutil.which("java") is None:
        pytest.skip("mvn and java are required")
    repo = project(tmp_path)
    (repo / "core" / "src" / "main" / "java" / "org" / "giml" / "fixture" / "core" / "Calculator.java").write_text(
        "package org.giml.fixture.core;\npublic class Calculator { int broken() { return undefinedSymbol; } }\n")  # fmt: skip
    git(repo, "commit", "-qam", "break the build")
    assert main(["--state-dir", str(tmp_path / "state"), "plan", str(repo)], Environment()) == ExitCode.CONFIGURATION
    captured = capsys.readouterr()
    assert "the build fail at the base commit" in captured.err
    assert "baseline compile: baseline_failed" in captured.out and "compile " in captured.out
