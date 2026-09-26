"""`giml plan` on a project whose newest dependency versions really break the build (slow, real Maven and javac).

The dependency lives in a throwaway file:// Maven repository built here: fx:lib 1.0.0 to 1.0.2 have
Lib.hello(); 1.1.0 and 1.2.0 renamed it, so the application no longer compiles against them. Only
1.0.0 is affected by the (fake) advisory. Run with `pytest -m slow`.
"""

import datetime
import json
import os
import re
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

VERSIONS = ["1.0.0", "1.0.1", "1.0.2", "1.1.0", "1.2.0", "2.0.0"]
WHEN = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)


@pytest.fixture
def real_home(monkeypatch):
    monkeypatch.setenv("HOME", str(Path(pwd.getpwuid(os.getuid()).pw_dir)))


def lib_source(version: str) -> str:
    if version == "1.0.3":  # compiles and has no unit tests to trip over, but cannot be loaded: only starting the application shows it
        return ('package fx;\npublic class Lib { static { if (true) throw new IllegalStateException("Lib 1.0.3 cannot initialise"); }\n'
                '  public static String hello() { return "hi 1.0.3"; } }\n')
    method = "hello" if version.startswith(("1.0", "2.")) else "greet"
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


SHADE = """  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-shade-plugin</artifactId>
        <version>3.6.2</version>
        <executions>
          <execution>
            <phase>package</phase>
            <goals><goal>shade</goal></goals>
            <configuration>
              <transformers>
                <transformer implementation="org.apache.maven.plugins.shade.resource.ManifestResourceTransformer">
                  <mainClass>fx.Main</mainClass>
                </transformer>
              </transformers>
            </configuration>
          </execution>
        </executions>
      </plugin>
    </plugins>
  </build>
"""

MAIN = """package fx;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;

public class Main {
    public static void main(String[] args) throws Exception {
        int port = Integer.parseInt(System.getProperty("fx.port", "8080"));
        String greeting = new App().run();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 0);
        server.createContext("/health", ex -> {
            byte[] body = ("{\\"status\\":\\"UP\\",\\"greeting\\":\\"" + greeting + "\\"}").getBytes();
            ex.sendResponseHeaders(200, body.length);
            ex.getResponseBody().write(body);
            ex.close();
        });
        server.start();
        System.out.println("Started Main in 0.1 seconds");
        Thread.sleep(600000);
    }
}
"""


def publish_other(repo: Path, version: str, work: Path) -> None:
    """fx:other 1.1.0 depends on fx:lib 1.0.1, so next to the application's own lib the tree has two versions of it."""
    directory = repo / "fx" / "other" / version
    directory.mkdir(parents=True)
    source = work / f"other-{version}" / "fx" / "Other.java"
    source.parent.mkdir(parents=True)
    source.write_text(f'package fx;\npublic class Other {{ public static String name() {{ return "other {version}"; }} }}\n')
    out = work / f"other-{version}" / "out"
    subprocess.run(["javac", "--release", "17", "-d", str(out), str(source)], check=True, capture_output=True)
    subprocess.run(["jar", "cf", str(directory / f"other-{version}.jar"), "-C", str(out), "."], check=True)
    dependency = ("<dependencies><dependency><groupId>fx</groupId><artifactId>lib</artifactId><version>1.0.1</version></dependency></dependencies>"
                  if version == "1.1.0" else "")  # fmt: skip
    (directory / f"other-{version}.pom").write_text(
        f'<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><groupId>fx</groupId>'
        f"<artifactId>other</artifactId><version>{version}</version>{dependency}</project>")  # fmt: skip


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
    <dependency>
      <groupId>fx</groupId>
      <artifactId>other</artifactId>
      <version>1.0.0</version>
    </dependency>
  </dependencies>
