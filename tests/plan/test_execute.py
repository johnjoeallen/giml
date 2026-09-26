import json
from pathlib import Path

import pytest

from giml.maven.declarations import read_declarations
from giml.maven.failures import COMPILE, Failure
from giml.maven.build import StageOutcome
from giml.plan.execute import deferral_records, execute, verdict_of
from giml.plan.trial import TrialResult
from tests.git.repo_helpers import git, make_repo
from tests.plan.test_steps import NOW, POM, analysis, dep, options  # noqa: F401  (the synthetic analysis helpers)
from tests.plan.test_steps import world as _world  # noqa: F401


def clock():
    return NOW


@pytest.fixture
def repo(tmp_path):
    root = make_repo(tmp_path / "repo", {"pom.xml": POM})
    return root


@pytest.fixture
def world(repo):
    from giml.core.model import Coordinate
    from giml.maven.tree import ModuleTree

    pom = repo / "pom.xml"
    tree = ModuleTree(Coordinate.parse("g:app"), "1", (
        dep("com.fasterxml.jackson.core:jackson-core", "2.18.2"), dep("com.fasterxml.jackson.core:jackson-databind", "2.18.2"),
        dep("o:lib", "1.0.0"), dep("o:clean", "1.0.0"), dep("o:deep", "3.0", via=["o:lib"]), dep("o:major", "2.0", via=["o:lib"])))  # fmt: skip
    return pom, read_declarations([pom]), tree


def result(passed=True, inconclusive=False, cached=False):
    failure = None if passed else Failure(COMPILE, "abc", ("cannot find symbol X",))
    outcome = StageOutcome("unit_test", passed, 1.0, Path("log"), failure, cache_hit=cached)
    return TrialResult(passed, inconclusive, None if passed else "compile", failure, (outcome,), (), ())


class FakeTrial:
    """Passes unless the pom text a trial would produce contains a poisoned version."""

    def __init__(self, poison=()):
        self.poison, self.calls, self.reference = poison, [], ()

    def refresh_reference(self):
        self.refreshed = getattr(self, "refreshed", 0) + 1

    def verify(self, changes, pit=True):
        self.calls.append(changes)
        versions = {getattr(c, "version", None) for c in changes}
        return result(not (versions & set(self.poison)))


def run(repo, world, trial, opts=None, reanalyse=None):
    first = analysis(world, opts)
    return execute(repo, "run-1", "B", first, reanalyse or (lambda name, units: first), trial, opts or options(), clock, repo / "pom.xml")


def test_accepted_steps_are_committed_one_each_in_ladder_order(repo, world):
    outcome = run(repo, world, FakeTrial())
    assert [c.key for c in outcome.committed] == [
        "dep:o:lib@1.0.0", "dep:com.fasterxml.jackson.core:jackson-core+com.fasterxml.jackson.core:jackson-databind@2.18.2",
        "dep:o:deep@3.0"]  # fmt: skip
    assert outcome.stop_reason == "complete" and [entry.key for entry in outcome.left] == ["dep:o:major@2.0"]  # the fix is a major
    subjects = git(repo, "log", "--format=%s", "-3").splitlines()
    assert subjects == [c.label for c in reversed(outcome.committed)]
    text = (repo / "pom.xml").read_text()
    assert "<version>1.0.2</version>" in text and "<jackson.version>2.18.9</jackson.version>" in text
    assert "pin o:deep at 3.1" in outcome.committed[2].edits[0]


def test_each_commit_holds_only_the_steps_up_to_it(repo, world):
    outcome = run(repo, world, FakeTrial())
    first = git(repo, "show", f"{outcome.committed[0].sha}:pom.xml")
    assert "<version>1.0.2</version>" in first and "<jackson.version>2.18.2</jackson.version>" in first


def test_a_failing_first_step_advances_the_ladder(repo, world):
    outcome = run(repo, world, FakeTrial(poison={"1.0.2"}))
    lib = next(c for c in outcome.committed if "o:lib" in c.label)
    assert lib.kind == "cve_minor" and "<version>1.1.0</version>" in (repo / "pom.xml").read_text()


