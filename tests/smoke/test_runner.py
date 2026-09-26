import subprocess
import zipfile
from pathlib import Path

import pytest

from giml.core.config import DatabaseSettings, ReadySettings, SmokeSettings
from giml.smoke.postgres import Unavailable
from giml.smoke.runner import SmokeRunner, classify_startup, find_artifact, redact, resolve_placeholders

DB = DatabaseSettings("PG_HOST", "PG_PORT", "PG_DB", "PG_USER", "PG_PASS")
ENVIRON = {"PATH": "/usr/bin", "JAVA_HOME": "/jdk", "HOME": "/home/dev", "GITHUB_TOKEN": "dev-secret-token",
           "PG_HOST": "127.0.0.1", "PG_PORT": "5432", "PG_DB": "scratch", "PG_USER": "u", "PG_PASS": "pw-1234"}  # fmt: skip


class FakeProcess:
    """A process that dies at once with ``code`` (or stays up when ``code`` is None); records the group kills."""

    pid = 424242

    def __init__(self, code):
        self.code, self.waited = code, 0

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        self.waited += 1
        return self.code or 0


class Launcher:
    def __init__(self, code=0, output="", per_attempt=None):
        self.code, self.output, self.per_attempt, self.calls = code, output, per_attempt, []

    def __call__(self, argv, cwd, env, log):
        self.calls.append((list(argv), cwd, dict(env)))
        text = self.per_attempt[len(self.calls) - 1] if self.per_attempt else self.output
        with log.open("ab") as handle:
            handle.write(text.encode())
        return FakeProcess(self.code if not self.per_attempt else (0 if len(self.calls) == len(self.per_attempt) else 1))


@pytest.fixture(autouse=True)
def no_real_kill(monkeypatch):
    monkeypatch.setattr("os.killpg", lambda pid, sig: None)


def jar(tmp_path, name="app.jar", main=True):
    path = tmp_path / "target" / name
    path.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n" + ("Main-Class: x.Main\n" if main else ""))
    return path


def runner(tmp_path, launcher, settings=None, **kw):
    settings = settings or SmokeSettings("smoke", ready=ReadySettings(http="/health", contains="UP", timeout_seconds=1))
    return SmokeRunner(settings, ENVIRON, tmp_path / "logs", "redkite-c282ac5d", "run-1", launch=launcher, sleep=lambda s: None, **kw)


def test_the_single_executable_jar_in_target_is_the_artifact(tmp_path):
    app = jar(tmp_path)
    jar(tmp_path, "app-sources.jar")
    jar(tmp_path, "original-app.jar")
    jar(tmp_path, "lib.jar", main=False)
    assert find_artifact(tmp_path, None) == app
    assert find_artifact(tmp_path, "target/app.jar") == app and find_artifact(tmp_path, "target/none.jar") is None


def test_two_executable_jars_are_ambiguous_and_none_is_missing(tmp_path):
    assert find_artifact(tmp_path, None) is None
    jar(tmp_path, "a.jar")
    jar(tmp_path, "b.jar")
    assert find_artifact(tmp_path, None) is None


def test_placeholders_are_filled_and_secrets_redacted():
    assert resolve_placeholders({"URL": "jdbc://${DB_HOST}:${DB_PORT}/${TRIAL_SCHEMA}"}, {"DB_HOST": "h", "DB_PORT": "1", "TRIAL_SCHEMA": "s"}) == {"URL": "jdbc://h:1/s"}
    assert redact("password=pw-1234 and pw-1234", ["pw-1234", ""]) == "password=*** and ***"


def test_a_ready_web_application_passes_and_is_started_the_documented_way(tmp_path):
    jar(tmp_path)
    launcher = Launcher(output="2026 WARN slow thing\nStarted App in 1.2 seconds\n")
    out = runner(tmp_path, launcher, http_get=lambda url, timeout: (200, '{"status":"UP"}')).run(tmp_path)
    (argv, cwd, env), = launcher.calls
    assert out.passed and out.failure is None and out.stage == "startup"
    assert argv[1:3] == ["-jar", str(tmp_path / "target" / "app.jar")] and "--spring.profiles.active=smoke" in argv
    assert "--server.address=127.0.0.1" in argv and any(a.startswith("--server.port=") for a in argv)
    assert out.details == {"log_problems": ["2026 WARN slow thing"]}


def test_the_application_gets_a_minimal_environment_and_its_own_home_not_the_developers(tmp_path):
    jar(tmp_path)
    launcher = Launcher()
    runner(tmp_path, launcher, http_get=lambda url, timeout: (200, "UP")).run(tmp_path)
    env = launcher.calls[0][2]
    assert "GITHUB_TOKEN" not in env and "PG_PASS" not in env and env["HOME"] != "/home/dev" and env["JAVA_HOME"] == "/jdk"


def test_an_application_that_exits_before_it_is_ready_fails_as_startup_with_its_own_diagnosis(tmp_path):
    jar(tmp_path)
    log = "APPLICATION FAILED TO START\nDescription:\nFailed to bind to port\nCaused by: java.net.SocketException: boom\n"
    out = runner(tmp_path, Launcher(code=1, output=log)).run(tmp_path)
    assert not out.passed and out.failure.failure_class == "startup" and "exited with code 1" in out.failure.key_lines[0]
    assert any("Caused by" in line for line in out.failure.key_lines)


