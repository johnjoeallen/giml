"""The smoke runner with a real JVM and real process trees (slow). Run with `pytest -m slow`."""

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from giml.core.config import ReadySettings, SmokeSettings
from giml.smoke.runner import SmokeRunner

pytestmark = pytest.mark.slow

MAIN = r"""
import com.sun.net.httpserver.HttpServer;
import java.io.*;
import java.net.InetSocketAddress;
import java.nio.file.*;

public class Demo {
    public static void main(String[] args) throws Exception {
        int port = Integer.parseInt(System.getProperty("demo.port", "0"));
        String mode = System.getenv().getOrDefault("DEMO_MODE", "up");
        System.out.println("args=" + String.join(" ", args) + " demo.port=" + port);
        System.out.println("secret-in-log=" + System.getenv().getOrDefault("DEMO_PASSWORD", "none"));
        System.out.println("developer-token=" + System.getenv("GITHUB_TOKEN"));
        System.out.println("user.home=" + System.getProperty("user.home"));
        if (mode.equals("write-home")) {
            Files.createDirectories(Path.of(System.getProperty("user.home"), ".giml-demo"));
            Files.writeString(Path.of(System.getProperty("user.home"), ".giml-demo", "state.db"), "written by the application under test");
        }
        String pidFile = System.getenv("DEMO_PIDFILE");
        if (pidFile != null) {
            Process child = new ProcessBuilder("sleep", "300").start();  // a grandchild the runner must also kill
            Files.writeString(Path.of(pidFile), Long.toString(child.pid()));
        }
        if (mode.equals("crash")) {
            System.out.println("APPLICATION FAILED TO START");
            System.out.println("Caused by: org.flywaydb.core.api.FlywayException: Validate failed");
            System.exit(3);
        }
        if (mode.equals("hang")) { Thread.sleep(600000); }
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 0);
        server.createContext("/health", ex -> {
            byte[] body = "{\"status\":\"UP\"}".getBytes();
            ex.sendResponseHeaders(200, body.length);
            ex.getResponseBody().write(body);
            ex.close();
        });
        server.start();
        System.out.println("Started Demo in 0.2 seconds");
        Thread.sleep(600000);
    }
}
"""


@pytest.fixture(scope="module")
def demo_jar(tmp_path_factory):
    if shutil.which("javac") is None or shutil.which("jar") is None:
        pytest.skip("a JDK is required")
    work = tmp_path_factory.mktemp("demo")
    (work / "Demo.java").write_text(MAIN)
    subprocess.run(["javac", "-d", str(work / "out"), str(work / "Demo.java")], check=True, capture_output=True)
    target = work / "project" / "target"
    target.mkdir(parents=True)
    subprocess.run(["jar", "cfe", str(target / "demo.jar"), "Demo", "-C", str(work / "out"), "."], check=True)
    return work / "project"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"


def run(tmp_path, project, env, timeout=20, **kw):
    settings = SmokeSettings("smoke", properties={"demo.port": "${PORT}"}, env=env, ready=ReadySettings(http="/health", contains="UP", timeout_seconds=timeout))
    environ = {"PATH": os.environ["PATH"], "GITHUB_TOKEN": "dev-secret-token", **kw.pop("environ", {})}
    return SmokeRunner(settings, environ, tmp_path / "logs", "demo-c282ac5d", "run-1", **kw).run(project)


def test_a_real_application_boots_answers_health_and_the_developers_token_stays_out(tmp_path, demo_jar):
    out = run(tmp_path, demo_jar, {"DEMO_MODE": "up"})
    log = out.log_path.read_text()
    assert out.passed, log
    assert "args= demo.port=" in log and "demo.port=0" not in log  # a plain jar gets no Spring arguments, and the port through a property
    assert "developer-token=null" in log and "dev-secret-token" not in log


def test_a_real_crash_is_a_migration_failure_with_the_applications_own_lines(tmp_path, demo_jar):
    out = run(tmp_path, demo_jar, {"DEMO_MODE": "crash"})
    assert not out.passed and out.failure.failure_class == "migration"
    assert any("FlywayException" in line for line in out.failure.key_lines)


def test_a_hung_application_times_out_and_its_whole_process_tree_is_killed(tmp_path, demo_jar):
    pidfile = tmp_path / "child.pid"
    started = time.monotonic()
    out = run(tmp_path, demo_jar, {"DEMO_MODE": "hang", "DEMO_PIDFILE": str(pidfile)}, timeout=4)
    assert not out.passed and "not ready within 4 seconds" in out.failure.key_lines[0]
    assert time.monotonic() - started < 20
    child = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while alive(child) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not alive(child)  # the grandchild died with the group
    with pytest.raises(ProcessLookupError):
        os.kill(child, signal.SIGKILL)


def test_the_database_password_is_passed_to_the_application_and_removed_from_its_log(tmp_path, demo_jar):
    from giml.core.config import DatabaseSettings

    class NoopLifecycle:
        def create(self, schema): ...
        def drop(self, schema): ...

    database = DatabaseSettings("H", "P", "D", "U", "W")
    settings = SmokeSettings("smoke", properties={"demo.port": "${PORT}"}, env={"DEMO_PASSWORD": "${DB_PASSWORD}"},
                             ready=ReadySettings(http="/health", timeout_seconds=20), database=database)
    environ = {"PATH": os.environ["PATH"], "H": "127.0.0.1", "P": "5432", "D": "d", "U": "u", "W": "hunter2-pw"}
    out = SmokeRunner(settings, environ, tmp_path / "logs", "demo-c282ac5d", "run-1", lifecycle_factory=lambda c: NoopLifecycle()).run(demo_jar)
    log = out.log_path.read_text()
    assert out.passed and "hunter2-pw" not in log and "secret-in-log=***" in log


def test_an_application_that_writes_to_its_home_writes_to_the_ephemeral_one_never_the_developers(tmp_path, demo_jar):
    real_home = Path(os.path.expanduser("~"))
    marker = real_home / ".giml-demo"
    assert not marker.exists()
    out = run(tmp_path, demo_jar, {"DEMO_MODE": "write-home"})
    log = out.log_path.read_text()
    assert out.passed, log
    assert "user.home=" + str(real_home) not in log and "user.home=/" in log
    assert not marker.exists()  # the developer's home was not touched
