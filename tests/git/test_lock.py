import os
import signal
import subprocess
import sys
import time

import pytest

from giml.git.lock import LockHeld, ProjectLock, is_locked


def test_second_holder_is_refused_with_pid(tmp_path):
    with ProjectLock(tmp_path, "proj"):
        assert is_locked(tmp_path, "proj")
        with pytest.raises(LockHeld, match=rf"another giml run \(pid {os.getpid()}\) holds .*proj.lock"):
            ProjectLock(tmp_path, "proj").acquire()
    assert not is_locked(tmp_path, "proj")


def test_locks_are_per_project(tmp_path):
    with ProjectLock(tmp_path, "a"):
        assert not is_locked(tmp_path, "b")


def test_release_is_idempotent_and_lock_can_be_retaken(tmp_path):
    lock = ProjectLock(tmp_path, "proj")
    lock.acquire()
    lock.release()
    lock.release()
    with ProjectLock(tmp_path, "proj"):
        pass


def test_lock_held_by_another_process_is_released_when_it_dies(tmp_path):
    code = (
        "import sys, time; from pathlib import Path; from giml.git.lock import ProjectLock\n"
        "lock = ProjectLock(Path(sys.argv[1]), 'proj'); lock.acquire(); print('locked', flush=True); time.sleep(60)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(LockHeld, match=rf"pid {child.pid}"):
            ProjectLock(tmp_path, "proj").acquire()
    finally:
        child.send_signal(signal.SIGKILL)
        child.wait()
    deadline = time.monotonic() + 5
    while is_locked(tmp_path, "proj") and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not is_locked(tmp_path, "proj")
