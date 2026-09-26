from giml.maven.build import StageOutcome
from giml.maven.failures import COMPILE, Failure
from giml.plan.naive import compare_naive, naive_changes
from giml.plan.trial import TrialResult
from pathlib import Path
from tests.plan.test_exposure import finding, tree_exposure
from tests.plan.test_steps import analysis, world  # noqa: F401


def stage(cached=False):
    return StageOutcome("compile", True, 1.0, Path("log"), None, cache_hit=cached)


def passed(exposure=None, cached=False):
    return TrialResult(True, False, None, None, (stage(cached),), (), (), exposure)


def failed():
    failure = Failure(COMPILE, "abc", ("x",))
    return TrialResult(False, False, "compile", failure, (StageOutcome("compile", False, 1.0, Path("log"), failure),), (), ())


def test_every_editable_dependency_with_a_newer_release_is_bumped_to_its_newest_on_its_own(world):
    found = {coordinates: (current, newest) for coordinates, current, newest, _ in naive_changes(analysis(world))}
    assert found[("o:lib",)] == ("1.0.0", "1.1.0") and found[("o:clean",)] == ("1.0.0", "1.0.1")
    jackson = ("com.fasterxml.jackson.core:jackson-core", "com.fasterxml.jackson.core:jackson-databind")
    assert found[jackson] == ("2.18.2", "2.19.0")  # two artifacts share one property: one bump
    assert not any("o:deep" in c or "o:major" in c for c in found)  # transitive, nothing to edit


def test_the_comparison_counts_touched_passed_builds_and_cleared_advisories(world):
    base = analysis(world)
    after = tree_exposure()
    outcomes = iter([failed(), passed(after), passed(after, cached=True)])

    def verify(edits):
        return next(outcomes)

    comparison = compare_naive(base, verify)
    assert comparison.touched == 3 and comparison.passed == 2 and comparison.builds == 2  # the cached one cost nothing
    assert set(comparison.cleared) == {"CVE-lib", "GHSA-core", "GHSA-db", "GHSA-deep", "GHSA-major"}
    assert comparison.as_dict()["failed"] == 1 and comparison.as_dict()["bumps"][0]["failed_stage"] == "compile"


def test_changes_taken_before_a_commit_are_used_as_given(world):
    base = analysis(world)
    taken = naive_changes(base)
    (world[0]).write_text(world[0].read_text().replace("<version>1.0.0</version>", "<version>1.0.2</version>", 1))  # a step was committed since
    seen = []
    compare_naive(base, lambda edits: (seen.append(edits), passed())[1], taken)
    assert any(edit.expected == "1.0.0" for edits in seen for edit in edits)  # not re-read from the modified file
