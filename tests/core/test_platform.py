"""The Windows/POSIX branches in giml.core.platform.

The POSIX-branch tests run for real (this is what CI's ubuntu/macos jobs exercise). The
Windows-branch tests fake ``msvcrt`` (not importable outside Windows) and force ``_WINDOWS`` on, so
the branch logic is covered everywhere; the real Windows behaviour is only proven by CI on
``windows-latest`` (see .github/workflows/tests.yml), never by this sandbox.
"""

import subprocess
import sys
import types

import pytest

from giml.core import platform


def test_posix_lock_is_exclusive_and_releasable(tmp_path):
    if sys.platform == "win32":
        pytest.skip("posix-only")
    path = tmp_path / "l"
    with path.open("a+b") as handle:
        assert platform.acquire_exclusive_lock(handle)
        platform.release_exclusive_lock(handle)


def test_posix_free_space_reports_something_real(tmp_path):
    if sys.platform == "win32":
        pytest.skip("posix-only")
    space = platform.free_space(tmp_path)
    assert space.bytes > 0


@pytest.fixture
def fake_msvcrt(monkeypatch):
    calls: list[tuple[int, int, int]] = []

    def locking(fd, mode, length):
        calls.append((fd, mode, length))
        if mode == fake.LK_NBLCK and locking.fail:
            raise OSError("locked by another process")

    fake = types.SimpleNamespace(LK_NBLCK=1, LK_UNLCK=0, locking=locking)
    locking.fail = False
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr(platform, "_WINDOWS", True)
    return fake


def test_windows_lock_succeeds_and_leaves_content_readable(fake_msvcrt, tmp_path):
    path = tmp_path / "l"
    with path.open("a+b") as handle:
        handle.write(b"42")
        assert platform.acquire_exclusive_lock(handle) is True
        handle.seek(0)
        assert handle.read() == b"42"  # the lock is on a byte range far past the content
        platform.release_exclusive_lock(handle)


def test_windows_lock_reports_failure(fake_msvcrt, tmp_path):
    fake_msvcrt.locking.fail = True
    path = tmp_path / "l"
    with path.open("a+b") as handle:
        assert platform.acquire_exclusive_lock(handle) is False


def test_no_hooks_path_is_an_empty_reused_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(platform.tempfile, "gettempdir", lambda: str(tmp_path))
    first = platform.no_hooks_path()
    second = platform.no_hooks_path()
    assert first == second == tmp_path / "giml-no-hooks"
    assert first.is_dir()
    assert list(first.iterdir()) == []


def test_windows_free_space_has_no_inode_count(monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "_WINDOWS", True)
    monkeypatch.setattr(platform.shutil, "disk_usage", lambda path: types.SimpleNamespace(free=123))
    assert platform.free_space(tmp_path) == platform.FreeSpace(bytes=123, inodes=None)


def test_windows_popen_uses_a_new_process_group(monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "_WINDOWS", True)
    captured = {}

    def fake_popen(argv, **kwargs):
        captured.update(kwargs)
        return "process"

    monkeypatch.setattr(platform.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(platform.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(platform.subprocess, "CREATE_NO_WINDOW", 0x8000000, raising=False)
    result = platform.popen_in_new_group(["echo", "hi"])
    assert result == "process"
    assert captured["creationflags"] == 0x200 | 0x8000000


def test_posix_popen_uses_a_new_session(monkeypatch):
    if sys.platform == "win32":
        pytest.skip("posix-only")
    captured = {}

    def fake_popen(argv, **kwargs):
        captured.update(kwargs)
        return "process"

    monkeypatch.setattr(platform.subprocess, "Popen", fake_popen)
    platform.popen_in_new_group(["echo", "hi"])
    assert captured["start_new_session"] is True


def test_posix_terminate_process_tree_kills_a_real_process_group():
    if sys.platform == "win32":
        pytest.skip("posix-only")
    process = platform.popen_in_new_group([sys.executable, "-c", "import time; time.sleep(60)"], stdout=subprocess.DEVNULL)
    platform.terminate_process_tree(process, grace_seconds=2)
    assert process.poll() is not None


def test_windows_terminate_process_tree_uses_ctrl_break_then_taskkill(monkeypatch):
    monkeypatch.setattr(platform, "_WINDOWS", True)
    monkeypatch.setattr(platform.signal, "CTRL_BREAK_EVENT", 1, raising=False)
    kills = []
    monkeypatch.setattr(platform.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    run_calls = []
    monkeypatch.setattr(platform.subprocess, "run", lambda *a, **k: run_calls.append(a))

    class FakeProcess:
        pid = 4321

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if timeout is not None:
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

    process = FakeProcess()
    platform.terminate_process_tree(process, grace_seconds=1)
    assert kills == [(4321, 1)]
    assert run_calls and run_calls[0][0][:2] == ["taskkill", "/PID"]
    assert process.waits == 2  # the graceful wait, then the final wait after taskkill
