import datetime
import json
from pathlib import Path

import pytest

from giml import workspace
from giml.core.config import PlanningSettings, default_gate_config_path, load_gate_config
from giml.core.model import Coordinate, Finding, GateResultRecord, ProjectRecord, RunRecord, Severity, SeverityRating
from giml.core.model import SeveritySource, SnapshotInfo, VersionRelease
from giml.git.preflight import PreflightRefusal, preflight
from giml.maven.isolation import InsufficientSpace
from giml.maven.jdk import JdkCatalog
from giml.maven.runner import MavenResult
from giml.maven.tree import ResolutionError
from giml.maven.version import ComparableVersion
from giml.plan.analysis import MissingSnapshotError, Sources, dry_run, newest_release, tier_status
from giml.store.sqlite_store import SqliteStateStore
from tests.git.repo_helpers import fingerprint, git, make_repo
from tests.maven.test_jdk import make_catalog, make_jdk

NOW = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.UTC)
CONFIG = load_gate_config(default_gate_config_path())
OLD = NOW - datetime.timedelta(days=90)

ROOT_POM = ('<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><groupId>g</groupId>'
            "<artifactId>root</artifactId><version>1</version><packaging>pom</packaging><modules><module>core</module></modules></project>\n")  # fmt: skip
CORE_POM = ('<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><parent><groupId>g</groupId>'
            "<artifactId>root</artifactId><version>1</version></parent><artifactId>core</artifactId>\n"
            "<properties><lib.version>2.17.1</lib.version></properties>\n<dependencies>\n"
            "<dependency><groupId>o</groupId><artifactId>lib</artifactId><version>${lib.version}</version></dependency>\n"
            "<dependency><groupId>o</groupId><artifactId>other</artifactId><version>1.0</version></dependency>\n"
            "</dependencies></project>\n")  # fmt: skip


def node(group, artifact, version, children=(), scope="compile"):
    return {"groupId": group, "artifactId": artifact, "version": version, "type": "jar", "scope": scope,
            "classifier": "", "optional": "false", "children": list(children)}  # fmt: skip


TREES = {
    ".": node("g", "root", "1"),
    "core": node("g", "core", "1", [node("o", "lib", "2.17.1"), node("o", "other", "1.0", [node("o", "deep", "3.0")])]),
}


class FakeMaven:
    """Writes each module's tree file the way the dependency plugin does."""

    def __init__(self, trees=None, succeed=True):
        self.trees, self.succeed, self.calls = TREES if trees is None else trees, succeed, []

    def __call__(self, project, args, log, timeout, env):
        self.calls.append((project, args, env))
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("fake mvn\n")
        if self.succeed:
            for module, tree in self.trees.items():
                target = project / module / "target"
                target.mkdir(parents=True, exist_ok=True)
                (target / "giml-tree.json").write_text(json.dumps(tree))
        return MavenResult(tuple(args), 0 if self.succeed else 1, 0.1, log, False)


class FakeAdvisories:
    snapshot_id = "osv-1"

    def affecting(self, coordinate, version):
        if str(coordinate) == "o:lib" and ComparableVersion(version) < ComparableVersion("2.17.3"):
            return [Finding("GHSA-lib", ("CVE-2026-1",), Severity(SeverityRating.HIGH, SeveritySource.CVSS_V3, 7.5),
                            coordinate, version, "0", "2.17.3")]  # fmt: skip
        return []


RELEASES = {
    "o:lib": ["2.17.1", "2.17.2", "2.17.3", "2.18.0", "3.0.0", "3.1.0-rc1"],
    "o:other": ["1.0", "1.1", "1.1.1", "2.0"],
}


class FakeMetadata:
    snapshot_id = "central-1"

    def versions(self, coordinate):
        found = RELEASES.get(str(coordinate))
        return None if found is None else [VersionRelease(v, OLD) for v in found]


SOURCES = Sources(lambda snapshot: FakeAdvisories(), lambda snapshot: FakeMetadata())