def test_a_flyway_error_is_a_migration_failure_not_a_startup_failure(tmp_path):
    jar(tmp_path)
    log = "Caused by: org.flywaydb.core.api.FlywayException: Validate failed: Migration checksum mismatch for migration version 2\n"
    out = runner(tmp_path, Launcher(code=1, output=log)).run(tmp_path)
    assert out.failure.failure_class == "migration"


def test_an_application_that_never_becomes_ready_times_out(tmp_path):
    jar(tmp_path)
    ticks = iter(range(0, 1000))
    clock_patch = lambda: next(ticks)  # noqa: E731
    import giml.smoke.runner as module

    original = module.time.monotonic
    module.time.monotonic = clock_patch
    try:
        out = runner(tmp_path, Launcher(code=None), http_get=lambda url, timeout: (503, "")).run(tmp_path)
    finally:
        module.time.monotonic = original
    assert not out.passed and "not ready within 1 seconds" in out.failure.key_lines[0] and out.failure.failure_class == "startup"


def test_a_clean_exit_without_a_port_counts_as_a_started_non_web_application(tmp_path):
    jar(tmp_path)
    assert runner(tmp_path, Launcher(code=0)).run(tmp_path).passed


def test_a_missing_artifact_is_a_startup_failure(tmp_path):
    out = runner(tmp_path, Launcher()).run(tmp_path)
    assert not out.passed and "no packaged executable jar" in out.failure.key_lines[0]


def test_a_taken_port_is_retried_with_another(tmp_path):
    jar(tmp_path)
    launcher = Launcher(per_attempt=["Port 8080 was already in use\n", "Started App in 1 seconds\n"])
    out = runner(tmp_path, launcher, http_get=lambda url, timeout: (200, "UP")).run(tmp_path)
    assert out.passed and len(launcher.calls) == 2


class FakeLifecycle:
    def __init__(self, fail_create=False):
        self.events, self.fail_create = [], fail_create

    def create(self, schema):
        self.events.append(("create", schema))
        if self.fail_create:
            raise Unavailable("psql failed: connection refused")

    def drop(self, schema):
        self.events.append(("drop", schema))


def with_db(**kw):
    return SmokeSettings("smoke", env={"URL": "jdbc:postgresql://${DB_HOST}:${DB_PORT}/${DB_NAME}?currentSchema=${TRIAL_SCHEMA}", "PW": "${DB_PASSWORD}"},
                         ready=ReadySettings(http="/h", timeout_seconds=1), database=DB)  # fmt: skip


def test_the_schema_is_created_before_and_dropped_after_and_the_password_never_reaches_the_log(tmp_path):
    jar(tmp_path)
    lifecycle = FakeLifecycle()
    launcher = Launcher(output="datasource password=pw-1234 ready\n")
    out = runner(tmp_path, launcher, with_db(), lifecycle_factory=lambda c: lifecycle, http_get=lambda u, t: (200, "")).run(tmp_path, trial=2)
    schema = "giml_c282ac5d_run1_2"
    assert out.passed and lifecycle.events == [("create", schema), ("drop", schema)]
    env = launcher.calls[0][2]
    assert env["URL"] == f"jdbc:postgresql://127.0.0.1:5432/scratch?currentSchema={schema}" and env["PW"] == "pw-1234"
    assert "pw-1234" not in out.log_path.read_text() and "***" in out.log_path.read_text()


def test_the_schema_is_dropped_even_when_the_application_fails(tmp_path):
    jar(tmp_path)
    lifecycle = FakeLifecycle()
    out = runner(tmp_path, Launcher(code=1), with_db(), lifecycle_factory=lambda c: lifecycle).run(tmp_path)
    assert not out.passed and lifecycle.events[-1][0] == "drop"


def test_an_unavailable_database_is_reported_as_such_and_never_blames_the_candidate(tmp_path):
    jar(tmp_path)
    launcher = Launcher()
    out = runner(tmp_path, launcher, with_db(), lifecycle_factory=lambda c: FakeLifecycle(fail_create=True)).run(tmp_path)
    assert out.failure.failure_class == "unavailable" and out.retryable and launcher.calls == []
    unset = SmokeRunner(with_db(), {"PATH": "/x"}, tmp_path / "logs", "k-c282ac5d", "r", launch=launcher).run(tmp_path)
    assert unset.failure.failure_class == "unavailable" and "PG_HOST" in unset.failure.key_lines[0]


def test_startup_classification_reads_the_logs_own_key_lines():
    failure = classify_startup("noise\nCaused by: java.lang.IllegalStateException: x at 12:30:01\nmore noise\n", "did not start")
    assert failure.failure_class == "startup" and failure.key_lines[0] == "did not start" and len(failure.signature) == 16
    same = classify_startup("other noise\nCaused by: java.lang.IllegalStateException: x at 09:15:59\n", "did not start")
    assert same.signature == failure.signature  # times and noise do not change the signature
