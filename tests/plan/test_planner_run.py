"""run_planning end to end with fakes for Maven, the build stages and the advisory data (fast); real Maven is in the slow tests."""

import json
from pathlib import Path

import pytest

from giml import workspace
from giml.core.config import default_gate_config_path, load_gate_config
from giml.maven.build import StageOutcome
from giml.maven.cache import CacheMetrics
from giml.maven.jdk import Jdk
from giml.plan.baseline import verify_baseline
from giml.plan.baseline_run import BaselineRun
from giml.plan.outcome_log import log_baseline
from giml.plan.planner import run_planning
from giml.store.sqlite_store import SqliteStateStore
from tests.git.repo_helpers import git
from tests.plan.test_analysis import NOW, SOURCES, FakeMaven, assess_result, repo, snapshot  # noqa: F401

CONFIG = load_gate_config(default_gate_config_path())


class PassingStages:
    """A BuildRunner on which every stage passes (the enforcer with no violations)."""

    def __init__(self):
        self.calls = []
        self._metrics = CacheMetrics()

    def metrics(self):
        return self._metrics

    def run_stage(self, worktree, stage, timeout_seconds):
        self.calls.append(stage)
        self._metrics.record(stage, False, 1.0)
        return StageOutcome(stage, True, 1.0, Path("/logs/x.log"), None, details={"violations": []} if stage == "enforcer" else None)


class NoJava:
    def __call__(self, args, timeout, env=None):
        import subprocess

        return subprocess.CompletedProcess(args, 1, "", "no japicmp here")  # japicmp is not available: nothing is skipped


def plan(tmp_path, repo, options=None, tier=True, **kw):  # noqa: F811
    state = tmp_path / "state"
    store = SqliteStateStore(state / "state.db")  # closed with the test's temp directory; the tests read it afterwards
    store.record_snapshot(snapshot("osv"))
    store.record_snapshot(snapshot("central"))
    if tier:
        assess_result(store, repo)
    results = []

    def verify(ws, temp):
        stages = PassingStages()
        baseline = verify_baseline(stages, ws.worktree, 60)
        log_baseline(store, ws.run_id, ws.repo.project_key, baseline, tier="A", oracle=None, rewind=None, jdk="17.0.1")
        baseline_run = BaselineRun(baseline, {}, state / "baseline.json", None, stages, Jdk(None, "17.0.1", "inherited"), {})
        results.append((run_planning(ws, baseline_run, state, store, CONFIG, options or CONFIG.planning, FakeMaven(), SOURCES,
                                     lambda: NOW, NoJava(), **kw), stages, ws))
        return results[0][0].stop_reason

    workspace.set_up(repo, state, store, lambda: NOW, verify=verify)
    return (*results[0], store, state)


def test_a_usable_tier_plans_commits_the_fix_and_writes_the_report_files(tmp_path, repo):  # noqa: F811
    run, stages, ws, store, state = plan(tmp_path, repo)
    result = run.report["result"]
    assert run.stop_reason == "planned" and [c["kind"] for c in result["committed"]] == ["cve_patch"]
    assert result["committed"][0]["label"] == "o:lib 2.17.1 → 2.17.3 (cve_patch)" and result["enforcer_clean"] and result["left"] == []
    assert "<lib.version>2.17.3</lib.version>" in (ws.worktree / "core" / "pom.xml").read_text()
    assert git(ws.worktree, "log", "-1", "--format=%s") == "o:lib 2.17.1 → 2.17.3 (cve_patch)"
    document = json.loads(run.json_path.read_text())
    assert document["kind"] == "plan" and document["result"]["branch"] == ws.branch and document["result"]["builds"] == 2  # the ladder trial and the strict check (this fake runner has no cache)
    assert run.markdown_path.read_text().startswith("# giml plan: proj") and "## Analysis at the base commit" in run.markdown_path.read_text()
    assert run.report["result"]["review"] == f"git diff {ws.repo.base_sha[:7]}..{ws.branch}"


def test_the_trials_are_logged_and_a_stage_chain_ran_for_the_final_state(tmp_path, repo):  # noqa: F811
    run, stages, ws, store, state = plan(tmp_path, repo)
    states = [s.id for s in store.list_states(ws.run_id)]
    assert states[0].endswith(":baseline") and any(s.endswith("trial-001") for s in states)
    assert {"compile", "enforcer", "unit_test"} <= set(stages.calls)