</project>
"""


class Advisories:
    """fx:lib below ``fixed`` is affected; ``worse`` versions carry a second, critical advisory."""

    snapshot_id = "osv-fx"

    def __init__(self, fixed="1.0.2", worse=()):
        self.fixed, self.worse = fixed, set(worse)

    def affecting(self, coordinate, version):
        if str(coordinate) != "fx:lib":
            return []
        found = []
        if ComparableVersion(version) < ComparableVersion(self.fixed):
            found.append(Finding("GHSA-fx", ("CVE-FX-1",), Severity(SeverityRating.HIGH, SeveritySource.LABEL), coordinate, version, "0", self.fixed))
        if version in self.worse:
            found.append(Finding("GHSA-worse", ("CVE-FX-2",), Severity(SeverityRating.CRITICAL, SeveritySource.LABEL), coordinate, version, "0", "9"))
        return found


class Metadata:
    snapshot_id = "central-fx"
    extra = ()

    def versions(self, coordinate):
        versions = {"fx:lib": [*VERSIONS, *self.extra], "fx:other": ["1.0.0", "1.1.0"], "fx:a": ["1.0.0", "1.1.0"], "fx:b": ["1.0.0", "1.1.0"]}.get(str(coordinate))
        return [VersionRelease(v, WHEN) for v in versions] if versions else None


def sources_for(extra_versions=(), **advisories) -> Sources:
    metadata = Metadata()
    metadata.extra = tuple(extra_versions)
    return Sources(lambda snapshot: Advisories(**advisories), lambda snapshot: metadata)


def publish_dup(repo: Path, artifact: str, version: str, work: Path) -> None:
    """fx:a and fx:b at 1.1.0 both contain class fx.Dup: each is fine next to the other's 1.0, together they duplicate it."""
    directory = repo / "fx" / artifact / version
    directory.mkdir(parents=True)
    out = work / f"{artifact}-{version}" / "out"
    classes = [artifact.upper()] + (["Dup"] if version == "1.1.0" else [])
    for name in classes:
        source = work / f"{artifact}-{version}" / "fx" / f"{name}.java"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"package fx;\npublic class {name} {{}}\n")
        subprocess.run(["javac", "--release", "17", "-d", str(out), str(source)], check=True, capture_output=True)
    subprocess.run(["jar", "cf", str(directory / f"{artifact}-{version}.jar"), "-C", str(out), "."], check=True)
    (directory / f"{artifact}-{version}.pom").write_text(
        f'<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><groupId>fx</groupId>'
        f"<artifactId>{artifact}</artifactId><version>{version}</version></project>")  # fmt: skip


@pytest.fixture
def scenario(tmp_path, real_home):
    return make_scenario(tmp_path, pair=False)


@pytest.fixture
def smoke_scenario(tmp_path, real_home):
    return make_scenario(tmp_path, pair=False, smoke=True)


@pytest.fixture
def pair_scenario(tmp_path, real_home):
    return make_scenario(tmp_path, pair=True)


def make_scenario(tmp_path, pair, smoke=False):
    if shutil.which("mvn") is None or shutil.which("javac") is None:
        pytest.skip("mvn and a JDK are required")
    repo = tmp_path / "fxrepo"
    for version in [*VERSIONS, "1.0.3"] if smoke else VERSIONS:
        publish(repo, version, tmp_path / "work")
    for version in ("1.0.0", "1.1.0"):
        publish_other(repo, version, tmp_path / "work")
    for artifact in ("a", "b"):
        for version in ("1.0.0", "1.1.0"):
            publish_dup(repo, artifact, version, tmp_path / "work")
    project = tmp_path / "app"
    (project / "src" / "main" / "java" / "fx").mkdir(parents=True)
    pom = APP_POM.format(repo=repo)
    if pair:
        extra = "".join(f"<dependency><groupId>fx</groupId><artifactId>{a}</artifactId><version>1.0.0</version></dependency>" for a in "ab")
        pom = pom.replace("  </dependencies>", f"    {extra}\n  </dependencies>")
    if smoke:
        pom = pom.replace("</project>", SHADE + "</project>")
        (project / ".giml").mkdir()
        (project / ".giml" / "settings.yml").write_text("smoke:\n  properties:\n    fx.port: ${PORT}\n  ready:\n    http: /health\n    contains: UP\n    timeout_seconds: 30\n")
        (project / "src" / "main" / "java" / "fx" / "Main.java").write_text(MAIN)
    (project / "pom.xml").write_text(pom)
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
    env = Environment(maven=run_maven, sources=sources_for())
    return project, state, config_path, env


