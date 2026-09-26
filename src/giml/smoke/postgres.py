"""A fresh PostgreSQL schema per startup trial (spec section 12.3).

The application under test gets its own schema, created before it boots and dropped afterwards in a
``finally`` path, so its migrations (Flyway, Liquibase) always start from nothing. Everything goes through the
``psql`` client. Connection details reach it only through PG* environment variables, so a password never
appears in an argument list, a log or a report, and no value from the repository can become an argument. The
SQL is built here from names that are checked against a strict pattern; no SQL ever comes from the project.

Anything that goes wrong talking to the server (unset variable, unreachable server, refused role) is
``Unavailable``: it says nothing about the candidate under test (spec 12.3).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from giml.core.config import DatabaseSettings

SCHEMA_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")  # a PostgreSQL identifier is at most 63 bytes
_PROJECT_HASH = re.compile(r"[0-9a-f]{8}$")

Psql = Callable[[str, Mapping[str, str]], subprocess.CompletedProcess]


class Unavailable(RuntimeError):
    """The database cannot be used for this run; the message names the problem without any secret value."""


@dataclass(frozen=True)
class Connection:
    """What the application and psql need to reach the server. ``password`` is never printed (repr is hidden)."""

    host: str
    port: str
    database: str
    user: str
    password: str = ""

    def __repr__(self) -> str:
        return f"Connection(host={self.host!r}, port={self.port!r}, database={self.database!r}, user={self.user!r}, password=***)"

    def psql_env(self, base: Mapping[str, str]) -> dict[str, str]:
        return {**{k: v for k, v in base.items() if k in ("PATH", "HOME", "LANG", "LC_ALL")}, "PGHOST": self.host, "PGPORT": self.port,
                "PGDATABASE": self.database, "PGUSER": self.user, "PGPASSWORD": self.password, "PGCONNECT_TIMEOUT": "10"}  # fmt: skip


def read_connection(settings: DatabaseSettings, environ: Mapping[str, str]) -> Connection:
    """The connection from the runner's environment; names the variables that are unset, never their values."""
    names = {"host": settings.host_env, "port": settings.port_env, "database": settings.name_env, "user": settings.user_env,
             "password": settings.password_env}  # fmt: skip
    missing = sorted(name for name in names.values() if not environ.get(name))
    if missing:
        raise Unavailable(f"environment variable(s) not set: {', '.join(missing)}")
    if not environ[settings.port_env].isdigit():
        raise Unavailable(f"{settings.port_env} is not a port number")
    return Connection(**{field: environ[name] for field, name in names.items()})


def run_psql(sql: str, env: Mapping[str, str]) -> subprocess.CompletedProcess:
    psql = shutil.which("psql", path=env.get("PATH", os.defpath))
    if psql is None:
        raise Unavailable("the psql client is not on PATH")
    return subprocess.run([psql, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-t", "-A", "-c", sql], capture_output=True, text=True,
                          timeout=60, check=False, env=dict(env))  # fmt: skip


def trial_schema(settings: DatabaseSettings, project_key: str, run_id: str, trial: int) -> str:
    """``<prefix>_<project hash>_<run>_<trial>``: only this project's runs share a prefix, so a sweep never touches another's."""
    match = _PROJECT_HASH.search(project_key)
    project = match.group() if match else re.sub(r"[^0-9a-f]", "", project_key.lower())[:8] or "0"
    run = re.sub(r"[^0-9a-z]", "", run_id.lower())
    name = f"{settings.schema_prefix}_{project}_{run}_{trial}"[:63]
    if not SCHEMA_NAME.fullmatch(name):
        raise ValueError(f"cannot build a safe schema name from {project_key!r} and {run_id!r}")
    return name


class SchemaLifecycle:
    def __init__(self, settings: DatabaseSettings, connection: Connection, environ: Mapping[str, str], psql: Psql = run_psql) -> None:
        self.settings, self.connection, self.psql = settings, connection, psql
        self._env = connection.psql_env(environ)

    def _run(self, sql: str) -> str:
        try:
            result = self.psql(sql, self._env)
        except subprocess.TimeoutExpired as exc:
            raise Unavailable("psql timed out") from exc
        if result.returncode != 0:
            first = (result.stderr or "").strip().splitlines()[:1]
            detail = first[0].replace(self.connection.password, "***") if first and self.connection.password else (first[0] if first else "")
            raise Unavailable(f"psql failed: {detail}")
        return result.stdout

    @staticmethod
    def _checked(schema: str) -> str:
        if not SCHEMA_NAME.fullmatch(schema):
            raise ValueError(f"refusing to use {schema!r} as a schema name")
        return schema

    def create(self, schema: str) -> None:
        name = self._checked(schema)
        self._run(f'DROP SCHEMA IF EXISTS "{name}" CASCADE; CREATE SCHEMA "{name}";')

    def drop(self, schema: str) -> None:
        self._run(f'DROP SCHEMA IF EXISTS "{self._checked(schema)}" CASCADE;')

    def sweep(self, project_key: str, keep: str | None = None) -> list[str]:
        """Drop this project's schemas left behind by earlier runs (crashes); returns what was dropped."""
        prefix = trial_schema(self.settings, project_key, "x", 0).rsplit("_", 2)[0] + "_"
        listing = self._run("SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE "
                            f"'{prefix.replace('_', chr(92) + '_')}%' ESCAPE '\\';")  # fmt: skip
        dropped = []
        for line in listing.splitlines():
            name = line.strip()
            if name.startswith(prefix) and SCHEMA_NAME.fullmatch(name) and name != keep:
                self.drop(name)
                dropped.append(name)
        return dropped
