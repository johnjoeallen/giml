from pathlib import Path

import pytest

from giml.core.model import Coordinate
from giml.git.preflight import preflight
from giml.git.runner import Git
from giml.git.worktrees import WorktreeManager
from giml.maven.build import StageOutcome
from giml.maven.declarations import read_declarations
from giml.maven.enforcer import Violation
from giml.maven.failures import COMPILE, DUPLICATE_CLASSES, ENFORCER_CONVERGENCE, INFRASTRUCTURE, UNIT_TEST, Failure
from giml.maven.pom_change import AddPin, SetVersion
from giml.plan.baseline import verify_baseline
from giml.plan.trial import TrialRunner
from tests.git.repo_helpers import fingerprint, git, make_repo
from tests.plan.test_baseline import Scripted

POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <groupId>g</groupId>
    <artifactId>app</artifactId>
    <version>1</version>
    <dependencies>
        <dependency>
            <groupId>o</groupId>
            <artifactId>lib</artifactId>
            <version>1.0.0</version>
        </dependency>
    </dependencies>
</project>
"""
LOG = Path("/logs/x.log")
CONVERGENCE = Violation("DependencyConvergence", "org.x:lib", {"versions": ["1", "2"]})
OTHER = Violation("DependencyConvergence", "org.y:other", {"versions": ["3", "4"]})
DUPLICATES = Violation("BanDuplicateClasses", "a:a + b:b", {"artifacts": ["a:a:1", "b:b:1"], "classes": 3})


def ok(stage, seconds=1.0, hit=False, violations=()):
    details = {"violations": [v.to_dict() for v in violations]} if stage == "enforcer" else None
    return StageOutcome(stage, True, seconds, LOG, None, cache_hit=hit, details=details)


def failed(stage, failure_class, seconds=1.0, violations=(), signature="0123456789abcdef"):
    details = {"violations": [v.to_dict() for v in violations]} if stage == "enforcer" else None
    return StageOutcome(stage, False, seconds, LOG, Failure(failure_class, signature, ("line",)), details=details)


class Stages:
    """A BuildRunner whose answers come from a function of (stage, trial worktree); records what it saw."""

    def __init__(self, respond=None):
        self.respond, self.calls = respond or (lambda stage, worktree: ok(stage)), []

    def run_stage(self, worktree, stage, timeout_seconds):
        self.calls.append((Path(worktree), stage, timeout_seconds, (Path(worktree) / "pom.xml").read_text()))
        return self.respond(stage, Path(worktree))

    @property
    def stages(self):
        return [stage for _, stage, _, _ in self.calls]


@pytest.fixture
def world(tmp_path):
    repo = make_repo(tmp_path / "repo")
    (repo / "pom.xml").write_text(POM)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    state = preflight(repo)
    manager = WorktreeManager(state, tmp_path / "state", "run-1")
    result = manager.create_result("giml/result")
    return repo, manager, result


def runner(world, stages, baseline=None, **kw):
    repo, manager, result = world
    baseline = baseline or verify_baseline(Scripted(compile=ok("compile"), unit_test=ok("unit_test"), enforcer=ok("enforcer")), result, 60)
    return TrialRunner(manager, result, "", stages, baseline, 90, **kw)


def bump(result, version="1.0.3"):
    site = read_declarations([result / "pom.xml"]).declared[0].site
    return SetVersion(site, "1.0.0", version)


def test_a_passing_candidate_is_built_in_a_trial_worktree_with_the_changes_applied(world):
    repo, manager, result = world
    stages = Stages()
    outcome = runner(world, stages).verify([bump(result)])
    assert outcome.passed and not outcome.inconclusive and outcome.failed_stage is None and outcome.failure is None
    assert stages.stages == ["compile", "enforcer", "unit_test"]  # the cheap enforcer check runs before the tests
    trial = stages.calls[0][0]
    assert trial != result and trial.name == "run-1-trial-1" and all(call[0] == trial for call in stages.calls)
    assert all("<version>1.0.3</version>" in call[3] for call in stages.calls) and [c[2] for c in stages.calls] == [90, 90, 90]
    assert [s.stage for s in outcome.outcomes] == ["compile", "enforcer", "unit_test"] and outcome.seconds == 3.0
    assert not trial.exists()  # a trial leaves nothing behind
    assert "<version>1.0.0</version>" in (result / "pom.xml").read_text() and git(result, "status", "--porcelain") == ""


def test_a_trial_starts_from_the_result_branchs_tip_not_the_base(world):
    repo, manager, result = world
    (result / "pom.xml").write_text(POM.replace("1.0.0", "1.0.1"))
    git(result, "commit", "-qam", "accepted step")  # an accepted step moves the tip
    stages = Stages()
    runner(world, stages).verify([])
    assert "<version>1.0.1</version>" in stages.calls[0][3]


def test_trials_are_numbered_so_worktrees_never_collide(world):
    stages, trial_runner = Stages(), None
    trial_runner = runner(world, stages)
    trial_runner.verify([])
    trial_runner.verify([])
    assert [c[0].name for c in stages.calls][::3] == ["run-1-trial-1", "run-1-trial-2"]


def test_a_failing_build_stops_the_trial(world):
    stages = Stages(lambda stage, wt: failed("compile", COMPILE) if stage == "compile" else ok(stage))
    outcome = runner(world, stages).verify([bump(world[2])])
    assert (outcome.passed, outcome.failed_stage, outcome.failure.failure_class) == (False, "compile", COMPILE)
    assert stages.stages == ["compile"] and outcome.new_violations == ()


def test_failing_unit_tests_fail_the_candidate(world):
    stages = Stages(lambda stage, wt: failed("unit_test", UNIT_TEST) if stage == "unit_test" else ok(stage))
    outcome = runner(world, stages).verify([bump(world[2])])
    assert (outcome.passed, outcome.failed_stage, outcome.failure.failure_class) == (False, "unit_test", UNIT_TEST)
    assert stages.stages == ["compile", "enforcer", "unit_test"]


def clean_baseline(world):
    return verify_baseline(Scripted(compile=ok("compile"), unit_test=ok("unit_test"), enforcer=ok("enforcer")), world[2], 60)


def reference_baseline(world, *violations):
    outcome = failed("enforcer", ENFORCER_CONVERGENCE, violations=violations)
    return verify_baseline(Scripted(compile=ok("compile"), unit_test=ok("unit_test"), enforcer=outcome), world[2], 60)


def test_any_violation_is_new_when_the_baseline_was_clean(world):
    stages = Stages(lambda stage, wt: failed("enforcer", ENFORCER_CONVERGENCE, violations=[CONVERGENCE]) if stage == "enforcer" else ok(stage))
    outcome = runner(world, stages, clean_baseline(world)).verify([bump(world[2])])
    assert (outcome.passed, outcome.failed_stage, outcome.new_violations) == (False, "enforcer", (CONVERGENCE,))
    assert outcome.failure.failure_class == ENFORCER_CONVERGENCE and outcome.failure.key_lines == (CONVERGENCE.identity,)
    assert stages.stages == ["compile", "enforcer"]  # the tests are not run for a candidate that adds a violation


def test_the_baselines_violations_are_tolerated_and_only_new_ones_fail(world):
    baseline = reference_baseline(world, CONVERGENCE)
    same = Stages(lambda stage, wt: failed("enforcer", ENFORCER_CONVERGENCE, violations=[CONVERGENCE]) if stage == "enforcer" else ok(stage))
    outcome = runner(world, same, baseline).verify([bump(world[2])])
    assert outcome.passed and outcome.new_violations == () and outcome.resolved_violations == ()
    worse = Stages(lambda stage, wt: failed("enforcer", ENFORCER_CONVERGENCE, violations=[CONVERGENCE, OTHER]) if stage == "enforcer" else ok(stage))
    result = runner(world, worse, baseline).verify([bump(world[2])])
    assert (result.passed, result.new_violations) == (False, (OTHER,))


def test_a_candidate_that_removes_baseline_violations_reports_them_resolved(world):
    baseline = reference_baseline(world, CONVERGENCE, OTHER)
    stages = Stages(lambda stage, wt: failed("enforcer", ENFORCER_CONVERGENCE, violations=[OTHER]) if stage == "enforcer" else ok(stage))
    outcome = runner(world, stages, baseline).verify([bump(world[2])])
    assert outcome.passed and outcome.resolved_violations == (CONVERGENCE,)
    clean = runner(world, Stages(), baseline).verify([bump(world[2])])
    assert clean.passed and {v.identity for v in clean.resolved_violations} == {CONVERGENCE.identity, OTHER.identity}


def test_new_duplicate_classes_are_classified_as_such(world):
    stages = Stages(lambda stage, wt: failed("enforcer", DUPLICATE_CLASSES, violations=[DUPLICATES]) if stage == "enforcer" else ok(stage))
    outcome = runner(world, stages).verify([bump(world[2])])
    assert outcome.failure.failure_class == DUPLICATE_CLASSES


def test_the_signature_of_an_enforcer_failure_depends_on_the_new_violations_only(world):
    def with_baseline_noise(extra):
        stages = Stages(lambda stage, wt: failed("enforcer", ENFORCER_CONVERGENCE, violations=[CONVERGENCE, *extra], signature="varies" + str(len(extra))) if stage == "enforcer" else ok(stage))
        return runner(world, stages, reference_baseline(world, CONVERGENCE)).verify([bump(world[2])])

    assert with_baseline_noise([OTHER]).failure.signature == with_baseline_noise([OTHER]).failure.signature
    assert with_baseline_noise([OTHER]).failure.signature != with_baseline_noise([DUPLICATES]).failure.signature


def test_an_enforcer_without_a_usable_baseline_is_not_an_oracle(world):
    unusable = verify_baseline(Scripted(compile=ok("compile"), unit_test=ok("unit_test"),
                                        enforcer=failed("enforcer", "resolution")), world[2], 60)  # fmt: skip
    stages = Stages(lambda stage, wt: failed("enforcer", ENFORCER_CONVERGENCE, violations=[CONVERGENCE]) if stage == "enforcer" else ok(stage))
    outcome = runner(world, stages, unusable).verify([bump(world[2])])
    assert outcome.passed and stages.stages == ["compile", "unit_test"]


def test_a_stage_that_failed_at_the_baseline_is_not_run(world):
    baseline = verify_baseline(Scripted(compile=ok("compile"), unit_test=ok("unit_test"), enforcer=ok("enforcer")), world[2], 60)
    only_compile = type(baseline)(tuple(s if s.stage == "compile" else type(s)(s.stage, "baseline_failed", s.attempts, s.outcome) for s in baseline.stages), None, False)
    stages = Stages()
    runner(world, stages, only_compile).verify([])
    assert stages.stages == ["compile"]


def test_an_infrastructure_failure_is_retried_and_then_inconclusive_not_a_candidate_failure(world):
    network = failed("compile", INFRASTRUCTURE)
    flaky = Stages(lambda stage, wt: (network if len(flaky.calls) == 1 else ok(stage)))
    outcome = runner(world, flaky).verify([bump(world[2])])
    assert outcome.passed and flaky.stages[:2] == ["compile", "compile"]
    down = Stages(lambda stage, wt: network)
    result = runner(world, down).verify([bump(world[2])])
    assert (result.passed, result.inconclusive, result.failed_stage) == (False, True, "compile") and down.stages == ["compile", "compile"]
    assert runner(world, Stages(lambda stage, wt: network), retries=0).verify([]).inconclusive


def test_the_trial_worktree_is_removed_even_when_the_runner_raises(world):
    def boom(stage, worktree):
        raise RuntimeError("mvn vanished")

    with pytest.raises(RuntimeError, match="mvn vanished"):
        runner(world, Stages(boom)).verify([bump(world[2])])
    assert Git(world[0]).out("worktree", "list").count("\n") == 1  # the main checkout and the result worktree only


def test_changes_that_cannot_be_applied_raise_and_leave_no_trial_behind(world):
    from giml.maven.pom_change import ChangeError

    stale = SetVersion(read_declarations([world[2] / "pom.xml"]).declared[0].site, "9.9.9", "1.0.3")
    with pytest.raises(ChangeError):
        runner(world, Stages()).verify([stale])
    assert Git(world[0]).out("worktree", "list").count("\n") == 1


def test_a_pin_is_applied_in_the_trial_too(world):
    stages = Stages()
    runner(world, stages).verify([AddPin(world[2] / "pom.xml", Coordinate.parse("c:d"), "2")])
    assert "<artifactId>d</artifactId>" in stages.calls[0][3]


def test_the_developers_checkout_and_the_result_worktree_are_never_touched(world):
    repo, manager, result = world
    before, result_before = fingerprint(repo), fingerprint(result)
    runner(world, Stages()).verify([bump(result)])
    assert fingerprint(repo) == before and fingerprint(result) == result_before


def test_cache_hits_are_counted(world):
    stages = Stages(lambda stage, wt: ok(stage, 2.0, hit=stage != "compile"))
    outcome = runner(world, stages).verify([])
    assert (outcome.cache_hits, outcome.seconds) == (2, 6.0)


def test_a_project_in_a_subdirectory_is_built_where_it_lives(world, tmp_path):
    repo, manager, result = world
    (result / "sub").mkdir()
    (result / "sub" / "pom.xml").write_text(POM)
    git(result, "add", "-A")
    git(result, "commit", "-qm", "sub")
    baseline = clean_baseline(world)
    stages = Stages()
    TrialRunner(manager, result, "sub", stages, baseline, 60).verify([])
    assert all(call[0].name == "sub" for call in stages.calls)


def test_after_commits_the_reference_becomes_the_result_worktrees_own_violations(world):
    baseline = reference_baseline(world, CONVERGENCE, OTHER)
    stages = Stages(lambda stage, wt: failed("enforcer", ENFORCER_CONVERGENCE, violations=[OTHER]) if stage == "enforcer" else ok(stage))
    trial_runner = runner(world, stages, baseline)
    assert trial_runner.refresh_reference() == (OTHER,) and stages.stages == ["enforcer"]
    again = Stages(lambda stage, wt: failed("enforcer", ENFORCER_CONVERGENCE, violations=[CONVERGENCE, OTHER]) if stage == "enforcer" else ok(stage))
    trial_runner.runner = again
    assert trial_runner.verify([bump(world[2])]).new_violations == (CONVERGENCE,)  # a resolved violation may not come back


def test_the_reference_is_kept_when_the_enforcer_is_not_an_oracle(world):
    unusable = verify_baseline(Scripted(compile=ok("compile"), unit_test=ok("unit_test"),
                                        enforcer=failed("enforcer", "resolution")), world[2], 60)  # fmt: skip
    stages = Stages()
    assert runner(world, stages, unusable).refresh_reference() == () and stages.stages == []


def exposure_of_tree(*findings):
    from tests.plan.test_exposure import tree_exposure

    return tree_exposure(*findings)


def test_a_candidate_that_makes_the_vulnerabilities_worse_fails_without_a_build(world):
    from tests.plan.test_exposure import finding

    stages = Stages()
    trial_runner = runner(world, stages)
    trial_runner.resolve_exposure = lambda project: exposure_of_tree(finding("A", "o:l", "1"))
    trial_runner.exposure_reference = exposure_of_tree()
    outcome = trial_runner.verify([bump(world[2])])
    assert (outcome.passed, outcome.failed_stage, outcome.failure.failure_class) == (False, "exposure", "vulnerability_worse")
    assert stages.calls == [] and outcome.failure.key_lines[-1] == "new advisory A"


def test_a_candidate_that_is_not_worse_goes_on_to_the_builds(world):
    stages = Stages()
    trial_runner = runner(world, stages)
    trial_runner.resolve_exposure = lambda project: exposure_of_tree()
    trial_runner.exposure_reference = exposure_of_tree()
    outcome = trial_runner.verify([bump(world[2])])
    assert outcome.passed and stages.stages == ["compile", "enforcer", "unit_test"] and outcome.outcomes[0].stage == "exposure"


def test_a_tree_that_cannot_be_resolved_fails_as_resolution(world):
    from giml.maven.tree import ResolutionError

    def broken(project):
        raise ResolutionError(f"Maven could not resolve {project}: see the log")

    trial_runner = runner(world, Stages())
    trial_runner.resolve_exposure, trial_runner.exposure_reference = broken, exposure_of_tree()
    outcome = trial_runner.verify([bump(world[2])])
    assert (outcome.failed_stage, outcome.failure.failure_class) == ("exposure", "resolution")
    assert "<project>" in outcome.failure.key_lines[0]


def pit_outcome(**counts):
    return StageOutcome("pit", True, 5.0, LOG, None, details={"mutations": counts})


def pit_baseline(world):
    scripted = Scripted(compile=ok("compile"), unit_test=ok("unit_test"), enforcer=ok("enforcer"), pit=pit_outcome(KILLED=9, SURVIVED=1))
    return verify_baseline(scripted, world[2], 60, stages_to_run=("compile", "unit_test", "enforcer", "pit"))


def pit_stages(**counts):
    return Stages(lambda stage, wt: pit_outcome(**counts) if stage == "pit" else ok(stage))


def test_pit_runs_last_and_a_candidate_that_keeps_the_scores_passes(world):
    stages = pit_stages(KILLED=9, SURVIVED=1)
    trial_runner = runner(world, stages, pit_baseline(world))
    trial_runner.pit_floors = (80.0, 85.0)
    assert trial_runner.verify([bump(world[2])]).passed
    assert stages.stages == ["compile", "enforcer", "unit_test", "pit"]


def test_a_candidate_whose_mutation_scores_fall_below_the_floor_fails_after_its_tests_passed(world):
    trial_runner = runner(world, pit_stages(KILLED=7, SURVIVED=3), pit_baseline(world))
    trial_runner.pit_floors = (80.0, 85.0)
    outcome = trial_runner.verify([bump(world[2])])
    assert (outcome.passed, outcome.failed_stage, outcome.failure.failure_class) == (False, "pit", "mutation_score_dropped")
    assert outcome.failure.key_lines == ("mutation coverage 70.0 is below 80.0", "test strength 70.0 is below 85.0")


def test_pit_that_finds_no_mutants_is_a_failure_not_a_pass(world):
    trial_runner = runner(world, pit_stages(), pit_baseline(world))
    trial_runner.pit_floors = (80.0, 85.0)
    assert trial_runner.verify([bump(world[2])]).failure.key_lines == ("PIT produced no mutations",)


def test_without_floors_pit_only_has_to_run(world):
    assert runner(world, pit_stages(KILLED=1, SURVIVED=9), pit_baseline(world)).verify([bump(world[2])]).passed