def plan(scenario, *extra):
    project, state, config_path, env = scenario
    return main(["--state-dir", str(state), "plan", "--gate-config", str(config_path), str(project), *extra], env)


def report(state: Path) -> dict:
    (path,) = state.glob("reports/*/plan.json")
    return json.loads(path.read_text())


def test_latest_does_not_build_versions_whose_api_break_the_project_uses(scenario, capsys):
    assert plan(scenario, "--strategy", "latest") == ExitCode.SUCCESS
    result = report(scenario[1])["result"]
    assert [c["label"] for c in result["committed"]] == ["fx:lib 1.0.0 → 1.0.2 (latest_in_major)"]
    assert result["failed_transitions"] == [] and result["builds"] == 1  # japicmp saw Lib.hello() removed in 1.1.0 and 1.2.0
    assert {h["step"].split(" → ")[1].split(" ")[0] for h in result["api_hints"]} == {"1.1.0", "1.2.0"}
    assert all("fx.Lib.hello (METHOD_REMOVED)" in h["breaks"] for h in result["api_hints"])
    assert result["exposure_after"]["max_severity"] is None and result["enforcer_clean"]
    assert "built last: fx:lib 1.0.0 → 1.2.0" in capsys.readouterr().out


def test_latest_chops_back_past_versions_that_break_the_build_when_japicmp_has_no_evidence(scenario, capsys):
    (scenario[0] / "src" / "main" / "java" / "fx" / "App.java").write_text(
        "package fx;\npublic class App { public String run() { return String.valueOf(Lib.class.getName().length()) + Lib.hello(); } }\n")
    git(scenario[0], "commit", "-qam", "use Lib differently")
    scenario[3].java = lambda args, timeout, env=None: subprocess.CompletedProcess(args, 1, "", "no japicmp")  # japicmp fails: no evidence
    assert plan(scenario, "--strategy", "latest") == ExitCode.SUCCESS
    result = report(scenario[1])["result"]
    assert [c["label"] for c in result["committed"]] == ["fx:lib 1.0.0 → 1.0.2 (latest_in_major)"] and result["api_hints"] == []
    failed = {(t["from"], t["to"], t["failure_class"]) for t in result["failed_transitions"]}
    assert failed == {("1.0.0", "1.2.0", "compile"), ("1.0.0", "1.1.0", "compile")}
    with SqliteStateStore(scenario[1] / "state.db") as store:
        assert {t.to_version for t in store.list_transitions("fx:lib")} == {"1.1.0", "1.2.0"}


def test_conservative_takes_the_patch_fix_without_meeting_the_breaking_versions(scenario):
    assert plan(scenario) == ExitCode.SUCCESS
    result = report(scenario[1])["result"]
    assert [c["kind"] for c in result["committed"]] == ["cve_patch"] and result["failed_transitions"] == []
    assert result["builds"] == 1


def test_a_fix_that_only_exists_in_a_breaking_version_is_deferred_with_its_reason(scenario, capsys):
    scenario[3].sources = sources_for(fixed="1.1.0")
    assert plan(scenario) == ExitCode.NO_IMPROVEMENT
    result = report(scenario[1])["result"]
    (left,) = result["left"]
    assert result["committed"] == [] and left["coordinates"] == ["fx:lib"]
    assert left["reason"].startswith("no fixing version passed") and "compile" in left["reason"]
    assert result["exposure_after"]["max_severity"] == "HIGH"
    with SqliteStateStore(scenario[1] / "state.db") as store:
        (deferral,) = store.list_deferrals(store.list_projects()[0].id, open_only=True)
        assert (deferral.coordinate, deferral.held_at_version) == ("fx:lib", "1.0.0")
        assert [t.failure_class for t in store.list_transitions("fx:lib")] == ["compile"]


