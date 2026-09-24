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

from giml.core.model import ProjectRecord, RunRecord, SnapshotInfo

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
            "INSERT INTO run (id, project_id, base_sha, branch, worktree_path, started_at, rewind_from_sha) "  # pragma: no mutate
            "VALUES (?, ?, ?, ?, ?, ?, ?)",  # pragma: no mutate
            (run.id, run.project_id, run.base_sha, run.branch, str(run.worktree_path),
             _utc_text(run.started_at), run.rewind_from_sha),
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
            f"stop_reason, rewind_from_sha FROM run{where} ORDER BY started_at, id",  # pragma: no mutate
            params,
        ).fetchall()
        return [_run_from_row(row) for row in rows]


def _parse_time(text: str | None) -> datetime.datetime | None:
    return datetime.datetime.fromisoformat(text) if text else None


def _project_from_row(row: tuple) -> ProjectRecord:
    project_id, path, remote_url_hash, created_at = row
    return ProjectRecord(project_id, Path(path), remote_url_hash, _parse_time(created_at))


def _run_from_row(row: tuple) -> RunRecord:
    run_id, project_id, base_sha, branch, worktree, started, finished, stop_reason, rewind = row
    return RunRecord(run_id, project_id, base_sha, branch, Path(worktree), _parse_time(started),
                     _parse_time(finished), stop_reason, rewind)  # fmt: skip


def _snapshot_from_row(row: tuple) -> SnapshotInfo:
    snapshot_id, source, fetched_at, content_hash, path = row
    return SnapshotInfo(
        snapshot_id, source, datetime.datetime.fromisoformat(fetched_at), content_hash, Path(path)
    )
