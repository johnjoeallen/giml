import subprocess

import pytest

from giml.core.config import DatabaseSettings
from giml.smoke.postgres import Connection, SchemaLifecycle, Unavailable, read_connection, trial_schema

SETTINGS = DatabaseSettings("PG_HOST", "PG_PORT", "PG_DB", "PG_USER", "PG_PASS")
ENVIRON = {"PG_HOST": "127.0.0.1", "PG_PORT": "5432", "PG_DB": "giml_scratch", "PG_USER": "giml", "PG_PASS": "s3cret-pw", "PATH": "/usr/bin"}
KEY = "redkite-c282ac5d"


class FakePsql:
    def __init__(self, listing="", code=0, stderr=""):
        self.calls, self.listing, self.code, self.stderr = [], listing, code, stderr

    def __call__(self, sql, env):
        self.calls.append((sql, dict(env)))
        return subprocess.CompletedProcess(["psql"], self.code, self.listing, self.stderr)


def lifecycle(psql):
    return SchemaLifecycle(SETTINGS, read_connection(SETTINGS, ENVIRON), ENVIRON, psql)


def test_the_connection_comes_from_the_environment_and_never_prints_the_password():
    connection = read_connection(SETTINGS, ENVIRON)
    assert (connection.host, connection.port, connection.database, connection.user) == ("127.0.0.1", "5432", "giml_scratch", "giml")
    assert "s3cret-pw" not in repr(connection) and "s3cret-pw" not in str(connection)


def test_unset_variables_are_named_but_no_value_is_ever_mentioned():
    with pytest.raises(Unavailable) as error:
        read_connection(SETTINGS, {"PG_HOST": "h", "PG_PASS": "s3cret-pw"})
    assert "PG_DB, PG_PORT, PG_USER" in str(error.value) and "s3cret-pw" not in str(error.value)
    with pytest.raises(Unavailable, match="PG_PORT is not a port number"):
        read_connection(SETTINGS, {**ENVIRON, "PG_PORT": "abc"})


def test_the_schema_name_is_built_from_the_project_hash_run_and_trial_and_is_a_safe_identifier():
    name = trial_schema(DatabaseSettings("A", "B", "C", "D", "E", "giml"), KEY, "20260926T125050Z-1b5895", 3)
    assert name == "giml_c282ac5d_20260926t125050z1b5895_3"
    assert len(trial_schema(SETTINGS, KEY, "r" * 200, 1)) <= 63
    assert trial_schema(SETTINGS, "odd; DROP--", "run", 1).startswith("giml_d")  # hex characters of the key only, still safe


def test_create_and_drop_send_fixed_sql_with_the_connection_only_in_the_environment():
    psql = FakePsql()
    schema = "giml_c282ac5d_run_1"
    lifecycle(psql).create(schema)
    lifecycle(psql).drop(schema)
    (create_sql, env), (drop_sql, _) = psql.calls
    assert create_sql == 'DROP SCHEMA IF EXISTS "giml_c282ac5d_run_1" CASCADE; CREATE SCHEMA "giml_c282ac5d_run_1";'
    assert drop_sql == 'DROP SCHEMA IF EXISTS "giml_c282ac5d_run_1" CASCADE;'
    assert env["PGPASSWORD"] == "s3cret-pw" and env["PGHOST"] == "127.0.0.1" and env["PGDATABASE"] == "giml_scratch"
    assert "s3cret-pw" not in create_sql + drop_sql


@pytest.mark.parametrize("bad", ['x"; DROP SCHEMA public; --', "Upper", "a b", "", "1abc", "a" * 64])
def test_a_name_that_is_not_a_plain_identifier_is_refused_before_any_sql_is_sent(bad):
    psql = FakePsql()
    with pytest.raises(ValueError, match="refusing to use"):
        lifecycle(psql).create(bad)
    with pytest.raises(ValueError):
        lifecycle(psql).drop(bad)
    assert psql.calls == []


def test_a_failing_psql_is_unavailable_and_its_message_hides_the_password():
    psql = FakePsql(code=2, stderr='psql: error: connection to server failed: password "s3cret-pw" rejected\nmore')
    with pytest.raises(Unavailable) as error:
        lifecycle(psql).create("giml_c282ac5d_run_1")
    assert "s3cret-pw" not in str(error.value) and "psql failed" in str(error.value)


def test_a_timeout_is_unavailable():
    def slow(sql, env):
        raise subprocess.TimeoutExpired("psql", 60)

    with pytest.raises(Unavailable, match="timed out"):
        lifecycle(slow).drop("giml_c282ac5d_run_1")


def test_the_sweep_drops_only_this_projects_leftovers_and_keeps_the_named_schema():
    listing = "giml_c282ac5d_old_1\ngiml_c282ac5d_old_2\ngiml_c282ac5d_current_1\ngiml_deadbeef_other_1\n"
    psql = FakePsql(listing)
    dropped = lifecycle(psql).sweep(KEY, keep="giml_c282ac5d_current_1")
    assert dropped == ["giml_c282ac5d_old_1", "giml_c282ac5d_old_2"]  # another project's schema is never touched
    assert "LIKE 'giml\\_c282ac5d\\_%' ESCAPE" in psql.calls[0][0]
    assert [c[0] for c in psql.calls[1:]] == ['DROP SCHEMA IF EXISTS "giml_c282ac5d_old_1" CASCADE;', 'DROP SCHEMA IF EXISTS "giml_c282ac5d_old_2" CASCADE;']