def test_a_version_that_brings_a_worse_advisory_fails_without_a_build(scenario):
    scenario[3].sources = sources_for(worse={"1.0.2"})
    assert plan(scenario) == ExitCode.NO_IMPROVEMENT
    result = report(scenario[1])["result"]
    failed = {(t["to"], t["failure_class"]) for t in result["failed_transitions"]}
    assert result["committed"] == [] and failed == {("1.0.2", "vulnerability_worse"), ("1.1.0", "compile")}
    assert result["builds"] == 1  # only 1.1.0 was built; 1.0.2 failed on its resolved tree


def test_a_general_update_that_splits_a_dependency_across_two_versions_fails_the_enforcer(scenario, capsys):
    assert plan(scenario, "--scope", "general") == ExitCode.SUCCESS
    result = report(scenario[1])["result"]
    assert [c["label"] for c in result["committed"]] == ["fx:lib 1.0.0 → 1.0.2 (cve_patch)"]
    (left,) = [e for e in result["left"] if e["coordinates"] == ["fx:other"]]
    assert "enforcer" in left["reason"] and result["enforcer_clean"]
    assert {(t["to"], t["failure_class"]) for t in result["failed_transitions"]} == {("1.1.0", "enforcer_convergence")}


def test_a_rewind_run_reaches_the_version_the_developer_moved_to_and_compares_the_three_states(scenario, capsys):
    project = scenario[0]
    pom = project / "pom.xml"
    pom.write_text(pom.read_text().replace("<version>1.0.0</version>\n    </dependency>\n    <dependency>", "<version>1.0.2</version>\n    </dependency>\n    <dependency>", 1))
    git(project, "commit", "-qam", "developer upgrades fx:lib")
    assert plan(scenario, "--rewind-to", "HEAD~1") == ExitCode.SUCCESS
    document = report(scenario[1])
    assert [c["label"] for c in document["result"]["committed"]] == ["fx:lib 1.0.0 → 1.0.2 (cve_patch)"]
    (row,) = [r for r in document["rewind_comparison"]["rows"] if r["coordinate"] == "fx:lib"]
    assert (row["rewound"]["versions"], row["result"]["versions"], row["base"]["versions"]) == (["1.0.0"], ["1.0.2"], ["1.0.2"])
    assert (row["rewound"]["advisories"], row["result"]["advisories"], row["base"]["advisories"]) == (1, 0, 0)
    assert "## Rewind comparison" in next(scenario[1].glob("reports/*/plan.md")).read_text()


def test_two_updates_that_only_clash_together_are_isolated_and_the_less_important_one_is_deferred(pair_scenario, capsys):
    assert plan(pair_scenario, "--scope", "general") == ExitCode.SUCCESS
    result = report(pair_scenario[1])["result"]
    labels = [c["label"] for c in result["committed"]]
    assert "fx:lib 1.0.0 → 1.0.2 (cve_patch)" in labels and any(l.startswith("fx:a ") for l in labels)
    assert not any(l.startswith("fx:b ") for l in labels)  # the later of the two clashing updates stays behind
    left = {e["coordinates"][0]: e["reason"] for e in result["left"]}
    assert "interacts with" in left["fx:b"] and "fx:a" in left["fx:b"]
    assert result["enforcer_clean"]


def resolved_majors(state: Path) -> set[str]:
    document = report(state)
    return {d["version"].split(".")[0] for d in document["dependencies"] if d["coordinate"] == "fx:lib"}


