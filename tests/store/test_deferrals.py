import datetime
import sqlite3
from pathlib import Path

import pytest

from giml.core.model import DeferralRecord, ProjectRecord
from giml.store.sqlite_store import SqliteStateStore

T0 = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture
def store(tmp_path):
    with SqliteStateStore(tmp_path / "state.db") as store:
        store.save_project(ProjectRecord("p", Path("/p"), None, T0))
        store.save_project(ProjectRecord("q", Path("/q"), None, T0))
        yield store


def deferral(id="d1", project="p", coordinate="o:lib", held="1.0.0", reason="no fixing version passed", trigger='{"new_release":"o:lib"}',
             created=T0, resolved=None):  # fmt: skip
    return DeferralRecord(id, project, coordinate, held, reason, trigger, created, resolved)


def test_a_deferral_round_trips(store):
    store.save_deferral(deferral())
    assert store.list_deferrals("p") == [deferral()]
    assert store.list_deferrals("q") == []


def test_open_deferrals_exclude_resolved_ones_and_are_ordered_by_creation(store):
    store.save_deferral(deferral("d2", coordinate="o:b", created=T0 + datetime.timedelta(minutes=2)))
    store.save_deferral(deferral("d1", coordinate="o:a"))
    store.save_deferral(deferral("d3", coordinate="o:c", created=T0 + datetime.timedelta(minutes=1), resolved=T0 + datetime.timedelta(hours=1)))
    assert [d.id for d in store.list_deferrals("p")] == ["d1", "d3", "d2"]
    assert [d.id for d in store.list_deferrals("p", open_only=True)] == ["d1", "d2"]


def test_a_deferral_can_be_resolved_once(store):
    store.save_deferral(deferral())
    store.resolve_deferral("d1", T0 + datetime.timedelta(days=1))
    assert store.list_deferrals("p")[0].resolved_at == T0 + datetime.timedelta(days=1)
    with pytest.raises(Exception, match="unknown or already resolved"):
        store.resolve_deferral("d1", T0 + datetime.timedelta(days=2))
    with pytest.raises(Exception, match="unknown or already resolved"):
        store.resolve_deferral("nope", T0)


def test_a_deferral_needs_its_project(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.save_deferral(deferral(project="unknown"))
