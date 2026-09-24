import os
import stat
import subprocess
import time

import pytest

from giml.maven.runner import MavenNotFound, run_maven

FAKE_MVN = """#!/bin/sh
echo "args: $*"
echo "cwd: $(pwd)"
case "$*" in
  *sleep*) sleep 30 & echo $! > "$CHILD_PID"; sleep 30 ;;
  *fail*) echo "BUILD FAILURE"; exit 3 ;;
  *leak*) sleep 30 & echo $! > "$CHILD_PID"; exit 0 ;;
esac
echo "BUILD SUCCESS"
"""


@pytest.fixture
def fake_mvn(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    mvn = bin_dir / "mvn"
    mvn.write_text(FAKE_MVN)
    mvn.chmod(mvn.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return mvn


def test_successful_run_is_logged_with_command(fake_mvn, tmp_path):
    log = tmp_path / "logs" / "test.log"
    result = run_maven(tmp_path, ["test", "-Dx=1"], log, timeout_seconds=10)
    assert result.succeeded and result.exit_code == 0 and not result.timed_out
    assert result.args == ("test", "-Dx=1") and result.log_path == log
    text = log.read_text()
    assert text.startswith(f"$ cd {tmp_path} && {fake_mvn} -B -ntp test -Dx=1\n")
    assert "args: -B -ntp test -Dx=1" in text and f"cwd: {tmp_path}" in text and "BUILD SUCCESS" in text


def test_failure_exit_code_is_returned(fake_mvn, tmp_path):
    result = run_maven(tmp_path, ["fail"], tmp_path / "f.log", timeout_seconds=10)
    assert result.exit_code == 3 and not result.succeeded


def test_timeout_kills_the_whole_process_group(fake_mvn, tmp_path):
    child_pid = tmp_path / "child.pid"
    env = dict(os.environ, CHILD_PID=str(child_pid))
    started = time.monotonic()
    result = run_maven(tmp_path, ["sleep"], tmp_path / "s.log", timeout_seconds=0.5, env=env)
    assert result.timed_out and result.exit_code is None and not result.succeeded
    assert time.monotonic() - started < 15
    assert "timed out after 0.5s; process group killed" in (tmp_path / "s.log").read_text()
    pid = int(child_pid.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.05)
    assert not _alive(pid)  # the backgrounded child died with the group


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A killed child of an exited parent may linger as a zombie until reaped by init.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def test_processes_left_behind_by_a_finished_build_are_killed(fake_mvn, tmp_path):
    child_pid = tmp_path / "child.pid"
    result = run_maven(tmp_path, ["leak"], tmp_path / "l.log", timeout_seconds=10,
                       env=dict(os.environ, CHILD_PID=str(child_pid)))  # fmt: skip
    assert result.succeeded
    pid = int(child_pid.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.05)
    assert not _alive(pid)


def test_missing_maven_is_reported(tmp_path):
    with pytest.raises(MavenNotFound, match="mvn is not on PATH"):
        run_maven(tmp_path, ["test"], tmp_path / "x.log", timeout_seconds=1, env={"PATH": str(tmp_path)})