def snapshot(source, age_days=1) -> SnapshotInfo:
    return SnapshotInfo(f"{source}-1", source, NOW - datetime.timedelta(days=age_days), "hash", Path("/nowhere"))


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "proj"
    (repo / "core").mkdir(parents=True)
    (repo / "pom.xml").write_text(ROOT_POM)
    (repo / "core" / "pom.xml").write_text(CORE_POM)
    make_repo(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    return repo


@pytest.fixture
def store(tmp_path):
    with SqliteStateStore(tmp_path / "state" / "state.db") as store:
        store.record_snapshot(snapshot("osv"))
        store.record_snapshot(snapshot("central"))
        yield store


def assess_result(store, repo, earned="A", version=3, measured=NOW - datetime.timedelta(days=2), expires=None,
                  base_sha=None, declared="A"):  # fmt: skip
    state = preflight(repo)
    store.save_project(ProjectRecord(state.project_key, state.project_dir, None, NOW))
    store.start_run(RunRecord("assess-run", state.project_key, base_sha or state.base_sha, "b", Path("/w"), measured, kind="assess"))
    store.save_gate_result(GateResultRecord("assess-run", state.project_key, base_sha or state.base_sha, version, earned,
                                            json.dumps({"declared_tier": declared}), measured,
                                            expires or NOW + datetime.timedelta(days=28)))  # fmt: skip


def run(repo, tmp_path, store, options=None, maven=None, **kw):
    options = options or CONFIG.planning
    return dry_run(repo, tmp_path / "state", store, lambda: NOW, CONFIG, options, maven=maven or FakeMaven(),
                   jdks=kw.pop("jdks", None) or make_catalog(tmp_path), environ=kw.pop("environ", {}),
                   sources=kw.pop("sources", SOURCES), **kw)  # fmt: skip


def entry(report, coordinate):
    return next(d for d in report["dependencies"] if d["coordinate"] == coordinate)


def test_dry_run_of_a_project_with_a_valid_tier(repo, tmp_path, store):
    assess_result(store, repo)
    before = fingerprint(repo)
    branches = git(repo, "branch", "--list")
    result = run(repo, tmp_path, store)
    report = result.report
    assert (report["kind"], report["project"], report["proposals_allowed"]) == ("dry_run", "proj", True)
    assert report["tier"]["earned"] == "A" and report["tier"]["declared"] == "A" and report["tier"]["note"] == ""
    assert report["snapshots"] == {"osv": "osv-1", "central": "central-1"} and report["warnings"] == []
    assert report["planning"] == {"strategy": "conservative", "scope": "cve", "major_updates": "disallowed",
                                  "major_updates_test_scope": "disallowed", "release_cooldown_days": 7}  # fmt: skip
    assert [d["coordinate"] for d in report["dependencies"]] == ["o:lib", "o:deep", "o:other"]
    lib = entry(report, "o:lib")
    assert (lib["status"], lib["change"], lib["direct"], lib["modules"]) == ("fix_available", "edit", True, ["g:core"])
    assert [(c["kinds"], c["version"], c["blocked"] is not None) for c in lib["candidates"]] == [
        (["cve_patch"], "2.17.3", False), (["cve_minor"], "2.18.0", False), (["cve_major"], "3.0.0", True)]  # fmt: skip
    assert lib["sites"] == [{"file": "core/pom.xml", "line": 2, "kind": "property", "name": "lib.version"}]
    assert lib["advisories"][0]["fixed"] == "2.17.3" and lib["latest_available"] == "3.0.0"
    assert entry(report, "o:other")["status"] == "unchanged_scope" and entry(report, "o:other")["latest_available"] == "2.0"
    deep = entry(report, "o:deep")
    assert (deep["status"], deep["direct"], deep["latest_available"]) == ("unchanged_scope", False, None)
    assert report["summary"]["cve_affected"] == 1 and report["missing_metadata"] == []
    assert report["exposure"] == {"max_severity": "HIGH", "at_max": 1, "total": 1}
    # files, run record and cleanliness
    assert json.loads(result.json_path.read_text(encoding="utf-8")) == report
    assert result.markdown_path.read_text(encoding="utf-8").startswith("# giml dry run: proj")
    assert result.json_path == tmp_path / "state" / "reports" / result.run_id / "report.json"
    (run_record,) = [r for r in store.list_runs() if r.kind == "plan"]
    assert (run_record.id, run_record.stop_reason, run_record.branch) == (result.run_id, "dry_run", "")
    assert not run_record.worktree_path.exists()
    assert fingerprint(repo) == before and git(repo, "branch", "--list") == branches


def test_maven_is_asked_only_for_the_dependency_trees(repo, tmp_path, store):
    maven = FakeMaven()
    run(repo, tmp_path, store, maven=maven)
    (project, args, env), = maven.calls
    assert args[0].endswith(":tree") and "-DoutputType=json" in args
    tmp = tmp_path / "state" / "runs" / next((tmp_path / "state" / "runs").iterdir()).name / "tmp"
    assert env["TMPDIR"] == str(tmp) and env["JAVA_TOOL_OPTIONS"] == f"-Djava.io.tmpdir={tmp}" and not tmp.exists()
    assert project.name.endswith("-trial-0") and not project.exists()


def test_the_projects_jdk_is_used_for_resolution(repo, tmp_path, store):
    home = make_jdk(tmp_path / "jdks" / "17", "17.0.16")
    (repo / ".giml").mkdir()
    (repo / ".giml" / "settings.yml").write_text("jdk: 17\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "settings")
    maven = FakeMaven()
    result = run(repo, tmp_path, store, maven=maven, jdks=make_catalog(tmp_path, [home]), environ={"PATH": "/usr/bin"})
    env = maven.calls[0][2]
    assert (env["PATH"], env["JAVA_HOME"]) == (f"{home / 'bin'}:/usr/bin", str(home))
    assert result.report["jdk"] == {"version": "17.0.16", "home": str(home), "source": "global config"}


def test_a_dry_run_without_an_assessment_lists_findings_but_proposes_nothing(repo, tmp_path, store):
    report = run(repo, tmp_path, store).report
    assert report["proposals_allowed"] is False and "giml assess" in report["tier"]["note"]
    lib = entry(report, "o:lib")
    assert lib["candidates"] == [] and lib["reason"].startswith("CVE-2026-1 open, fixed in 2.17.3 (report only")
    assert entry(report, "o:other")["reason"] == "no CVE; 2.0 is the newest release (report only: no usable tier, so nothing is proposed)"


def test_missing_central_snapshot_means_no_metadata_and_a_sync_command(repo, tmp_path):
    with SqliteStateStore(tmp_path / "state" / "state.db") as store:
        store.record_snapshot(snapshot("osv"))
        assess_result(store, repo)
        report = run(repo, tmp_path, store).report
    assert report["snapshots"] == {"osv": "osv-1", "central": None}
    assert entry(report, "o:lib")["status"] == "no_metadata"
    assert report["missing_metadata"] == ["o:lib"] and report["sync_command"] == "giml sync --central --coordinate o:lib"


def test_missing_osv_snapshot_is_an_error_naming_sync(repo, tmp_path):
    with SqliteStateStore(tmp_path / "state" / "state.db") as store:
        with pytest.raises(MissingSnapshotError, match=r"no OSV snapshot; run `giml sync`"):
            run(repo, tmp_path, store)
        assert [(r.kind, r.stop_reason) for r in store.list_runs()] == [("plan", "setup_failed")]


def test_stale_snapshots_produce_warnings(repo, tmp_path):
    with SqliteStateStore(tmp_path / "state" / "state.db") as store:
        store.record_snapshot(snapshot("osv", age_days=9))
        store.record_snapshot(snapshot("central", age_days=7))
        assess_result(store, repo)
        report = run(repo, tmp_path, store).report
    assert report["warnings"] == ["osv snapshot osv-1 is 9 days old (limit 7); run `giml sync`"]


def test_scope_general_and_the_options_change_the_candidates(repo, tmp_path, store):
    assess_result(store, repo)
    options = PlanningSettings(7, 60, 120, "conservative", "general", "allowed", 7, "disallowed")
    report = run(repo, tmp_path, store, options).report
    other = entry(report, "o:other")
    assert (other["status"], [(c["kinds"], c["version"]) for c in other["candidates"]]) == (
        "update_available", [(["next_minor"], "1.1")])  # fmt: skip
    assert entry(report, "o:lib")["candidates"][-1]["blocked"] is None
    assert entry(report, "o:deep")["status"] == "no_metadata" and report["missing_metadata"] == ["o:deep"]


def test_a_failed_resolution_is_reported_and_leaves_nothing_behind(repo, tmp_path, store):
    with pytest.raises(ResolutionError, match="dependency resolution failed"):
        run(repo, tmp_path, store, maven=FakeMaven(succeed=False))
    (run_record,) = [r for r in store.list_runs() if r.kind == "plan"]
    assert run_record.stop_reason == "resolution_failed" and not run_record.worktree_path.exists()
    assert git(repo, "worktree", "list").count("\n") == 0


def test_a_full_disk_stops_the_dry_run_early(repo, tmp_path, store, monkeypatch):
    def full(path):
        raise InsufficientSpace(f"{path}: only 10 MB free on this filesystem, need 512 MB")

    monkeypatch.setattr("giml.maven.isolation.check_space", full)
    with pytest.raises(InsufficientSpace, match="only 10 MB free"):
        run(repo, tmp_path, store)
    assert [(r.kind, r.stop_reason) for r in store.list_runs()] == [("plan", "setup_failed")]
    assert git(repo, "worktree", "list").count("\n") == 0


def test_a_dirty_checkout_is_refused(repo, tmp_path, store):
    (repo / "core" / "pom.xml").write_text(CORE_POM + "<!-- edit -->\n")
    with pytest.raises(PreflightRefusal):
        run(repo, tmp_path, store)


def test_clean_after_a_dry_run_has_nothing_to_delete(repo, tmp_path, store):
    run(repo, tmp_path, store)
    actions = workspace.clean(tmp_path / "state", store, lambda: NOW, repo, False, True)
    assert not any("branch" in a for a in actions)
    assert git(repo, "branch", "--list").strip().startswith("* ")


def test_results_are_identical_for_identical_inputs(repo, tmp_path, store):
    assess_result(store, repo)
    first = run(repo, tmp_path, store).report
    second = run(repo, tmp_path, store).report
    assert {k: v for k, v in first.items() if k != "run_id"} == {k: v for k, v in second.items() if k != "run_id"}


# tier_status --------------------------------------------------------------------------------------------------

def record(earned="B", version=3, expires=None, base="abc", declared=None):
    return GateResultRecord("r", "p", base, version, earned, json.dumps({"declared_tier": declared}), NOW - datetime.timedelta(days=3),
                            expires or NOW + datetime.timedelta(days=27))  # fmt: skip


def test_tier_status_valid():
    status = tier_status(record("A", declared="A"), CONFIG, NOW, "abc")
    assert (status.earned, status.declared, status.usable, status.note) == ("A", "A", True, "")
    assert status.measured_at == (NOW - datetime.timedelta(days=3)).isoformat()


@pytest.mark.parametrize(("kwargs", "note"), [
    ({"earned": None}, "the last assessment earned no tier"),
    ({"version": 2}, "assessed under gate config version 2, the current one is 3; run `giml assess`"),
    ({"expires": NOW - datetime.timedelta(seconds=1)}, "the assessment expired on 2026-09-25"),
])  # fmt: skip
def test_tier_status_unusable(kwargs, note):
    status = tier_status(record(**kwargs), CONFIG, NOW, "abc")
    assert status.usable is False and note in status.note
    assert status.earned == kwargs.get("earned", "B")


def test_tier_status_without_a_record():
    status = tier_status(None, CONFIG, NOW, "abc")
    assert (status.earned, status.usable) == (None, False) and status.note == "no assessment; run `giml assess <path>`"


def test_tier_status_for_another_commit_is_usable_with_a_note():
    status = tier_status(record("B", base="1111111aaaa"), CONFIG, NOW, "2222222bbbb")
    assert status.usable is True and status.note == "assessed at 1111111, the base commit is 2222222"


def test_an_unknown_earned_tier_is_not_usable():
    assert tier_status(record("Z"), CONFIG, NOW, "abc").usable is False


def test_newest_release():
    releases = [VersionRelease(v, OLD) for v in ("1.0", "1.10", "1.9", "2.0-rc1")]
    assert newest_release(releases) == "1.10"
    assert newest_release([VersionRelease("2.0-rc1", OLD), VersionRelease("2.0-rc2", OLD)]) == "2.0-rc2"
    assert newest_release([]) is None and newest_release(None) is None
