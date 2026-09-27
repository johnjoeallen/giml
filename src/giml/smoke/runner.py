"""Boot the packaged application and see whether it comes up (spec section 12.2).

The packaged jar is run with ``java -jar`` (never an IDE-style classpath) under the project's own smoke profile,
bound to 127.0.0.1 on a free port, in an ephemeral working directory and home. The application gets a minimal
environment (PATH, JAVA_HOME, temp settings) plus what the settings list, with placeholders filled in; the
developer's other environment variables, secrets included, are not passed on. Its whole process tree is killed
in a ``finally`` path, its output goes to a log with secret values removed, and how it ended is classified:
``migration`` when Flyway or Liquibase failed, ``startup`` for any other failure to come up, ``unavailable``
when the database could not be provided (that says nothing about the candidate).
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path

from giml.core.config import PLACEHOLDER, SmokeSettings
from giml.core.platform import popen_in_new_group, terminate_process_tree
from giml.maven.build import StageOutcome
from giml.maven.failures import MIGRATION, STARTUP, UNAVAILABLE, Failure, failure_from_lines, normalise
from giml.smoke.postgres import Connection, SchemaLifecycle, Unavailable, read_connection, trial_schema

_KEPT_ENV = ("PATH", "JAVA_HOME", "LANG", "LC_ALL", "TMPDIR", "JAVA_TOOL_OPTIONS")
_MIGRATION = re.compile(r"org\.flywaydb|FlywayException|Flyway\S* .*(?:failed|error)|liquibase\.exception|LiquibaseException|Migration .* failed", re.I)
_KEY_LINE = re.compile(r"APPLICATION FAILED TO START|Caused by:|Exception|\bERROR\b|Description:|Action:|Port \d+ was already in use", re.I)
_STARTED = re.compile(r"\bStarted \S+ in [\d.]+ seconds")
_PORT_IN_USE = re.compile(r"Port \d+ was already in use|Address already in use|BindException", re.I)
_KILL_GRACE_SECONDS = 5
_LAUNCH_ATTEMPTS = 3

Launch = Callable[[Sequence[str], Path, Mapping[str, str], Path], subprocess.Popen]


def default_launch(argv: Sequence[str], cwd: Path, env: Mapping[str, str], log: Path) -> subprocess.Popen:
    """Start the application in its own group (so its whole process tree can be killed), output to the log."""
    handle = log.open("ab")
    try:
        return popen_in_new_group(list(argv), cwd=cwd, env=dict(env), stdout=handle, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL)  # fmt: skip
    finally:
        handle.close()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def find_artifact(project_dir: Path, configured: str | None) -> Path | None:
    """The configured jar (a path or a glob that matches exactly one file), or the single executable jar in target/
    (one with a Main-Class; sources, tests and originals skipped)."""
    if configured is not None:
        matches = [p for p in sorted(project_dir.glob(configured)) if p.is_file()]
        return matches[0] if len(matches) == 1 else None
    candidates = [p for p in sorted((project_dir / "target").glob("*.jar"))
                  if not p.name.endswith(("-sources.jar", "-tests.jar", "-javadoc.jar")) and not p.name.startswith("original-")]  # fmt: skip
    runnable = [p for p in candidates if _has_main_class(p)]
    return runnable[0] if len(runnable) == 1 else None


def is_spring_boot(jar: Path) -> bool:
    """A Spring Boot jar carries Spring-Boot-Version or Start-Class in its manifest; only such an application is given Spring arguments."""
    try:
        with zipfile.ZipFile(jar) as archive:
            manifest = archive.read("META-INF/MANIFEST.MF")
    except (OSError, KeyError, zipfile.BadZipFile):
        return False
    return b"Spring-Boot-Version" in manifest or b"Start-Class" in manifest


def _has_main_class(jar: Path) -> bool:
    try:
        with zipfile.ZipFile(jar) as archive:
            return b"Main-Class" in archive.read("META-INF/MANIFEST.MF")
    except (OSError, KeyError, zipfile.BadZipFile):
        return False


def resolve_placeholders(env: Mapping[str, str], values: Mapping[str, str]) -> dict[str, str]:
    return {name: PLACEHOLDER.sub(lambda m: values[m.group(1)], value) for name, value in env.items()}


def redact(text: str, secrets: Sequence[str]) -> str:
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def classify_startup(log_text: str, why: str) -> Failure:
    """``migration`` when a migration tool's error is in the log, else ``startup``; the key lines are the log's own diagnosis."""
    failure_class = MIGRATION if _MIGRATION.search(log_text) else STARTUP
    lines = [line for line in log_text.splitlines() if _KEY_LINE.search(line)]
    return failure_from_lines(failure_class, [why, *lines[:12]])


@contextlib.contextmanager
def _kill_tree(process: subprocess.Popen) -> Iterator[None]:
    """Whatever happens, the application's whole process tree is gone afterwards."""
    try:
        yield
    finally:
        terminate_process_tree(process, _KILL_GRACE_SECONDS)