def test_without_a_usable_tier_nothing_is_proposed_and_the_reason_is_reported(tmp_path, repo):  # noqa: F811
    run, stages, ws, store, state = plan(tmp_path, repo, tier=False)
    assert run.stop_reason == "no_usable_tier" and run.outcome is None and not run.succeeded
    assert run.report["result"]["committed"] == [] and run.report["result"]["stop_reason"] == "no_usable_tier"
    assert git(ws.worktree, "log", "-1", "--format=%s") == "fixture"  # nothing was committed on the result branch


def test_a_compare_naive_run_adds_both_sets_of_figures(tmp_path, repo):  # noqa: F811
    run, *_ = plan(tmp_path, repo, compare_naive_bumps=True)
    comparison = run.report["result"]["naive_comparison"]
    assert comparison["naive"]["touched"] >= 1 and comparison["giml"]["touched"] == 1 and "builds" in comparison["giml"]


def test_a_successful_plan_needs_a_committed_step_and_a_clean_enforcer(tmp_path, repo):  # noqa: F811
    run, *_ = plan(tmp_path, repo)
    assert run.succeeded and run.enforcer_clean and run.remaining_violations == ()


def test_the_summary_and_markdown_render_exactly_what_was_planned(tmp_path, repo):  # noqa: F811
    from giml.plan.planner import render_plan_markdown, render_plan_summary

    run, stages, ws, store, state = plan(tmp_path, repo, compare_naive_bumps=True)
    summary = render_plan_summary(run.report)
    lines = summary.splitlines()
    sha = run.report["result"]["committed"][0]["sha"]
    assert lines[0] == f"plan: proj at {ws.repo.base_sha[:7]}: 1 step(s) committed, 0 left, 2 build(s), stop reason complete"
    assert lines[1].startswith("exposure: worst HIGH, 1 at that severity, 1 in all -> ")
    assert f"  + o:lib 2.17.1 → 2.17.3 (cve_patch) ({sha[:7]})" in lines
    assert "enforcer: clean" in lines and any(l.startswith("naive (each dependency to its newest, on its own): ") for l in lines)
    assert any(l.startswith("giml: 1 touched, 2 build(s), ") for l in lines)
    markdown = render_plan_markdown(run.report)
    assert markdown.startswith(f"# giml plan: proj\n\nBase commit `{ws.repo.base_sha}`. Run `{ws.run_id}`. Branch `{ws.branch}`.\n")
    assert f"## o:lib 2.17.1 → 2.17.3 (cve_patch)\nCommit `{sha}`. Clears GHSA-lib." in markdown
    assert "- set property lib.version 2.17.1 → 2.17.3 (core/pom.xml:" in markdown
    assert f"## Review\n`git diff {ws.repo.base_sha[:7]}..{ws.branch}`\n\n## Clean up\n`giml clean <project>`" in markdown


def test_the_report_only_summary_says_why_nothing_was_proposed(tmp_path, repo):  # noqa: F811
    from giml.plan.planner import render_plan_summary

    run, *_ = plan(tmp_path, repo, tier=False)
    lines = render_plan_summary(run.report).splitlines()
    assert lines[0].endswith("0 step(s) committed, 0 left, 0 build(s), stop reason no_usable_tier")
    assert lines[-1] == "report only: no assessment; run `giml assess <path>`" and "enforcer: clean" in lines
    assert not any(l.startswith("exposure:") for l in lines)  # no plan ran, so no before/after


