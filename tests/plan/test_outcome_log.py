import datetime
import json
from pathlib import Path

import pytest

from giml.core.model import GateResultRecord, ProjectRecord, RunRecord
from giml.maven.build import StageOutcome
from giml.maven.failures import COMPILE, ENFORCER_CONVERGENCE, INFRASTRUCTURE, Failure
from giml.plan.baseline import verify_baseline
from giml.plan.outcome_log import RewindFacts, log_baseline, oracle_strength
from giml.store.sqlite_store import SqliteStateStore
from tests.plan.test_baseline import VIOLATION, Scripted

T0 = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.UTC)
LOG = Path("/logs/x.log")
FAILURE = Failure(COMPILE, "0123456789abcdef", ("App.java:[<n>,<n>] cannot find symbol", "symbol: variable x"))


def ok(stage, seconds=1.5, hit=False, key=None, details=None):
    return StageOutcome(stage, True, seconds, LOG, None, cache_hit=hit, details=details, cache_key=key or f"key-{stage}" * 3)


def bad(stage, failure=FAILURE, seconds=2.0, details=None, key=None):
    return StageOutcome(stage, False, seconds, LOG, failure, details=details, cache_key=key or f"key-{stage}" * 3)


@pytest.fixture
def store(tmp_path):
    with SqliteStateStore(tmp_path / "state.db") as store:
        store.save_project(ProjectRecord("proj", Path("/p"), None, T0))
        for n in (1, 2, 3):
            store.start_run(RunRecord(f"run{n}", "proj", "abc", f"b{n}", Path(f"/w{n}"), T0 + datetime.timedelta(minutes=n)))
        yield store


def passing():
    return verify_baseline(Scripted(compile=ok("compile"), unit_test=ok("unit_test", 12.0, hit=True), enforcer=ok("enforcer", details={"violations": []})), Path("/wt"), 60)


def log(store, baseline, run="run1", **kw):
    args = {"tier": None, "oracle": None, "rewind": None, "jdk": "21.0.9"}
    return log_baseline(store, run, "proj", baseline, **{**args, **kw})


def test_a_passing_baseline_logs_a_state_an_attempt_per_stage_and_examples(store):
    logged = log(store, passing())
    assert (logged.state_id, logged.attempts, logged.examples_new) == ("run1:baseline", 3, 3)
    (state,) = store.list_states("run1")
    assert (state.id, state.parent_id, state.changes_json, state.status) == ("run1:baseline", None, "[]", "baseline_verified")
    attempts = store.list_attempts("run1")
    assert [(a.id, a.stage, a.outcome, a.failure_class, a.error_signature) for a in attempts] == [
        ("run1:baseline:compile:1", "compile", "pass", None, None),
        ("run1:baseline:unit_test:1", "unit_test", "pass", None, None),
        ("run1:baseline:enforcer:1", "enforcer", "pass", None, None)]  # fmt: skip
    assert [(a.cache_key, a.cache_hit, a.duration_ms, a.log_path) for a in attempts][1] == ("key-unit_test" * 3, True, 12000, "/logs/x.log")
    examples = store.list_examples()
    assert [e.id for e in examples] == ["run1:compile", "run1:enforcer", "run1:unit_test"] and {e.split_group for e in examples} == {"proj"}
    features, label = json.loads(examples[0].features_json), json.loads(examples[0].label_json)
    assert features == {"kind": "baseline", "stage": "compile", "failure_class": None, "signature": None, "key_lines": [],
                        "duration_seconds": 1.5, "cache_hit": False, "retries": 0, "tier": None, "oracle": None,
                        "rewind": {"is_rewind": False, "commit": None, "commit_date": None}, "jdk": "21.0.9", "violations": []}  # fmt: skip
    assert label == {"stage": "compile", "outcome": "pass", "failure_class": None}


def test_a_failure_is_logged_with_its_class_signature_and_normalised_lines(store):
    baseline = verify_baseline(Scripted(compile=bad("compile")), Path("/wt"), 60)
    logged = log(store, baseline)
    assert (logged.attempts, logged.examples_new) == (1, 1)  # later stages never ran, so there is nothing to log for them
    (state,) = store.list_states("run1")
    assert state.status == "baseline_failed"
    (attempt,) = store.list_attempts("run1")
    assert (attempt.outcome, attempt.failure_class, attempt.error_signature) == ("fail", COMPILE, "0123456789abcdef")
    (example,) = store.list_examples()
    features = json.loads(example.features_json)
    assert (features["failure_class"], features["signature"], features["key_lines"]) == (COMPILE, "0123456789abcdef", list(FAILURE.key_lines))
    assert json.loads(example.label_json) == {"stage": "compile", "outcome": "fail", "failure_class": COMPILE}