def test_a_major_only_fix_is_not_attempted_by_default_but_is_when_major_updates_are_allowed(scenario, capsys):
    scenario[3].sources = sources_for(fixed="2.0.0")
    assert plan(scenario) == ExitCode.NO_IMPROVEMENT
    result = report(scenario[1])["result"]
    (left,) = result["left"]
    assert result["committed"] == [] and "major" in left["reason"] and result["builds"] == 0  # blocked before any build
    assert resolved_majors(scenario[1]) == {"1"}
    import shutil as sh

    sh.rmtree(scenario[1] / "reports")
    assert plan(scenario, "--major-updates", "allowed") == ExitCode.SUCCESS
    allowed = report(scenario[1])["result"]
    assert [c["label"] for c in allowed["committed"]] == ["fx:lib 1.0.0 → 2.0.0 (cve_major)"] and allowed["exposure_after"]["max_severity"] is None


def only_versions_differ(before: str, after: str) -> bool:
    """The POMs are identical once every <version> is masked: the plan changed versions and nothing else (spec 8.4)."""
    mask = lambda text: re.sub(r"<version>[^<]*</version>", "<version/>", text)  # noqa: E731
    return mask(before) == mask(after)


def test_the_result_branch_only_touches_versions_and_pins(pair_scenario, capsys):
    assert plan(pair_scenario, "--scope", "general") == ExitCode.SUCCESS
    result = report(pair_scenario[1])["result"]
    branch = result["branch"]
    setup = git(pair_scenario[0], "log", "-1", "--format=%H", "--grep=^\\[giml-setup\\]", branch)  # tooling setup is its own commit (spec 3)
    before, after = git(pair_scenario[0], "show", f"{setup}:pom.xml"), git(pair_scenario[0], "show", f"{branch}:pom.xml")
    assert before != after and only_versions_differ(before, after)
    assert git(pair_scenario[0], "diff", "--stat", setup, branch).count("|") == 1  # one file: pom.xml
    assert git(pair_scenario[0], "log", "--format=%s", f"{setup}..{branch}").count("\n") + 1 == len(result["committed"])


def test_the_naive_comparison_counts_what_bumping_everything_to_latest_touches_and_gets(scenario, capsys):
    assert plan(scenario, "--compare-naive") == ExitCode.SUCCESS
    comparison = report(scenario[1])["result"]["naive_comparison"]
    naive, giml = comparison["naive"], comparison["giml"]
    assert naive["touched"] == 2 and naive["passed"] == 1 and naive["failed"] == 1  # lib to 2.0.0 passes; other splits lib's versions
    assert {b["to"]: b["failed_stage"] for b in naive["bumps"]} == {"2.0.0": None, "1.1.0": "enforcer"}
    assert naive["cleared"] == ["GHSA-fx"] and giml["cleared"] == ["GHSA-fx"] and giml["touched"] == 1
    assert "naive (each dependency to its newest, on its own): 2 touched, 1 passed" in capsys.readouterr().out


def test_a_version_that_only_fails_when_the_application_starts_is_chopped_back_from(smoke_scenario, capsys):
    smoke_scenario[3].sources = sources_for(extra_versions=["1.0.3"])
    assert plan(smoke_scenario, "--strategy", "latest") == ExitCode.SUCCESS
    document = report(smoke_scenario[1])
    result = document["result"]
    assert [c["label"] for c in result["committed"]] == ["fx:lib 1.0.0 → 1.0.2 (latest_in_major)"]
    assert {(t["to"], t["failure_class"]) for t in result["failed_transitions"]} == {("1.0.3", "startup")}
    baseline = json.loads(next(smoke_scenario[1].glob("reports/*/baseline.json")).read_text())["baseline"]
    assert [s["stage"] for s in baseline["stages"]] == ["compile", "unit_test", "enforcer", "startup"]
    assert [s["status"] for s in baseline["stages"]] == ["passed"] * 4
    assert "failed: startup" in capsys.readouterr().out