def test_a_stale_snapshot_is_warned_about_in_the_report_and_the_cache_figures_are_recorded(tmp_path, repo):  # noqa: F811
    state = tmp_path / "state"
    store = SqliteStateStore(state / "state.db")
    store.record_snapshot(snapshot("osv", age_days=30))
    store.record_snapshot(snapshot("central", age_days=1))
    assess_result(store, repo)
    holder = []

    def verify(ws, temp):
        stages = PassingStages()
        baseline = verify_baseline(stages, ws.worktree, 60)
        log_baseline(store, ws.run_id, ws.repo.project_key, baseline, tier="A", oracle=None, rewind=None, jdk="17")
        baseline_run = BaselineRun(baseline, {}, state / "b.json", None, stages, Jdk(None, "17", "inherited"), {})
        holder.append(run_planning(ws, baseline_run, state, store, CONFIG, CONFIG.planning, FakeMaven(), SOURCES, lambda: NOW, NoJava()))
        return holder[0].stop_reason

    workspace.set_up(repo, state, store, lambda: NOW, verify=verify)
    report = holder[0].report
    assert report["warnings"] == ["osv snapshot osv-1 is 30 days old (limit 7); run `giml sync`"]
    cache = report["result"]["cache"]
    assert cache["hits"] == 0 and cache["misses"] >= 6 and cache["seconds_spent"] >= 6.0  # the baseline's 3 stages and the trials' builds
    assert "cache (baseline and trials): 0 hit(s)," in __import__("giml.plan.planner", fromlist=["x"]).render_plan_summary(report)


class WritingMaven:
    """A real MavenResult-returning callable: every call writes its own call number into the log path it is given
    (the real MavenBuildRunner, not a fake, decides that path), so a silent overwrite would leave a mismatch."""

    def __init__(self):
        self.calls = 0

    def __call__(self, project, args, log, timeout, env):
        from giml.maven.runner import MavenResult

        self.calls += 1
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"[INFO] BUILD SUCCESS (call {self.calls}, goal {args[0]})\n")
        return MavenResult(tuple(args), 0, 0.1, log, False)


def test_every_attempts_log_is_its_own_file_none_overwritten_across_the_run(tmp_path, repo):  # noqa: F811
    """The real MavenBuildRunner is reused across the baseline and every trial (one growing counter, spec 9.1);
    each numbered log still holds exactly its own attempt's content after the whole plan has run."""
    from giml.maven.build import MavenBuildRunner
    from giml.maven.cache import CachingBuildRunner
    from giml.store.result_cache import FileResultCache

    state = tmp_path / "state"
    store = SqliteStateStore(state / "state.db")
    store.record_snapshot(snapshot("osv"))
    store.record_snapshot(snapshot("central"))
    assess_result(store, repo)
    maven = WritingMaven()
    holder = []

    def verify(ws, temp):
        logs_dir = state / "runs" / ws.run_id / "logs"
        runner = CachingBuildRunner(MavenBuildRunner(maven, {}, logs_dir), FileResultCache(state / "cache"),
                                    lambda worktree, stage: {"worktree": str(worktree), "stage": stage}, logs_dir)  # fmt: skip
        baseline = verify_baseline(runner, ws.worktree, 60)
        log_baseline(store, ws.run_id, ws.repo.project_key, baseline, tier="A", oracle=None, rewind=None, jdk="17")
        baseline_run = BaselineRun(baseline, {}, state / "b.json", None, runner, Jdk(None, "17", "inherited"), {})
        holder.append(run_planning(ws, baseline_run, state, store, CONFIG, CONFIG.planning, FakeMaven(), SOURCES, lambda: NOW, NoJava(),
                                   compare_naive_bumps=True))  # fmt: skip
        return holder[0].stop_reason

    ws = workspace.set_up(repo, state, store, lambda: NOW, verify=verify)
    logs_dir = state / "runs" / ws.run_id / "logs"
    stage_suffixes = ("compile", "unit_test", "enforcer", "integration", "startup", "pit", "package")
    numbered = sorted(p for p in logs_dir.glob("*.log")
                      if p.stem.split("-", 1)[0].isdigit() and p.stem.rsplit("-", 1)[-1] in stage_suffixes)  # fmt: skip
    assert len(numbered) == maven.calls >= 4  # baseline's 3 stages plus at least one trial build; one file per call
    for path in numbered:
        expected_call = int(path.stem.split("-", 1)[0])
        assert f"call {expected_call}," in path.read_text()  # still this attempt's own content, not a later one's
    for attempt in store.list_attempts(ws.run_id):
        if attempt.stage == "exposure":  # a resolved-tree check, no build, no log file (spec 8.5)
            continue
        assert Path(attempt.log_path).is_file() and Path(attempt.log_path).parent == logs_dir
