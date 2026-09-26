import datetime
import json
from pathlib import Path

import pytest

from giml.core.model import ProjectRecord, RunRecord
from giml.maven.build import StageOutcome
from giml.maven.failures import COMPILE, Failure
from giml.plan.outcome_log import log_trial
from giml.plan.trial import TrialResult
from giml.store.sqlite_store import SqliteStateStore

T0 = datetime.datetime(2026, 9, 26, 12, 0, tzinfo=datetime.UTC)
LIB = {"key": "dep:o:lib@1.0.0", "members": ["o:lib"], "kind": "cve_patch", "from": "1.0.0", "to": "1.0.2"}


@pytest.fixture
def store(tmp_path):
    with SqliteStateStore(tmp_path / "state.db") as store:
        store.save_project(ProjectRecord("p", Path("/p"), None, T0))
        store.start_run(RunRecord("r1", "p", "sha", "b", Path("/w"), T0))
        from giml.plan.baseline import Baseline
        from giml.plan.outcome_log import log_baseline

        log_baseline(store, "r1", "p", Baseline((), None, False), tier="A", oracle=None, rewind=None, jdk="21")
        yield store


def stage(name, passed=True, failure=None, hit=False):
    return StageOutcome(name, passed, 2.0, Path("/logs/x.log"), failure, cache_hit=hit)


def result(*outcomes, passed=False, inconclusive=False, failure=None, stage_name=None):
    return TrialResult(passed, inconclusive, stage_name, failure, tuple(outcomes), (), ())


def log(store, changes, outcome, number=1):
    return log_trial(store, "r1", "p", number, changes, outcome, tier="A", oracle=None, jdk="21", now=T0)


FAILURE = Failure(COMPILE, "abc123", ("cannot find symbol X",))


def test_a_trial_is_logged_as_a_state_with_an_attempt_and_example_per_stage(store):
    outcome = result(stage("compile"), stage("unit_test", False, FAILURE), stage_name="unit_test", failure=FAILURE)
    log(store, [LIB], outcome)
    (state,) = [s for s in store.list_states("r1") if s.id.endswith("trial-001")]
    assert (state.parent_id, state.status, json.loads(state.changes_json)) == ("r1:baseline", "failed", [LIB])
    assert [(a.stage, a.outcome) for a in store.list_attempts("r1") if a.state_id == state.id] == [("compile", "pass"), ("unit_test", "fail")]
    labels = [json.loads(e.label_json) for e in store.list_examples() if e.id.startswith("r1:trial-001")]
    assert {(l["stage"], l["outcome"]) for l in labels} == {("compile", "pass"), ("unit_test", "fail")}


def test_a_failed_single_step_is_remembered_with_how_often_it_failed_before(store):
    outcome = result(stage("compile", False, FAILURE), stage_name="compile", failure=FAILURE)
    first = log(store, [LIB], outcome, 1)
    second = log(store, [LIB], outcome, 2)
    assert [e["prior_failures"] for e in first + second] == [0, 1]
    assert first[0]["coordinate"] == "o:lib" and first[0]["failure_class"] == COMPILE
    assert store.list_transitions("o:lib")[0].count == 2


def test_passes_combinations_and_environment_failures_teach_nothing_about_transitions(store):
    assert log(store, [LIB], result(stage("compile"), passed=True), 1) == []
    two = [LIB, {**LIB, "key": "dep:o:other@1", "members": ["o:other"]}]
    assert log(store, two, result(stage("compile", False, FAILURE), failure=FAILURE), 2) == []
    infra = Failure("infrastructure", "zzz", ())
    assert log(store, [LIB], result(stage("compile", False, infra), inconclusive=True, failure=infra), 3) == []
    assert store.list_transitions() == []
    assert not [a for a in store.list_attempts("r1") if a.state_id.endswith("trial-003")]  # the failed environment is not an attempt


def test_a_step_without_a_known_version_move_is_not_a_transition(store):
    outcome = result(stage("compile", False, FAILURE), failure=FAILURE)
    assert log(store, [{**LIB, "from": None, "to": None}], outcome) == []
