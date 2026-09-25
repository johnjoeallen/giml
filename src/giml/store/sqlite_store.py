"""SQLite implementation of the StateStore (spec section 11).

Schema changes are numbered SQL files in ``giml/store/migrations``; the applied version is kept in
``PRAGMA user_version`` and each migration runs in its own transaction.
"""

from __future__ import annotations

import datetime
import re
import sqlite3
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from giml.core.model import (
    BuildAttemptRecord, CandidateStateRecord, ExampleRecord, GateResultRecord, ProjectRecord, RunRecord, SnapshotInfo,
)  # fmt: skip

_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class StoreError(RuntimeError):
    """The state database cannot be used (for example it is newer than this giml)."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def packaged_migrations() -> list[Migration]:
    """The migrations shipped with giml, validated to be numbered 1..N without gaps."""
    files = resources.files("giml.store") / "migrations"
    migrations = []
    for entry in files.iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match:
            migrations.append(Migration(int(match.group(1)), entry.name, entry.read_text("utf-8")))
    return validate_migrations(migrations)


def validate_migrations(migrations: list[Migration]) -> list[Migration]:
    ordered = sorted(migrations, key=lambda m: m.version)
    expected = list(range(1, len(ordered) + 1))
    if [m.version for m in ordered] != expected:
        raise StoreError(f"migrations must be numbered 1..N without gaps: {[m.name for m in ordered]}")
    return ordered


def _utc_text(moment: datetime.datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    # Fixed width so text order is time order.
    return moment.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class SqliteStateStore:
    def __init__(self, path: Path, migrations: list[Migration] | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None)
        try:
            self._conn.execute("PRAGMA foreign_keys = ON")  # pragma: no mutate
            self._migrate(packaged_migrations() if migrations is None else validate_migrations(migrations))
        except BaseException:
            self._conn.close()
            raise

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SqliteStateStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def _migrate(self, migrations: list[Migration]) -> None:
        current = self.schema_version
        if current > len(migrations):
            raise StoreError(
                f"state database schema version {current} is newer than this giml supports "
                f"({len(migrations)}); upgrade giml"
            )
        for migration in migrations[current:]:
            try:
                self._conn.executescript(
                    f"BEGIN;\n{migration.sql}\nPRAGMA user_version = {migration.version};\nCOMMIT;"
                )
            except sqlite3.Error as exc:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")  # pragma: no mutate
                raise StoreError(f"migration {migration.name} failed: {exc}") from exc

    def record_snapshot(self, snapshot: SnapshotInfo) -> None:
        self._conn.execute(
            "INSERT INTO snapshot (id, source, fetched_at, content_hash, path) VALUES (?, ?, ?, ?, ?)",  # pragma: no mutate
            (snapshot.id, snapshot.source, _utc_text(snapshot.fetched_at), snapshot.content_hash,
             str(snapshot.path)),
        )  # fmt: skip

    def latest_snapshot(self, source: str) -> SnapshotInfo | None:
        row = self._conn.execute(
            "SELECT id, source, fetched_at, content_hash, path FROM snapshot "  # pragma: no mutate
            "WHERE source = ? ORDER BY fetched_at DESC, id DESC LIMIT 1",  # pragma: no mutate
            (source,),
        ).fetchone()
        return _snapshot_from_row(row) if row else None

    def list_snapshots(self) -> list[SnapshotInfo]:
        rows = self._conn.execute(
            "SELECT id, source, fetched_at, content_hash, path FROM snapshot "  # pragma: no mutate
            "ORDER BY fetched_at DESC, id DESC"  # pragma: no mutate
        ).fetchall()
        return [_snapshot_from_row(row) for row in rows]


    def save_project(self, project: ProjectRecord) -> None:
        """Insert a project, or refresh its path and remote hash if it is already known."""
        self._conn.execute(
            "INSERT INTO project (id, path, remote_url_hash, created_at) VALUES (?, ?, ?, ?) "  # pragma: no mutate
            "ON CONFLICT (id) DO UPDATE SET path = excluded.path, remote_url_hash = excluded.remote_url_hash",  # pragma: no mutate
            (project.id, str(project.path), project.remote_url_hash, _utc_text(project.created_at)),
        )

    def get_project(self, project_id: str) -> ProjectRecord | None:
        row = self._conn.execute(
            "SELECT id, path, remote_url_hash, created_at FROM project WHERE id = ?",  # pragma: no mutate
            (project_id,),
        ).fetchone()
        return _project_from_row(row) if row else None

    def list_projects(self) -> list[ProjectRecord]:
        rows = self._conn.execute(
            "SELECT id, path, remote_url_hash, created_at FROM project ORDER BY id"  # pragma: no mutate
        ).fetchall()
        return [_project_from_row(row) for row in rows]

    def start_run(self, run: RunRecord) -> None:
        self._conn.execute(
            "INSERT INTO run (id, project_id, base_sha, branch, worktree_path, started_at, rewind_from_sha, kind) "  # pragma: no mutate
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",  # pragma: no mutate
            (run.id, run.project_id, run.base_sha, run.branch, str(run.worktree_path),
             _utc_text(run.started_at), run.rewind_from_sha, run.kind),
        )  # fmt: skip

    def finish_run(self, run_id: str, finished_at: datetime.datetime, stop_reason: str) -> None:
        updated = self._conn.execute(
            "UPDATE run SET finished_at = ?, stop_reason = ? WHERE id = ? AND finished_at IS NULL",  # pragma: no mutate
            (_utc_text(finished_at), stop_reason, run_id),
        ).rowcount
        if updated != 1:
            raise StoreError(f"run {run_id} is unknown or already finished")

    def list_runs(self, project_id: str | None = None, unfinished_only: bool = False) -> list[RunRecord]:
        clauses, params = [], []
        if project_id is not None:
            clauses.append("project_id = ?")  # pragma: no mutate
            params.append(project_id)
        if unfinished_only:
            clauses.append("finished_at IS NULL")  # pragma: no mutate
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            "SELECT id, project_id, base_sha, branch, worktree_path, started_at, finished_at, "  # pragma: no mutate
            f"stop_reason, rewind_from_sha, kind FROM run{where} ORDER BY started_at, id",  # pragma: no mutate
            params,
        ).fetchall()
        return [_run_from_row(row) for row in rows]

    def save_gate_result(self, result: GateResultRecord) -> None:
        self._conn.execute(
            "INSERT INTO gate_result (run_id, project_id, base_sha, config_version, earned_tier, json, "  # pragma: no mutate
            "measured_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",  # pragma: no mutate
            (result.run_id, result.project_id, result.base_sha, result.config_version, result.earned_tier,
             result.json, _utc_text(result.measured_at), _utc_text(result.expires_at)),
        )  # fmt: skip

    def latest_gate_result(self, project_id: str) -> GateResultRecord | None:
        row = self._conn.execute(
            "SELECT run_id, project_id, base_sha, config_version, earned_tier, json, measured_at, expires_at "  # pragma: no mutate
            "FROM gate_result WHERE project_id = ? ORDER BY measured_at DESC, run_id DESC LIMIT 1",  # pragma: no mutate
            (project_id,),
        ).fetchone()
        return _gate_result_from_row(row) if row else None


    def save_state(self, state: CandidateStateRecord) -> None:
        self._conn.execute(
            "INSERT INTO candidate_state (id, run_id, parent_id, changes_json, status) VALUES (?, ?, ?, ?, ?)",  # pragma: no mutate
            (state.id, state.run_id, state.parent_id, state.changes_json, state.status),
        )

    def list_states(self, run_id: str) -> list[CandidateStateRecord]:
        rows = self._conn.execute(
            "SELECT id, run_id, parent_id, changes_json, status FROM candidate_state "  # pragma: no mutate
            "WHERE run_id = ? ORDER BY rowid",  # pragma: no mutate
            (run_id,),
        ).fetchall()
        return [CandidateStateRecord(*row) for row in rows]

    def save_attempt(self, attempt: BuildAttemptRecord) -> None:
        self._conn.execute(
            "INSERT INTO build_attempt (id, state_id, stage, outcome, failure_class, error_signature, cache_key, "  # pragma: no mutate
            "cache_hit, duration_ms, log_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",  # pragma: no mutate
            (attempt.id, attempt.state_id, attempt.stage, attempt.outcome, attempt.failure_class, attempt.error_signature,
             attempt.cache_key, int(attempt.cache_hit), attempt.duration_ms, attempt.log_path),
        )  # fmt: skip

    def list_attempts(self, run_id: str) -> list[BuildAttemptRecord]:
        rows = self._conn.execute(
            "SELECT a.id, a.state_id, a.stage, a.outcome, a.failure_class, a.error_signature, a.cache_key, "  # pragma: no mutate
            "a.cache_hit, a.duration_ms, a.log_path FROM build_attempt a "  # pragma: no mutate
            "JOIN candidate_state s ON s.id = a.state_id WHERE s.run_id = ? ORDER BY a.rowid",  # pragma: no mutate
            (run_id,),
        ).fetchall()
        return [BuildAttemptRecord(*row[:7], bool(row[7]), row[8], row[9]) for row in rows]

    def save_example(self, example: ExampleRecord) -> bool:
        inserted = self._conn.execute(
            "INSERT OR IGNORE INTO example (id, run_id, features_json, label_json, split_group, dedup_hash) "  # pragma: no mutate
            "VALUES (?, ?, ?, ?, ?, ?)",  # pragma: no mutate
            (example.id, example.run_id, example.features_json, example.label_json, example.split_group, example.dedup_hash),
        ).rowcount
        return inserted == 1

    def list_examples(self) -> list[ExampleRecord]:
        rows = self._conn.execute(
            "SELECT id, run_id, features_json, label_json, split_group, dedup_hash FROM example ORDER BY id"  # pragma: no mutate
        ).fetchall()
        return [ExampleRecord(*row) for row in rows]


def _gate_result_from_row(row: tuple) -> GateResultRecord:
    run_id, project_id, base_sha, version, tier, data, measured, expires = row
    return GateResultRecord(run_id, project_id, base_sha, version, tier, data, _parse_time(measured), _parse_time(expires))


def _parse_time(text: str | None) -> datetime.datetime | None:
    return datetime.datetime.fromisoformat(text) if text else None


def _project_from_row(row: tuple) -> ProjectRecord:
    project_id, path, remote_url_hash, created_at = row
    return ProjectRecord(project_id, Path(path), remote_url_hash, _parse_time(created_at))


def _run_from_row(row: tuple) -> RunRecord:
    run_id, project_id, base_sha, branch, worktree, started, finished, stop_reason, rewind, kind = row
    return RunRecord(run_id, project_id, base_sha, branch, Path(worktree), _parse_time(started),
                     _parse_time(finished), stop_reason, rewind, kind)  # fmt: skip


def _snapshot_from_row(row: tuple) -> SnapshotInfo:
    snapshot_id, source, fetched_at, content_hash, path = row
    return SnapshotInfo(
        snapshot_id, source, datetime.datetime.fromisoformat(fetched_at), content_hash, Path(path)
    )
