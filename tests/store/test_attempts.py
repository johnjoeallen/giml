import datetime
import sqlite3
from pathlib import Path

import pytest

from giml.core.model import BuildAttemptRecord, CandidateStateRecord, ExampleRecord, ProjectRecord, RunRecord
from giml.store.sqlite_store import SqliteStateStore

T0 = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture
def store(tmp_path):
    with SqliteStateStore(tmp_path / "state.db") as store:
        store.save_project(ProjectRecord("p", Path("/p"), None, T0))
        store.start_run(RunRecord("r1", "p", "abc", "b1", Path("/w1"), T0))
        store.start_run(RunRecord("r2", "p", "abc", "b2", Path("/w2"), T0 + datetime.timedelta(minutes=1)))
        yield store


def state(id="r1:baseline", run="r1", parent=None, status="baseline_verified", changes="[]"):
    return CandidateStateRecord(id, run, parent, changes, status)


def attempt(id="r1:baseline:compile:1", state_id="r1:baseline", stage="compile", outcome="pass", failure_class=None,
            signature=None, key="k" * 64, hit=False, duration=1500, log="/logs/01.log"):  # fmt: skip
    return BuildAttemptRecord(id, state_id, stage, outcome, failure_class, signature, key, hit, duration, log)


def example(id="r1:compile", run="r1", features='{"a":1}', label='{"outcome":"pass"}', group="p", dedup="d1"):
    return ExampleRecord(id, run, features, label, group, dedup)


def test_states_round_trip_in_run_order_and_can_have_a_parent(store):
    store.save_state(state())
    store.save_state(state("r1:cand1", parent="r1:baseline", status="verified", changes='[{"o:l":["1","2"]}]'))
    store.save_state(state("r2:baseline", run="r2"))
    assert store.list_states("r1") == [state(), state("r1:cand1", parent="r1:baseline", status="verified", changes='[{"o:l":["1","2"]}]')]
    assert store.list_states("r2") == [state("r2:baseline", run="r2")] and store.list_states("nope") == []


def test_attempts_round_trip_in_the_order_they_were_recorded(store):
    store.save_state(state())
    first = attempt()
    second = attempt("r1:baseline:unit_test:1", stage="unit_test", outcome="fail", failure_class="unit_test", signature="abcd" * 4,
                     hit=True, duration=0, log=None, key=None)  # fmt: skip
    store.save_attempt(first)
    store.save_attempt(second)
    assert store.list_attempts("r1") == [first, second]
    assert store.list_attempts("r2") == []


def test_an_attempt_needs_its_state_and_a_state_needs_its_run(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.save_attempt(attempt())
    with pytest.raises(sqlite3.IntegrityError):
        store.save_state(state("x", run="unknown-run"))


def test_a_state_id_is_unique(store):
    store.save_state(state())
    with pytest.raises(sqlite3.IntegrityError):
        store.save_state(state())


def test_examples_are_deduplicated_on_their_hash(store):
    assert store.save_example(example()) is True
    assert store.save_example(example("r2:compile", run="r2")) is False  # same dedup hash: an exact repeat
    assert store.save_example(example("r2:unit_test", run="r2", dedup="d2")) is True
    assert [e.id for e in store.list_examples()] == ["r1:compile", "r2:unit_test"]


def test_examples_round_trip_ordered_by_id(store):
    b, a = example("r1:zz", dedup="z"), example("r1:aa", dedup="a")
    store.save_example(b)
    store.save_example(a)
    assert store.list_examples() == [a, b]


def test_an_example_needs_its_run(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.save_example(example(run="unknown-run"))