def test_a_dependency_that_never_passes_is_deferred_with_its_reason(repo, world):
    outcome = run(repo, world, FakeTrial(poison={"1.0.2", "1.1.0"}))
    lib = next(entry for entry in outcome.left if entry.key == "dep:o:lib@1.0.0")
    assert lib.reason.startswith("no fixing version passed") and "compile" in lib.reason
    assert "<version>1.0.0</version>" in (repo / "pom.xml").read_text()


def test_nothing_is_committed_when_nothing_passes(repo, world):
    before = git(repo, "rev-parse", "HEAD")
    outcome = run(repo, world, FakeTrial(poison={"1.0.2", "1.1.0", "2.18.9", "2.19.0", "3.1", "3.2"}))
    assert outcome.committed == () and git(repo, "rev-parse", "HEAD") == before and git(repo, "status", "--porcelain") == ""


def test_the_tree_is_analysed_again_after_commits_for_the_final_exposure(repo, world):
    seen = []

    def reanalyse(name, units):
        seen.append((name, units))
        return analysis(world)

    run(repo, world, FakeTrial(), reanalyse=reanalyse)
    assert seen == [("03-tree.log", False)]


def test_the_build_budget_stops_the_run_and_defers_the_rest(repo, world):
    from dataclasses import replace

    outcome = run(repo, world, FakeTrial(), opts=replace(options(), max_builds=1))
    assert outcome.stop_reason == "budget_builds" and outcome.builds == 1
    assert any(entry.reason.startswith("not tried: the build budget") for entry in outcome.left)


def test_an_inconclusive_trial_stops_without_committing_a_guess(repo, world):
    class Broken(FakeTrial):
        def verify(self, changes, pit=True):
            return result(False, inconclusive=True)

    outcome = run(repo, world, Broken())
    assert outcome.stop_reason == "inconclusive" and outcome.committed == ()


def test_verdicts_name_the_failing_stage_and_cost_nothing_when_cached():
    failed = verdict_of(result(False))
    assert (failed.passed, failed.reason, failed.cost) == (False, f"compile {COMPILE}: cannot find symbol X", 1)
    assert verdict_of(result(cached=True)).cost == 0 and verdict_of(result()).reason == "passed"


def test_deferral_records_carry_the_held_version_and_triggers(repo, world):
    outcome = run(repo, world, FakeTrial(poison={"1.0.2", "1.1.0"}))
    records = deferral_records(outcome, "proj", "run-1", NOW)
    lib = next(r for r in records if r.coordinate == "o:lib")
    assert (lib.id, lib.held_at_version, lib.project_id) == ("run-1:o:lib", "1.0.0", "proj")
    assert json.loads(lib.trigger_json) == {"on": ["new_release", "new_advisory", "pom_change"], "coordinates": ["o:lib"]}
    assert all(r.created_at == NOW and r.resolved_at is None for r in records)


def test_only_the_edited_poms_are_committed_never_build_output(repo, world):
    (repo / "target").mkdir()
    (repo / "target" / "giml-tree.json").write_text("{}")
    run(repo, world, FakeTrial())
    assert "target" not in git(repo, "ls-files")
    assert git(repo, "status", "--porcelain") == "?? target/"


def test_the_naive_baseline_is_every_first_step_verified_together(repo, world):
    outcome = run(repo, world, FakeTrial(poison={"1.0.2"}))
    assert outcome.naive is not None and outcome.naive.passed is False
    assert "o:lib 1.0.0 → 1.0.2 (cve_patch)" in outcome.naive.steps and outcome.naive.reason.startswith("compile")


def test_the_naive_baseline_passes_when_nothing_conflicts(repo, world):
    assert run(repo, world, FakeTrial()).naive.passed is True


def test_the_reference_is_refreshed_after_each_phase_that_committed(repo, world):
    trial = FakeTrial()
    run(repo, world, trial)
    assert trial.refreshed == 1


