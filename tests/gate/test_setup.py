import re

import pytest

from giml.gate.setup import (
    ENFORCER_EXECUTION_ID,
    apply_setup,
    inheritance_roots,
    setup_root,
    target_classes,
    SetupResult,
    tooling,
    uses_junit5,
)
from giml.git.preflight import preflight
from giml.git.worktrees import WorktreeManager
from giml.maven.pom_edit import find_plugin, parse
from giml.maven.project import discover_reactor
from tests.git.repo_helpers import git, make_repo

NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'
PARENT = f"""<project {NS}>
    <modelVersion>4.0.0</modelVersion>
    <groupId>com.example</groupId>
    <artifactId>parent</artifactId>
    <version>1.0</version>
    <packaging>pom</packaging>
    <modules>
        <module>core</module>
        <module>app</module>
    </modules>
</project>
"""


def module(artifact: str, parent: str = "<groupId>com.example</groupId><artifactId>parent</artifactId>") -> str:
    return f"<project {NS}>\n    <parent>{parent}<version>1.0</version></parent>\n    <artifactId>{artifact}</artifactId>\n</project>\n"


REACTOR = {
    "pom.xml": PARENT,
    "core/pom.xml": module("core"),
    "core/src/main/java/com/example/core/Core.java": "package com.example.core; class Core {}\n",
    "core/src/main/java/com/example/core/util/U.java": "package com.example.core.util; class U {}\n",
    "core/src/test/java/com/example/core/CoreTest.java": "import org.junit.jupiter.api.Test;\n",
    "app/pom.xml": module("app"),
    "app/src/main/java/com/example/app/App.java": "package com.example.app; class App {}\n",
}


@pytest.fixture
def reactor_worktree(tmp_path):
    repo = make_repo(tmp_path / "repo", REACTOR)
    state = preflight(repo)
    worktree = WorktreeManager(state, tmp_path / "state", "run-1").create_result("giml/assess/x/1")
    return repo, state, worktree


def test_tooling_versions_are_pinned():
    tools = tooling()
    assert tools["jacoco"].key == "org.jacoco:jacoco-maven-plugin" and tools["jacoco"].version == "0.8.15"
    assert tools["pitest"].version == "1.30.0" and tools["pitest_junit5"].version == "1.2.3"
    assert tools["enforcer"].version == "3.6.3" and tools["extra_enforcer_rules"].version == "1.12.1"


def test_reactor_facts(reactor_worktree):
    _, _, worktree = reactor_worktree
    reactor = discover_reactor(worktree)
    assert inheritance_roots(reactor) == [worktree / "pom.xml"]
    assert uses_junit5(reactor)
    assert target_classes(reactor) == ["com.example.app.*", "com.example.core.*"]


def test_module_with_external_parent_is_its_own_root(tmp_path):
    files = dict(REACTOR, **{"app/pom.xml": module("app", "<groupId>org.springframework.boot</groupId>"
                                                          "<artifactId>spring-boot-starter-parent</artifactId>")})  # fmt: skip
    repo = make_repo(tmp_path / "repo", files)
    reactor = discover_reactor(repo)
    assert inheritance_roots(reactor) == [repo / "pom.xml", repo / "app" / "pom.xml"]


def test_target_classes_edge_cases(tmp_path):
    for path in ["a/src/main/java/com/x/A.java", "b/src/main/java/com/x/deep/B.java", "c/src/main/java/C.java"]:
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("class X {}")
    poms = [tmp_path / m / "pom.xml" for m in "abc"]
    assert target_classes(poms[:2]) == ["com.x.*"]  # com.x.deep.* is covered by com.x.*
    assert target_classes(poms) == ["*"]  # default package
    assert target_classes([tmp_path / "none" / "pom.xml"]) == []
    assert not uses_junit5(poms)


def test_setup_adds_everything_to_the_root_only_and_commits(reactor_worktree):
    repo, state, worktree = reactor_worktree
    result = apply_setup(worktree, discover_reactor(worktree))
    assert result.changed_files == ["pom.xml"] and result.kept == []
    assert len(result.added) == 3
    assert git(worktree, "diff", "--name-only", f"{state.base_sha}..{result.commit}") == "pom.xml"
    text = (worktree / "pom.xml").read_text()
    root = parse(text)
    for key in ("jacoco", "pitest", "enforcer"):
        assert find_plugin(text, root, tooling()[key].group_id, tooling()[key].artifact_id) is not None
    assert "<param>com.example.app.*</param>" in text and "pitest-junit5-plugin" in text
    assert f"<id>{ENFORCER_EXECUTION_ID}</id>" in text and "extra-enforcer-rules" in text
    assert (worktree / "core" / "pom.xml").read_text() == module("core")
    message = git(worktree, "log", "-1", "--format=%B")
    assert message.startswith("[giml-setup] add quality tooling for assessment\n")
    assert "Cherry-pick this commit to keep the tooling" in message
    assert (repo / "pom.xml").read_text() == PARENT  # developer checkout untouched

    again = apply_setup(worktree, discover_reactor(worktree))
    assert again.commit is None and again.changed_files == [] and len(again.kept) == 3