class SmokeRunner:
    def __init__(self, settings: SmokeSettings, environ: Mapping[str, str], logs_dir: Path, project_key: str, run_id: str,
                 *, launch: Launch = default_launch, lifecycle_factory=None, sleep: Callable[[float], None] = time.sleep,
                 http_get=None) -> None:  # fmt: skip
        self.settings, self.environ, self.logs_dir = settings, environ, logs_dir
        self.project_key, self.run_id, self.launch, self.sleep = project_key, run_id, launch, sleep
        self.lifecycle = lifecycle_factory or (lambda connection: SchemaLifecycle(settings.database, connection, environ))
        self._http_get = http_get or self._get
        self._runs = 0

    @staticmethod
    def _get(url: str, timeout: float) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:  # loopback only: the URL is built from 127.0.0.1
                return response.status, response.read(65536).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return exc.code, ""
        except (urllib.error.URLError, OSError):
            return 0, ""

    def _outcome(self, passed: bool, started: float, log: Path, failure: Failure | None, details: dict | None = None) -> StageOutcome:
        return StageOutcome("startup", passed, time.monotonic() - started, log, failure, details=details)

    def run(self, worktree: Path, trial: int = 0) -> StageOutcome:
        """Boot the packaged application of ``worktree`` once; the outcome's details carry the WARN/ERROR lines seen."""
        self._runs += 1
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log = self.logs_dir / f"{self._runs:02d}-startup.log"
        log.write_bytes(b"")
        started = time.monotonic()
        artifact = find_artifact(worktree, self.settings.artifact)
        if artifact is None:
            return self._outcome(False, started, log, failure_from_lines(STARTUP, ["no packaged executable jar was found to start"]))
        connection: Connection | None = None
        lifecycle: SchemaLifecycle | None = None
        schema = trial_schema(self.settings.database, self.project_key, self.run_id, trial) if self.settings.database else ""
        try:
            if self.settings.database is not None:
                connection = read_connection(self.settings.database, self.environ)
                lifecycle = self.lifecycle(connection)
                lifecycle.create(schema)
            return self._boot(artifact, worktree, log, started, connection, schema)
        except Unavailable as exc:
            return self._outcome(False, started, log, failure_from_lines(UNAVAILABLE, [f"the database is unavailable: {exc}"]))
        finally:
            if lifecycle is not None:
                with contextlib.suppress(Unavailable):
                    lifecycle.drop(schema)

    def _boot(self, artifact: Path, worktree: Path, log: Path, started: float, connection: Connection | None, schema: str) -> StageOutcome:
        secrets = [connection.password] if connection else []
        for attempt in range(1, _LAUNCH_ATTEMPTS + 1):
            with tempfile.TemporaryDirectory(prefix="giml-smoke-") as scratch:
                port = free_port()
                values = {"PORT": str(port), "TRIAL_SCHEMA": schema}
                if connection is not None:
                    values |= {"DB_HOST": connection.host, "DB_PORT": connection.port, "DB_NAME": connection.database,
                               "DB_USER": connection.user, "DB_PASSWORD": connection.password}  # fmt: skip
                env = {k: v for k, v in self.environ.items() if k in _KEPT_ENV} | {"HOME": scratch}
                env |= resolve_placeholders(self.settings.env or {}, values)
                java = shutil.which("java", path=env.get("PATH", os.defpath)) or "java"
                properties = [f"-D{name}={value}" for name, value in resolve_placeholders(self.settings.properties or {}, values).items()]
                # A JVM takes user.home from the password database, not from $HOME, so without this the application under test
                # would read and write the developer's real home (found on redkite, which opened its real ~/.redkite database).
                argv = [java, f"-Duser.home={scratch}", *properties, "-jar", str(artifact)]
                if is_spring_boot(artifact):  # Spring's own conventions; any other application is configured through properties and env
                    argv += ([f"--spring.profiles.active={self.settings.profile}"] if self.settings.profile else [])
                    argv += [f"--server.port={port}", "--server.address=127.0.0.1"]
                process = self.launch(argv, Path(scratch), env, log)
                with _kill_tree(process):
                    ready, why = self._await_ready(process, port, log, secrets)
                text = redact(log.read_text(encoding="utf-8", errors="replace"), secrets)
                log.write_text(text, encoding="utf-8")
            if not ready and _PORT_IN_USE.search(text) and attempt < _LAUNCH_ATTEMPTS:
                continue  # someone took the port between choosing it and binding it
            warnings = [normalise(line) for line in text.splitlines() if re.search(r"\b(WARN|ERROR)\b", line)]
            details = {"log_problems": sorted(set(warnings))[:50]}
            return self._outcome(ready, started, log, None if ready else classify_startup(text, why), details)
        raise AssertionError("unreachable")  # pragma: no cover

    def _await_ready(self, process: subprocess.Popen, port: int, log: Path, secrets: Sequence[str]) -> tuple[bool, str]:
        ready = self.settings.ready
        deadline = time.monotonic() + ready.timeout_seconds
        while time.monotonic() < deadline:
            code = process.poll()
            text = log.read_text(encoding="utf-8", errors="replace")
            if code is not None:
                return (code == 0, "" if code == 0 else f"the application exited with code {code} before it was ready")
            if ready.http is not None:
                status, body = self._http_get(f"http://127.0.0.1:{port}{ready.http}", 2.0)
                if status == 200 and (ready.contains is None or ready.contains in body):
                    return True, ""
            elif _STARTED.search(text) and self._connects(port):
                return True, ""
            self.sleep(0.5)
        return False, f"the application was not ready within {ready.timeout_seconds} seconds"

    @staticmethod
    def _connects(port: int) -> bool:
        with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
        return False
