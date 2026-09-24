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

from giml.core.model import SnapshotInfo

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


def _snapshot_from_row(row: tuple) -> SnapshotInfo:
    snapshot_id, source, fetched_at, content_hash, path = row
    return SnapshotInfo(
        snapshot_id, source, datetime.datetime.fromisoformat(fetched_at), content_hash, Path(path)
    )