def test_added_pit_skips_integration_tests(reactor_worktree):
    # Tiers measure unit tests; Surefire skips Failsafe-named tests, so PIT must too, or a *IT that
    # needs a packaged artifact fails PIT's green-suite check (found on arete).
    _, _, worktree = reactor_worktree
    apply_setup(worktree, discover_reactor(worktree))
    text = (worktree / "pom.xml").read_text()
    block = text[text.index("<excludedTestClasses>"):text.index("</excludedTestClasses>")]
    assert re.findall(r"<param>([^<]+)</param>", block) == ["*.IT*", "*IT", "*ITCase"]


def test_setup_output_is_readable_xml_with_matching_indentation(reactor_worktree):
    _, _, worktree = reactor_worktree
    apply_setup(worktree, discover_reactor(worktree))
    text = (worktree / "pom.xml").read_text()
    assert "\n    <build>\n        <plugins>\n            <plugin>\n                <groupId>org.jacoco</groupId>" in text
    assert "\t" not in text


def pom_with(build: str) -> str:
    return f"<project {NS}>\n    <artifactId>p</artifactId>\n    <build>\n{build}\n    </build>\n</project>\n"


def run_setup(text: str, tmp_path) -> tuple[str, SetupResult]:
    result = SetupResult()
    return setup_root(text, [tmp_path / "pom.xml"], "pom.xml", result), result


def test_existing_tooling_is_kept(tmp_path):
    build = """        <pluginManagement>
            <plugins>
                <plugin><groupId>org.pitest</groupId><artifactId>pitest-maven</artifactId></plugin>
            </plugins>
        </pluginManagement>
        <plugins>
            <plugin><groupId>org.jacoco</groupId><artifactId>jacoco-maven-plugin</artifactId></plugin>
            <plugin><artifactId>maven-enforcer-plugin</artifactId>
                <executions><execution><configuration><rules>
                    <banDuplicateClasses/><banDuplicatePomDependencyVersions/><dependencyConvergence/>
                </rules></configuration></execution></executions>
            </plugin>
        </plugins>"""
    text = pom_with(build)
    edited, result = run_setup(text, tmp_path)
    assert edited == text and result.added == [] and len(result.kept) == 3


def test_enforcer_without_bans_gets_giml_execution_inside_existing_declaration(tmp_path):
    text = pom_with("        <plugins>\n            <plugin>\n                <artifactId>maven-enforcer-plugin</artifactId>\n"
                    "                <version>3.0.0</version>\n            </plugin>\n        </plugins>")  # fmt: skip
    edited, result = run_setup(text, tmp_path)
    assert edited.count("<artifactId>maven-enforcer-plugin</artifactId>") == 1
    assert "<version>3.0.0</version>" in edited and f"<id>{ENFORCER_EXECUTION_ID}</id>" in edited
    assert "<dependencies>" in edited and "extra-enforcer-rules" in edited
    assert any("execution 'giml-bans'" in entry for entry in result.added)


def test_enforcer_only_in_plugin_management_is_declared_without_version(tmp_path):
    text = pom_with("        <pluginManagement>\n            <plugins>\n                <plugin>\n"
                    "                    <artifactId>maven-enforcer-plugin</artifactId>\n"
                    "                    <version>3.1.0</version>\n                </plugin>\n            </plugins>\n"
                    "        </pluginManagement>")  # fmt: skip
    edited, _ = run_setup(text, tmp_path)
    plugins_section = edited[edited.index("</pluginManagement>") :]
    assert "<artifactId>maven-enforcer-plugin</artifactId>" in plugins_section
    assert "<version>3.6.3</version>" not in plugins_section.split("maven-enforcer-plugin")[1].split("</plugin>")[0]


def test_crlf_pom_keeps_crlf(tmp_path):
    repo = make_repo(tmp_path / "repo")
    (repo / "pom.xml").write_bytes(pom_with("        <plugins>\n        </plugins>").replace("\n", "\r\n").encode())
    git(repo, "add", "pom.xml")
    git(repo, "commit", "-q", "-m", "crlf")
    worktree = WorktreeManager(preflight(repo), tmp_path / "state", "r").create_result("giml/assess/c/1")
    apply_setup(worktree, discover_reactor(worktree))
    data = (worktree / "pom.xml").read_bytes()
    assert b"\r\n" in data and data.count(b"\n") == data.count(b"\r\n")


def test_setup_commit_sits_directly_on_the_base_commit(reactor_worktree):
    _, state, worktree = reactor_worktree
    result = apply_setup(worktree, discover_reactor(worktree))
    assert result.commit is not None
    assert git(worktree, "rev-parse", "HEAD~1") == state.base_sha