def test_a_passing_trial_that_leaves_a_required_violation_is_a_failure():
    from giml.maven.enforcer import Violation

    gone = Violation("DependencyConvergence", "o:lib", {"versions": ["1", "2"]})
    passing = result()
    assert verdict_of(passing, frozenset({gone.identity})).reason == f"did not resolve {gone.identity}"
    resolved = TrialResult(True, False, None, None, passing.outcomes, (), (gone,))
    assert verdict_of(resolved, frozenset({gone.identity})).passed


def test_a_baseline_convergence_conflict_is_aligned_and_committed_when_it_resolves(repo, world):
    from giml.maven.enforcer import Violation

    conflict = Violation("DependencyConvergence", "o:clean", {"versions": ["1.0.0", "1.0.1"]})

    class Resolving(FakeTrial):
        def verify(self, changes, pit=True):
            base = super().verify(changes, pit)
            return TrialResult(True, False, None, None, base.outcomes, (), (conflict,)) if changes else base

    trial = Resolving()
    trial.reference = (conflict,)
    outcome = run(repo, world, trial)
    assert any(c.kind == "enforcer_pin" and "o:clean" in c.label for c in outcome.committed)
    assert "<version>1.0.1</version>" in (repo / "pom.xml").read_text()


def test_a_vulnerability_failure_costs_no_build_and_is_named():
    failure = Failure("vulnerability_worse", "abc", ("exposure rose from none to LOW x1 (1 in all)", "new advisory A"))
    outcome = StageOutcome("exposure", False, 0.4, Path("/dev/null"), failure)
    verdict = verdict_of(TrialResult(False, False, "exposure", failure, (outcome,), (), ()))
    assert (verdict.passed, verdict.cost) == (False, 0)
    assert verdict.reason == "exposure vulnerability_worse: exposure rose from none to LOW x1 (1 in all)"


class PitAware(FakeTrial):
    """Passes without PIT unless poisoned; with PIT, versions in ``pit_poison`` fail."""

    def __init__(self, pit_poison=()):
        super().__init__()
        self.pit_poison, self.flags = set(pit_poison), []

    def verify(self, changes, pit=True):
        self.flags.append(pit)
        versions = {getattr(c, "version", None) for c in changes}
        return result(not (pit and versions & self.pit_poison))


def test_candidates_are_built_without_pit_and_only_what_is_kept_runs_it(repo, world):
    trial = PitAware()
    outcome = run(repo, world, trial)
    assert len(outcome.committed) == 3 and trial.flags.count(True) == 1  # the combination, once
    assert trial.flags[:3] == [False, False, False]


def test_a_step_that_only_pit_rejects_is_backed_off_or_deferred(repo, world):
    outcome = run(repo, world, PitAware(pit_poison={"1.0.2"}))
    lib = next(c for c in outcome.committed if "o:lib" in c.label)
    assert lib.kind == "cve_minor" and "<version>1.1.0</version>" in (repo / "pom.xml").read_text()


def test_every_trial_is_reported_to_the_recorder_with_what_it_applied(repo, world):
    seen = []
    first = analysis(world)
    execute(repo, "run-1", "B", first, lambda name, units: first, FakeTrial(poison={"1.0.2"}), options(), clock, repo / "pom.xml",
            lambda applied, trial_result: seen.append((applied, trial_result.passed)))  # fmt: skip
    lib = [entry for entry, _ in seen if len(entry) == 1 and entry[0]["key"] == "dep:o:lib@1.0.0"]
    assert {(e[0]["from"], e[0]["to"]) for e in lib} == {("1.0.0", "1.0.2"), ("1.0.0", "1.1.0")}
    assert any(passed is False for _, passed in seen) and lib[0][0]["members"] == ["o:lib"]


def test_a_trial_whose_builds_all_came_from_the_cache_costs_nothing_even_after_the_vulnerability_check():
    exposure = StageOutcome("exposure", True, 0.3, Path("/dev/null"), None)
    cached = StageOutcome("compile", True, 1.0, Path("log"), None, cache_hit=True)
    fresh = StageOutcome("unit_test", True, 1.0, Path("log"), None)
    assert verdict_of(TrialResult(True, False, None, None, (exposure, cached), (), ())).cost == 0
    assert verdict_of(TrialResult(True, False, None, None, (exposure, cached, fresh), (), ())).cost == 1
