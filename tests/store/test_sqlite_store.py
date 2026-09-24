import datetime
import sqlite3
from pathlib import Path

import pytest

from giml.core.model import SnapshotInfo
from giml.store.sqlite_store import (
    Migration,
    SqliteStateStore,
    StoreError,
    packaged_migrations,
    validate_migrations,
)

UTC = datetime.UTC


def snapshot(snapshot_id: str, source: str, hour: int) -> SnapshotInfo:
    fetched = datetime.datetime(2026, 9, 24, hour, 0, tzinfo=UTC)
    return SnapshotInfo(snapshot_id, source, fetched, "ab" * 32, Path(f"/state/{snapshot_id}"))


def test_packaged_migrations_are_contiguous_and_create_snapshot_table():
    migrations = packaged_migrations()
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    assert "CREATE TABLE snapshot" in migrations[0].sql


def test_fresh_database_is_migrated_to_latest(tmp_path):
    with SqliteStateStore(tmp_path / "sub" / "state.db") as store:
        assert store.schema_version == len(packaged_migrations())


def test_reopening_does_not_reapply_migrations(tmp_path):
    db = tmp_path / "state.db"
    with SqliteStateStore(db) as store:
        store.record_snapshot(snapshot("osv-1", "osv", 1))
    with SqliteStateStore(db) as store:
        assert [s.id for s in store.list_snapshots()] == ["osv-1"]


def test_newer_database_is_refused(tmp_path):
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(StoreError, match="schema version 99 is newer"):
        SqliteStateStore(db)


def test_failed_migration_is_rolled_back(tmp_path):
    db = tmp_path / "state.db"
    good = Migration(1, "0001_a.sql", "CREATE TABLE a (x INTEGER);")
    bad = Migration(2, "0002_b.sql", "CREATE TABLE b (x INTEGER); INSERT INTO missing VALUES (1);")
    with pytest.raises(StoreError, match="migration 0002_b.sql failed"):
        SqliteStateStore(db, migrations=[good, bad])
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert tables == {"a"}


@pytest.mark.parametrize("versions", [[2], [1, 3], [1, 1]])
def test_migration_numbering_must_be_contiguous(versions):
    migrations = [Migration(v, f"{v:04d}_x.sql", "") for v in versions]
    with pytest.raises(StoreError, match="without gaps"):
        validate_migrations(migrations)


def test_latest_snapshot_is_per_source_and_newest(tmp_path):
    with SqliteStateStore(tmp_path / "state.db") as store:
        store.record_snapshot(snapshot("osv-old", "osv", 1))
        store.record_snapshot(snapshot("osv-new", "osv", 3))
        store.record_snapshot(snapshot("central-1", "central", 2))
        assert store.latest_snapshot("osv").id == "osv-new"
        assert store.latest_snapshot("central").id == "central-1"
        assert store.latest_snapshot("other") is None
        assert [s.id for s in store.list_snapshots()] == ["osv-new", "central-1", "osv-old"]


def test_snapshot_round_trips_all_fields(tmp_path):
    original = snapshot("osv-1", "osv", 5)
    with SqliteStateStore(tmp_path / "state.db") as store:
        store.record_snapshot(original)
        assert store.latest_snapshot("osv") == original


def test_duplicate_snapshot_id_is_rejected(tmp_path):
    with SqliteStateStore(tmp_path / "state.db") as store:
        store.record_snapshot(snapshot("osv-1", "osv", 1))
        with pytest.raises(sqlite3.IntegrityError):
            store.record_snapshot(snapshot("osv-1", "osv", 2))


def test_naive_timestamps_are_rejected(tmp_path):
    naive = SnapshotInfo("x", "osv", datetime.datetime(2026, 9, 24), "00", Path("/x"))
    with SqliteStateStore(tmp_path / "state.db") as store, pytest.raises(ValueError, match="timezone"):
        store.record_snapshot(naive)


def test_packaged_migration_names_match_files():
    assert packaged_migrations()[0].name == "0001_snapshot.sql"


def test_state_directory_is_created_with_parents(tmp_path):
    with SqliteStateStore(tmp_path / "a" / "b" / "state.db") as store:
        assert store.schema_version >= 1


from giml.core.model import ProjectRecord, RunRecord  # noqa: E402

T0 = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def project(project_id="demo-12345678", path="/repo"):
    return ProjectRecord(project_id, Path(path), "ab" * 32, T0)


def run(run_id, project_id="demo-12345678", minute=0, rewind=None):
    started = T0 + datetime.timedelta(minutes=minute)
    return RunRecord(run_id, project_id, "c" * 40, f"giml/ccccccc/{run_id}", Path(f"/state/wt/{run_id}"),
                     started, rewind_from_sha=rewind)  # fmt: skip


def test_project_round_trip_and_refresh(tmp_path):
    with SqliteStateStore(tmp_path / "state.db") as store:
        store.save_project(project())
        store.save_project(ProjectRecord("demo-12345678", Path("/moved"), None, T0 + datetime.timedelta(days=1)))
        assert store.get_project("demo-12345678") == ProjectRecord("demo-12345678", Path("/moved"), None, T0)
        assert store.get_project("unknown") is None
        store.save_project(project("another-00000000", "/other"))
        assert [p.id for p in store.list_projects()] == ["another-00000000", "demo-12345678"]


def test_run_lifecycle(tmp_path):
    with SqliteStateStore(tmp_path / "state.db") as store:
        store.save_project(project())
        store.start_run(run("r1", rewind="d" * 40))
        store.start_run(run("r2", minute=5))
        assert [r.id for r in store.list_runs(unfinished_only=True)] == ["r1", "r2"]
        store.finish_run("r1", T0 + datetime.timedelta(minutes=1), "planning_not_implemented")
        [unfinished] = store.list_runs("demo-12345678", unfinished_only=True)
        assert unfinished == run("r2", minute=5)
        [finished, _] = store.list_runs("demo-12345678")
        assert finished.finished_at == T0 + datetime.timedelta(minutes=1)
        assert (finished.stop_reason, finished.rewind_from_sha) == ("planning_not_implemented", "d" * 40)
        assert store.list_runs("other") == []


def test_finishing_twice_or_unknown_run_is_an_error(tmp_path):
    with SqliteStateStore(tmp_path / "state.db") as store:
        store.save_project(project())
        store.start_run(run("r1"))
        store.finish_run("r1", T0, "done")
        with pytest.raises(StoreError, match="run r1 is unknown or already finished"):
            store.finish_run("r1", T0, "again")
        with pytest.raises(StoreError, match="run nope is unknown"):
            store.finish_run("nope", T0, "x")


def test_run_requires_known_project(tmp_path):
    with SqliteStateStore(tmp_path / "state.db") as store, pytest.raises(sqlite3.IntegrityError):
        store.start_run(run("r1", project_id="missing"))
