import json
import re

import pytest

from giml.core.config import PlanningSettings
from giml.core.model import VersionRelease
from giml.maven.runner import MavenResult
from giml.plan.analysis import Sources
from tests.git.repo_helpers import fingerprint, git, make_repo
from tests.plan.test_analysis import (  # noqa: F401
    CONFIG, NOW, OLD, FakeAdvisories, assess_result, node, run, snapshot, store,
)

APP_POM = ('<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
           "<parent><groupId>org.boot</groupId><artifactId>starter-parent</artifactId><version>3.3.5</version></parent>\n"
           "<groupId>g</groupId><artifactId>app</artifactId><version>1</version>\n"
           "<dependencies><dependency><groupId>o</groupId><artifactId>lib</artifactId></dependency></dependencies></project>\n")

BY_PARENT = {  # what the reactor resolves to under each parent version
    "3.3.5": "2.17.1", "3.3.6": "2.17.3", "3.4.0": "2.18.0", "4.0.0": "3.0.0",
}  # fmt: skip


class ParentAwareMaven:
    """Resolves the way Maven would: the version of o:lib comes from the parent version in the POM."""

    def __init__(self, fail_for=(), lib_for=None):
        self.fail_for, self.lib_for, self.parents_seen, self.logs = set(fail_for), lib_for or BY_PARENT, [], []

    def __call__(self, project, args, log, timeout, env):
        version = re.search(r"<parent>.*?<version>([^<]+)</version>", (project / "pom.xml").read_text(), re.S).group(1)
        self.parents_seen.append(version)
        self.logs.append(log)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("fake mvn\n")
        ok = version not in self.fail_for
        if ok:
            target = project / "target"
            target.mkdir(parents=True, exist_ok=True)
            tree = node("g", "app", "1", [node("o", "lib", self.lib_for[version])])
            (target / "giml-tree.json").write_text(json.dumps(tree))
        return MavenResult(tuple(args), 0 if ok else 1, 0.1, log, False)


RELEASES = {
    "org.boot:starter-parent": ["3.3.5", "3.3.6", "3.4.0", "4.0.0"],
    "o:lib": ["2.17.1", "2.17.3", "2.18.0", "3.0.0"],
}


class Metadata:
    snapshot_id = "central-1"

    def __init__(self, without=()):
        self.without = set(without)

    def versions(self, coordinate):
        found = RELEASES.get(str(coordinate))
        return None if found is None or str(coordinate) in self.without else [VersionRelease(v, OLD) for v in found]