def test_enforcer_violations_are_features_of_the_enforcer_example(store):
    outcome = bad("enforcer", Failure(ENFORCER_CONVERGENCE, "feedfeedfeedfeed", ("x",)), details={"violations": [VIOLATION.to_dict()]})
    baseline = verify_baseline(Scripted(compile=ok("compile"), unit_test=ok("unit_test"), enforcer=outcome), Path("/wt"), 60)
    log(store, baseline)
    enforcer = next(e for e in store.list_examples() if e.id == "run1:enforcer")
    assert json.loads(enforcer.features_json)["violations"] == ["DependencyConvergence:org.x:lib"]


def test_an_identical_baseline_in_another_run_adds_attempts_but_no_examples(store):
    first = log(store, passing(), run="run1")
    second = log(store, passing(), run="run2")
    assert (first.examples_new, second.examples_new) == (3, 0)
    assert len(store.list_attempts("run1")) == len(store.list_attempts("run2")) == 3
    assert len(store.list_examples()) == 3


def test_the_same_failure_with_a_different_signature_is_a_different_example(store):
    other = Failure(COMPILE, "aaaaaaaaaaaaaaaa", ("something else",))
    log(store, verify_baseline(Scripted(compile=bad("compile")), Path("/wt"), 60), run="run1")
    logged = log(store, verify_baseline(Scripted(compile=bad("compile", other)), Path("/wt"), 60), run="run2")
    assert logged.examples_new == 1 and len(store.list_examples()) == 2


def test_rewind_facts_are_features_and_keep_a_rewound_failure_apart(store):
    plain = verify_baseline(Scripted(compile=bad("compile")), Path("/wt"), 60)
    rewound = verify_baseline(Scripted(compile=bad("compile")), Path("/wt"), 60, rewound=True)
    log(store, plain, run="run1")
    logged = log(store, rewound, run="run2", rewind=RewindFacts("deadbeef", "2025-01-01T00:00:00+00:00"))
    assert logged.examples_new == 1
    example = next(e for e in store.list_examples() if e.run_id == "run2")
    assert json.loads(example.features_json)["rewind"] == {"is_rewind": True, "commit": "deadbeef", "commit_date": "2025-01-01T00:00:00+00:00"}
    assert store.list_states("run2")[0].status == "baseline_failed"


def test_retries_tier_oracle_and_jdk_are_features(store):
    flaky = Scripted(compile=[bad("compile", Failure(INFRASTRUCTURE, "0" * 16, ("net",))), ok("compile")], unit_test=ok("unit_test"), enforcer=ok("enforcer"))
    baseline = verify_baseline(flaky, Path("/wt"), 60)
    oracle = {"unit_line_coverage": 91.2, "pit_test_strength": 93.1}
    log(store, baseline, tier="B", oracle=oracle, jdk="17.0.16")
    features = json.loads(next(e for e in store.list_examples() if e.id == "run1:compile").features_json)
    assert (features["retries"], features["tier"], features["oracle"], features["jdk"]) == (1, "B", oracle, "17.0.16")
    attempts = [a for a in store.list_attempts("run1") if a.stage == "compile"]
    assert [(a.id, a.outcome, a.failure_class) for a in attempts] == [("run1:baseline:compile:1", "pass", None)]  # the final outcome only


def test_oracle_strength_is_read_from_a_stored_assessment():
    record = GateResultRecord("r", "p", "abc", 3, "B", json.dumps({"measured": {"unit_line_coverage": 91.2, "unit_branch_coverage": 78.0,
                              "pit_test_strength": 93.1, "pit_mutation_coverage": 88.4}}), T0, T0)  # fmt: skip
    assert oracle_strength(record) == {"unit_line_coverage": 91.2, "unit_branch_coverage": 78.0, "pit_test_strength": 93.1, "pit_mutation_coverage": 88.4}
    assert oracle_strength(None) is None
    assert oracle_strength(GateResultRecord("r", "p", "abc", 3, None, json.dumps({}), T0, T0)) is None
