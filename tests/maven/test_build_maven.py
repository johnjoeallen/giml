"""The stage runner with real Maven (slow: starts mvn and needs the plugins). Run with `pytest -m slow`."""

import os
import pwd
import shutil
from pathlib import Path

import pytest

from giml.maven.build import MavenBuildRunner
from giml.maven.failures import COMPILE
from giml.maven.isolation import isolated_env, run_temp
from giml.maven.runner import run_maven

pytestmark = pytest.mark.slow

POM = ('<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><groupId>t</groupId>'
       "<artifactId>broken</artifactId><version>1</version><properties><maven.compiler.release>17</maven.compiler.release>"
       "<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties></project>\n")
SOURCE = "package t;\npublic class App {\n  int f() { return undefinedSymbol; }\n}\n"


@pytest.fixture
def real_home(monkeypatch):
    # Maven must use the developer's real ~/.m2 (spec 9.2); the autouse isolated HOME would re-download every plugin.
    monkeypatch.setenv("HOME", str(Path(pwd.getpwuid(os.getuid()).pw_dir)))


def broken_project(root: Path) -> Path:
    (root / "src" / "main" / "java" / "t").mkdir(parents=True)
    (root / "pom.xml").write_text(POM)
    (root / "src" / "main" / "java" / "t" / "App.java").write_text(SOURCE)
    return root


def test_the_same_real_failure_has_the_same_signature_from_two_directories(tmp_path, real_home):
    if shutil.which("mvn") is None or shutil.which("java") is None:
        pytest.skip("mvn and java are required")
    outcomes = []
    for index, place in enumerate(("first", "deeper/second")):
        project = broken_project(tmp_path / place)
        with run_temp(tmp_path / f"state{index}", "run-1") as temp:
            env = isolated_env(None, os.environ, temp)
            outcomes.append(MavenBuildRunner(run_maven, env, tmp_path / f"logs{index}").run_stage(project, "compile", 600))
    first, second = outcomes
    assert (first.passed, first.failure_class) == (False, COMPILE)
    assert first.signature == second.signature and first.failure.key_lines == second.failure.key_lines
    assert str(tmp_path) not in "\n".join(first.failure.key_lines)
    assert "App.java:[<n>,<n>] cannot find symbol" in first.failure.key_lines