def sources(without=()):
    return Sources(lambda snapshot: FakeAdvisories(), lambda snapshot: Metadata(without))


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "pom.xml").write_text(APP_POM)
    make_repo(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    return repo


def unit_of(report):
    (unit,) = report["change_units"]
    return unit


def test_candidate_parents_are_resolved_with_their_version_in_place(repo, tmp_path, store):  # noqa: F811
    assess_result(store, repo)
    before, maven = fingerprint(repo), ParentAwareMaven()
    result = run(repo, tmp_path, store, maven=maven, sources=sources())
    unit = unit_of(result.report)
    assert maven.parents_seen == ["3.3.5", "3.3.6", "3.4.0"]  # today's tree, then each candidate; 4.0.0 is a major
    assert (unit["kind"], unit["coordinate"], unit["version"], unit["status"]) == ("parent", "org.boot:starter-parent", "3.3.5", "improves")
    assert unit["picks"] == [{"kind": "parent_patch", "version": "3.3.6"}]
    assert [(e["version"], e["level"], e["exposure"]["total"], e["cleared"]) for e in unit["evaluations"]] == [
        ("3.3.6", "patch", 0, ["GHSA-lib"]), ("3.4.0", "minor", 0, ["GHSA-lib"])]  # fmt: skip
    assert unit["skipped_majors"] == 1 and (unit["file"], unit["line"]) == ("pom.xml", 1)
    assert unit["reason"] == ("a patch bump (3.3.5 → 3.3.6) clears 1 of 1 advisories, leaving none; a candidate, not built (dry run)")
    assert fingerprint(repo) == before
    lib = next(d for d in result.report["dependencies"] if d["coordinate"] == "o:lib")
    assert lib["change"] == "pin"  # its version comes from the parent, so the dependency alone would need a pin
    assert [log.name for log in maven.logs] == ["01-tree.log", "02-parent-starter-parent-3.3.6.log", "03-parent-starter-parent-3.4.0.log"]


def test_the_analysis_leaves_no_trace_of_the_candidate_versions(repo, tmp_path, store):  # noqa: F811
    assess_result(store, repo)
    run(repo, tmp_path, store, maven=ParentAwareMaven(), sources=sources())
    assert (repo / "pom.xml").read_text() == APP_POM
    assert git(repo, "worktree", "list").count("\n") == 0 and git(repo, "status", "--porcelain") == ""


def test_the_latest_strategy_aims_for_the_newest_and_keeps_the_smallest_step(repo, tmp_path, store):  # noqa: F811
    assess_result(store, repo)
    options = PlanningSettings(7, 60, 120, "latest", "cve", "disallowed", 7, "disallowed")
    unit = unit_of(run(repo, tmp_path, store, options, maven=ParentAwareMaven(), sources=sources()).report)
    assert unit["picks"] == [{"kind": "parent_latest", "version": "3.4.0"}, {"kind": "parent_patch", "version": "3.3.6"}]


def test_major_parent_versions_are_evaluated_when_major_updates_are_allowed(repo, tmp_path, store):  # noqa: F811
    assess_result(store, repo)
    options = PlanningSettings(7, 60, 120, "conservative", "cve", "allowed", 7, "disallowed")
    maven = ParentAwareMaven()
    unit = unit_of(run(repo, tmp_path, store, options, maven=maven, sources=sources()).report)
    assert maven.parents_seen == ["3.3.5", "3.3.6", "3.4.0", "4.0.0"] and unit["skipped_majors"] == 0


def test_a_candidate_that_cannot_be_resolved_is_recorded_and_skipped(repo, tmp_path, store):  # noqa: F811
    assess_result(store, repo)
    unit = unit_of(run(repo, tmp_path, store, maven=ParentAwareMaven(fail_for=["3.3.6"]), sources=sources()).report)
    assert unit["evaluations"][0]["exposure"] is None and unit["evaluations"][0]["failed"].startswith("dependency resolution failed")
    assert unit["picks"] == [{"kind": "parent_minor", "version": "3.4.0"}]


def test_a_parent_without_central_metadata_asks_for_a_sync(repo, tmp_path, store):  # noqa: F811
    assess_result(store, repo)
    maven = ParentAwareMaven()
    report = run(repo, tmp_path, store, maven=maven, sources=sources(without=["org.boot:starter-parent"])).report
    assert unit_of(report)["status"] == "no_metadata" and maven.parents_seen == ["3.3.5"]
    assert report["missing_metadata"] == ["org.boot:starter-parent"]
    assert report["sync_command"] == "giml sync --central --coordinate org.boot:starter-parent"


def test_without_a_usable_tier_nothing_is_evaluated(repo, tmp_path, store):  # noqa: F811
    maven = ParentAwareMaven()
    unit = unit_of(run(repo, tmp_path, store, maven=maven, sources=sources()).report)
    assert (unit["status"], unit["note"], maven.parents_seen) == ("not_evaluated", "report only: no usable tier", ["3.3.5"])


def test_a_project_without_cves_evaluates_no_parent_versions(repo, tmp_path, store):  # noqa: F811
    assess_result(store, repo)
    maven = ParentAwareMaven(lib_for={**BY_PARENT, "3.3.5": "2.17.3"})
    unit = unit_of(run(repo, tmp_path, store, maven=maven, sources=sources()).report)
    assert (unit["status"], unit["note"], maven.parents_seen) == ("not_evaluated", "no CVE to fix", ["3.3.5"])


def test_the_summary_and_markdown_carry_the_parent_findings(repo, tmp_path, store):  # noqa: F811
    assess_result(store, repo)
    result = run(repo, tmp_path, store, maven=ParentAwareMaven(), sources=sources())
    text = result.markdown_path.read_text()
    assert "## Parent and BOM upgrades" in text and "| 3.3.6 | patch | none | 0 | 1 | 0 | 1 |" in text
