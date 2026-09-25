import stat
import subprocess
from pathlib import Path

import pytest

from giml.maven.build import StageOutcome
from giml.maven.cache import (
    BuildEnvironment, CacheMetrics, CachingBuildRunner, maven_version, stage_key_parts, tooling_fingerprint,
)  # fmt: skip
from giml.maven.failures import COMPILE, ENFORCER_CONVERGENCE, INFRASTRUCTURE, TIMEOUT, UNKNOWN, Failure
from giml.store.result_cache import FileResultCache
from tests.git.repo_helpers import git, make_repo

ENV = BuildEnvironment(jdk="21.0.9", maven="3.9.11", tooling="tool-1", config_version=3)


@pytest.fixture
def repo(tmp_path):
    repo = make_repo(tmp_path / "repo")
    (repo / "pom.xml").write_text("<project><version>1</version></project>\n")
    (repo / "src").mkdir()
    (repo / "src" / "App.java").write_text("class App {}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo


def key(repo, stage="unit_test", env=ENV, dependencies=None):
    return FileResultCache(repo / ".cache").key(stage_key_parts(repo, stage, env, dependencies))


def test_key_parts_name_everything_that_decides_a_result(repo):
    parts = stage_key_parts(repo, "unit_test", ENV)
    assert set(parts) == {"stage", "head_tree", "changes", "dependencies", "jdk", "maven", "tooling", "config_version"}
    assert (parts["stage"], parts["jdk"], parts["maven"], parts["tooling"], parts["config_version"], parts["dependencies"]) == (
        "unit_test", "21.0.9", "3.9.11", "tool-1", 3, None)  # fmt: skip
    assert parts["head_tree"] == git(repo, "rev-parse", "HEAD^{tree}") and len(parts["changes"]) == 64


def test_the_same_content_at_another_path_has_the_same_key(repo, tmp_path):
    other = tmp_path / "somewhere" / "else"
    subprocess.run(["git", "clone", "-q", str(repo), str(other)], check=True)
    assert key(repo) == key(other)


def test_a_pom_edit_changes_the_key_and_reverting_restores_it(repo):
    before = key(repo)
    (repo / "pom.xml").write_text("<project><version>2</version></project>\n")
    edited = key(repo)
    assert edited != before
    (repo / "pom.xml").write_text("<project><version>1</version></project>\n")
    assert key(repo) == before


def test_the_same_edit_gives_the_same_key_whichever_worktree_holds_it(repo, tmp_path):
    other = tmp_path / "second"
    subprocess.run(["git", "clone", "-q", str(repo), str(other)], check=True)
    for place in (repo, other):
        (place / "pom.xml").write_text("<project><version>2</version></project>\n")
    assert key(repo) == key(other)


def test_untracked_build_output_does_not_change_the_key(repo):
    before = key(repo)
    (repo / "target" / "classes").mkdir(parents=True)
    (repo / "target" / "classes" / "App.class").write_bytes(b"\xca\xfe")
    assert key(repo) == before


def test_committing_the_edit_changes_the_head_tree_so_the_key_differs(repo):
    (repo / "pom.xml").write_text("<project><version>2</version></project>\n")
    uncommitted = key(repo)
    git(repo, "commit", "-qam", "edit")
    assert key(repo) != uncommitted


@pytest.mark.parametrize("change", [
    {"stage": "compile"},
    {"env": BuildEnvironment("17.0.16", "3.9.11", "tool-1", 3)},
    {"env": BuildEnvironment("21.0.9", "3.9.9", "tool-1", 3)},
    {"env": BuildEnvironment("21.0.9", "3.9.11", "tool-2", 3)},
    {"env": BuildEnvironment("21.0.9", "3.9.11", "tool-1", 4)},
    {"dependencies": "abc123"},
])  # fmt: skip
def test_every_input_changes_the_key(repo, change):
    assert key(repo, **change) != key(repo)


def test_the_tooling_fingerprint_follows_the_pinned_versions():
    assert tooling_fingerprint() == tooling_fingerprint() and len(tooling_fingerprint()) == 64


def test_maven_version_reads_the_first_line_of_mvn_version(tmp_path, monkeypatch):
    script = tmp_path / "bin" / "mvn"
    script.parent.mkdir()
    script.write_text('#!/bin/sh\necho "Apache Maven 3.9.11 (3e54c93a704957b63ee3494413a2b544fd3d825b)"\necho "Java version: 21"\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    assert maven_version({"PATH": str(script.parent)}) == "3.9.11"
    assert maven_version({"PATH": str(tmp_path / "empty")}) == "unknown"
    script.write_text("#!/bin/sh\nexit 3\n")
    assert maven_version({"PATH": str(script.parent)}) == "unknown"


# CachingBuildRunner ------------------------------------------------------------------------------------------


class Inner:
    """A BuildRunner that returns queued outcomes and counts its calls."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def run_stage(self, worktree, stage, timeout_seconds):
        self.calls.append((worktree, stage))
        return self.outcomes.pop(0)


def passed(stage="unit_test", seconds=12.0):
    return StageOutcome(stage, True, seconds, Path("/orig/01.log"), None)


def failed(failure_class=COMPILE, stage="compile", seconds=4.0):
    return StageOutcome(stage, False, seconds, Path("/orig/02.log"), Failure(failure_class, "abc123abc123abc1", ("a", "b")))


def caching(tmp_path, repo, *outcomes, env=ENV):
    inner = Inner(*outcomes)
    runner = CachingBuildRunner(inner, FileResultCache(tmp_path / "cache"), lambda worktree, stage: stage_key_parts(worktree, stage, env),
                                tmp_path / "logs")  # fmt: skip
    return runner, inner


def test_a_repeated_stage_is_answered_from_the_cache_with_its_original_timing(tmp_path, repo):
    runner, inner = caching(tmp_path, repo, passed())
    first = runner.run_stage(repo, "unit_test", 60)
    second = runner.run_stage(repo, "unit_test", 60)
    assert len(inner.calls) == 1
    assert (first.cache_hit, second.cache_hit) == (False, True)
    assert (second.passed, second.duration_seconds, second.failure) == (True, 12.0, None)
    assert second.log_path.name == "cache-hit-01-unit_test.log"
    assert second.log_path.read_text() == f"cache hit: outcome of {Path('/orig/01.log')}, 12.0 s originally\n"


def test_outcomes_carry_the_cache_key_on_a_miss_and_on_a_hit(tmp_path, repo):
    runner, _ = caching(tmp_path, repo, passed())
    expected = runner.cache.key(stage_key_parts(repo, "unit_test", ENV))
    assert runner.run_stage(repo, "unit_test", 60).cache_key == expected
    assert runner.run_stage(repo, "unit_test", 60).cache_key == expected


def test_failures_are_cached_with_their_class_and_signature(tmp_path, repo):
    runner, inner = caching(tmp_path, repo, failed())
    runner.run_stage(repo, "compile", 60)
    hit = runner.run_stage(repo, "compile", 60)
    assert len(inner.calls) == 1 and hit.cache_hit
    assert (hit.passed, hit.failure) == (False, Failure(COMPILE, "abc123abc123abc1", ("a", "b")))
    assert "key lines:\n  a\n  b\n" in hit.log_path.read_text()


@pytest.mark.parametrize("failure_class", [INFRASTRUCTURE, TIMEOUT])
def test_outcomes_that_say_nothing_about_the_candidate_are_never_cached(tmp_path, repo, failure_class):
    runner, inner = caching(tmp_path, repo, failed(failure_class), passed("compile"))
    assert runner.run_stage(repo, "compile", 60).failure_class == failure_class
    assert runner.run_stage(repo, "compile", 60).passed
    assert len(inner.calls) == 2  # the retry really ran


def test_an_unknown_failure_is_cached_because_it_is_deterministic_enough(tmp_path, repo):
    runner, inner = caching(tmp_path, repo, failed(UNKNOWN))
    runner.run_stage(repo, "compile", 60)
    assert runner.run_stage(repo, "compile", 60).cache_hit and len(inner.calls) == 1


def test_a_different_stage_or_content_is_a_miss(tmp_path, repo):
    runner, inner = caching(tmp_path, repo, passed("compile"), passed("unit_test"), passed("unit_test"))
    runner.run_stage(repo, "compile", 60)
    runner.run_stage(repo, "unit_test", 60)
    (repo / "pom.xml").write_text("<project><version>2</version></project>\n")
    assert not runner.run_stage(repo, "unit_test", 60).cache_hit
    assert len(inner.calls) == 3


def test_details_such_as_enforcer_violations_survive_the_cache(tmp_path, repo):
    outcome = StageOutcome("enforcer", False, 3.0, Path("/orig/03.log"), Failure(ENFORCER_CONVERGENCE, "sig0sig0sig0sig0", ("x",)),
                           details={"violations": [{"rule": "DependencyConvergence", "subject": "g:a", "detail": {"versions": ["1", "2"]}}]})  # fmt: skip
    runner, inner = caching(tmp_path, repo, outcome)
    runner.run_stage(repo, "enforcer", 60)
    hit = runner.run_stage(repo, "enforcer", 60)
    assert hit.cache_hit and len(inner.calls) == 1
    assert hit.details == outcome.details and [v.subject for v in hit.violations] == ["g:a"]


def test_a_result_is_shared_between_worktrees_with_the_same_content(tmp_path, repo):
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(repo), str(other)], check=True)
    runner, inner = caching(tmp_path, repo, passed())
    runner.run_stage(repo, "unit_test", 60)
    assert runner.run_stage(other, "unit_test", 60).cache_hit and len(inner.calls) == 1


def test_an_entry_of_another_schema_is_ignored(tmp_path, repo):
    runner, inner = caching(tmp_path, repo, passed(), passed())
    cache_key = runner.cache.key(stage_key_parts(repo, "unit_test", ENV))
    runner.cache.put(cache_key, {"schema": 999, "whatever": True})
    assert not runner.run_stage(repo, "unit_test", 60).cache_hit
    assert runner.run_stage(repo, "unit_test", 60).cache_hit and len(inner.calls) == 1


def test_metrics_count_hits_misses_and_time_per_stage(tmp_path, repo):
    runner, _ = caching(tmp_path, repo, passed("unit_test", 12.0), failed(seconds=4.0))
    for stage in ("unit_test", "unit_test", "unit_test", "compile", "compile"):
        runner.run_stage(repo, stage, 60)
    metrics = runner.metrics()
    assert metrics.stages["unit_test"] == {"hits": 2, "misses": 1, "seconds_saved": 24.0, "seconds_spent": 12.0}
    assert metrics.stages["compile"] == {"hits": 1, "misses": 1, "seconds_saved": 4.0, "seconds_spent": 4.0}
    assert (metrics.hits, metrics.misses, metrics.seconds_saved) == (3, 2, 28.0)
    assert metrics.hit_rate == 0.6
    assert metrics.as_dict() == {"hits": 3, "misses": 2, "hit_rate": 0.6, "seconds_saved": 28.0, "seconds_spent": 16.0,
                                 "stages": metrics.stages}  # fmt: skip


def test_metrics_of_a_run_that_did_nothing():
    assert CacheMetrics().hit_rate == 0.0 and CacheMetrics().as_dict()["hits"] == 0
