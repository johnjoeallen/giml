"""`giml plan` on a project whose newest dependency versions really break the build (slow, real Maven and javac).

The dependency lives in a throwaway file:// Maven repository built here: fx:lib 1.0.0 to 1.0.2 have
Lib.hello(); 1.1.0 and 1.2.0 renamed it, so the application no longer compiles against them. Only
1.0.0 is affected by the (fake) advisory. Run with `pytest -m slow`.
"""

import datetime
import json
import os
import pwd
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from giml.cli import Environment, ExitCode, main
from giml.core.config import default_gate_config_path
from giml.core.model import Coordinate, Finding, Severity, SeverityRating, SeveritySource, SnapshotInfo, VersionRelease
from giml.maven.runner import run_maven
from giml.maven.version import ComparableVersion
from giml.plan.analysis import Sources
from giml.store.sqlite_store import SqliteStateStore
from tests.git.repo_helpers import git, make_repo
from tests.plan.test_analysis import assess_result

pytestmark = pytest.mark.slow

VERSIONS = ["1.0.0", "1.0.1", "1.0.2", "1.1.0", "1.2.0"]
WHEN = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)


@pytest.fixture
def real_home(monkeypatch):
    monkeypatch.setenv("HOME", str(Path(pwd.getpwuid(os.getuid()).pw_dir)))


def lib_source(version: str) -> str:
    method = "hello" if version.startswith("1.0") else "greet"
    return f'package fx;\npublic class Lib {{ public static String {method}() {{ return "hi {version}"; }} }}\n'


def publish(repo: Path, version: str, work: Path) -> None:
    directory = repo / "fx" / "lib" / version
    directory.mkdir(parents=True)
    source = work / version / "fx" / "Lib.java"
    source.parent.mkdir(parents=True)
    source.write_text(lib_source(version))
    subprocess.run(["javac", "--release", "17", "-d", str(work / version / "out"), str(source)], check=True, capture_output=True)
    subprocess.run(["jar", "cf", str(directory / f"lib-{version}.jar"), "-C", str(work / version / "out"), "."], check=True)
    (directory / f"lib-{version}.pom").write_text(
        f'<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><groupId>fx</groupId>'
        f"<artifactId>lib</artifactId><version>{version}</version></project>")  # fmt: skip


APP_POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>fx</groupId>
  <artifactId>app</artifactId>
  <version>1</version>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
  <repositories>
    <repository><id>fx-local</id><url>file://{repo}</url></repository>
  </repositories>
  <dependencies>
    <dependency>
      <groupId>fx</groupId>
      <artifactId>lib</artifactId>
      <version>1.0.0</version>
    </dependency>
  </dependencies>
</project>
"""


class Advisories:
    snapshot_id = "osv-fx"

    def affecting(self, coordinate, version):
        if str(coordinate) == "fx:lib" and ComparableVersion(version) < ComparableVersion("1.0.2"):
            return [Finding("GHSA-fx", ("CVE-FX-1",), Severity(SeverityRating.HIGH, SeveritySource.LABEL), coordinate, version, "0", "1.0.2")]
        return []


class Metadata:
    snapshot_id = "central-fx"

    def versions(self, coordinate):
        return [VersionRelease(v, WHEN) for v in VERSIONS] if str(coordinate) == "fx:lib" else None


@pytest.fixture
def scenario(tmp_path, real_home):
    if shutil.which("mvn") is None or shutil.which("javac") is None:
        pytest.skip("mvn and a JDK are required")
    repo = tmp_path / "fxrepo"
    for version in VERSIONS:
        publish(repo, version, tmp_path / "work")
    project = tmp_path / "app"
    (project / "src" / "main" / "java" / "fx").mkdir(parents=True)
    (project / "pom.xml").write_text(APP_POM.format(repo=repo))
    (project / "src" / "main" / "java" / "fx" / "App.java").write_text(
        "package fx;\npublic class App { public String run() { return Lib.hello(); } }\n")  # fmt: skip
    make_repo(project)
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "fixture")
    (project / ".gitignore").write_text("target/\n")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "ignore target")
    state = tmp_path / "state"
    with SqliteStateStore(state / "state.db") as store:
        for source in ("osv", "central"):
            directory = tmp_path / f"{source}-snapshot"
            directory.mkdir()
            (directory / "manifest.json").write_text(json.dumps({"stats": {}}))
            store.record_snapshot(SnapshotInfo(f"{source}-fx", source, datetime.datetime.now(datetime.UTC), "hash", directory))
        assess_result(store, project)
    config = yaml.safe_load(default_gate_config_path().read_text())
    config["verification"]["pit"] = "off"
    config_path = tmp_path / "gate.yaml"
    config_path.write_text(yaml.safe_dump(config))
    env = Environment(maven=run_maven, sources=Sources(lambda snapshot: Advisories(), lambda snapshot: Metadata()))
    return project, state, config_path, env


def plan(scenario, *extra):
    project, state, config_path, env = scenario
    return main(["--state-dir", str(state), "plan", "--gate-config", str(config_path), str(project), *extra], env)


def report(state: Path) -> dict:
    (path,) = state.glob("reports/*/plan.json")
    return json.loads(path.read_text())


def test_latest_chops_back_past_versions_that_break_the_build(scenario, capsys):
    assert plan(scenario, "--strategy", "latest") == ExitCode.SUCCESS
    result = report(scenario[1])["result"]
    assert [c["label"] for c in result["committed"]] == ["fx:lib 1.0.0 → 1.0.2 (latest_in_major)"]
    failed = {(t["from"], t["to"], t["failure_class"]) for t in result["failed_transitions"]}
    assert failed == {("1.0.0", "1.2.0", "compile"), ("1.0.0", "1.1.0", "compile")}
    assert result["exposure_after"]["max_severity"] is None and result["enforcer_clean"]
    assert "failed: compile" in capsys.readouterr().out
    with SqliteStateStore(scenario[1] / "state.db") as store:
        assert {t.to_version for t in store.list_transitions("fx:lib")} == {"1.1.0", "1.2.0"}


def test_conservative_takes_the_patch_fix_without_meeting_the_breaking_versions(scenario):
    assert plan(scenario) == ExitCode.SUCCESS
    result = report(scenario[1])["result"]
    assert [c["kind"] for c in result["committed"]] == ["cve_patch"] and result["failed_transitions"] == []
    assert result["builds"] == 1
