"""The schema lifecycle against a real PostgreSQL in a throwaway container (slow). Run with `pytest -m slow`.

Needs docker and psql. The container is created here, bound to 127.0.0.1 on a random port, and removed in the
fixture's teardown; nothing else on the machine is touched.
"""

import os
import secrets
import shutil
import subprocess
import time

import pytest

from giml.core.config import DatabaseSettings
from giml.smoke.postgres import SchemaLifecycle, Unavailable, read_connection, trial_schema

pytestmark = pytest.mark.slow

SETTINGS = DatabaseSettings("PG_HOST", "PG_PORT", "PG_DB", "PG_USER", "PG_PASS")
KEY = "redkite-c282ac5d"


def docker(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=120)


@pytest.fixture(scope="module")
def server():
    if shutil.which("docker") is None or shutil.which("psql") is None:
        pytest.skip("docker and psql are required")
    if docker("image", "inspect", "postgres:17-alpine", check=False).returncode != 0:
        pytest.skip("postgres:17-alpine is not available locally (docker pull postgres:17-alpine)")
    name, password = f"giml-test-pg-{secrets.token_hex(4)}", secrets.token_hex(8)
    docker("run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::5432", "-e", f"POSTGRES_PASSWORD={password}",
           "-e", "POSTGRES_USER=giml", "-e", "POSTGRES_DB=scratch", "postgres:17-alpine")  # fmt: skip
    try:
        port = docker("port", name, "5432/tcp").stdout.split(":")[-1].strip()
        environ = {"PATH": os.environ["PATH"], "PG_HOST": "127.0.0.1", "PG_PORT": port, "PG_DB": "scratch", "PG_USER": "giml", "PG_PASS": password}
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            probe = subprocess.run(["psql", "-X", "-t", "-A", "-c", "SELECT 1"], capture_output=True, text=True, timeout=20,
                                   env={**environ, "PGHOST": "127.0.0.1", "PGPORT": port, "PGUSER": "giml", "PGDATABASE": "scratch", "PGPASSWORD": password})
            if probe.returncode == 0 and probe.stdout.strip() == "1":
                break
            time.sleep(1)
        else:
            pytest.fail("the throwaway PostgreSQL never came up")
        yield environ
    finally:
        docker("rm", "-f", name, check=False)


def lifecycle(server):
    return SchemaLifecycle(SETTINGS, read_connection(SETTINGS, server), server)


def schemas(server) -> set[str]:
    life = lifecycle(server)
    return {line.strip() for line in life._run("SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'giml\\_%' ESCAPE '\\';").splitlines() if line.strip()}


def test_a_schema_is_created_empty_and_dropped_with_its_contents(server):
    life = lifecycle(server)
    name = trial_schema(SETTINGS, KEY, "run-a", 1)
    life.create(name)
    life._run(f'CREATE TABLE "{name}".t (id int); INSERT INTO "{name}".t VALUES (1);')
    assert name in schemas(server)
    life.create(name)  # again: dropped and recreated, so migrations always start from nothing
    assert life._run(f"SELECT count(*) FROM information_schema.tables WHERE table_schema = '{name}';").strip() == "0"
    life.drop(name)
    assert name not in schemas(server)
    life.drop(name)  # dropping a missing schema is not an error


def test_the_sweep_removes_only_this_projects_leftovers(server):
    life = lifecycle(server)
    mine_old, mine_current = trial_schema(SETTINGS, KEY, "old", 1), trial_schema(SETTINGS, KEY, "current", 1)
    other = trial_schema(SETTINGS, "other-deadbeef", "run", 1)
    for name in (mine_old, mine_current, other):
        life.create(name)
    try:
        assert life.sweep(KEY, keep=mine_current) == [mine_old]
        assert {mine_current, other} <= schemas(server) and mine_old not in schemas(server)
    finally:
        life.drop(mine_current)
        life.drop(other)


def test_a_wrong_password_is_unavailable_and_the_password_is_not_in_the_message(server):
    wrong = {**server, "PG_PASS": "definitely-wrong-pw"}
    life = SchemaLifecycle(SETTINGS, read_connection(SETTINGS, wrong), wrong)
    with pytest.raises(Unavailable) as error:
        life.create(trial_schema(SETTINGS, KEY, "x", 1))
    assert "definitely-wrong-pw" not in str(error.value)


def test_an_unreachable_server_is_unavailable(server):
    dead = {**server, "PG_PORT": "1"}
    with pytest.raises(Unavailable):
        SchemaLifecycle(SETTINGS, read_connection(SETTINGS, dead), dead).drop(trial_schema(SETTINGS, KEY, "x", 1))
