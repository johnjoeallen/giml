from pathlib import Path

import pytest

from giml.core.platform import FreeSpace
from giml.maven import isolation
from giml.maven.isolation import InsufficientSpace, TempStats, check_space, isolated_env, run_temp

pytestmark = pytest.mark.real_space_check


def fake_free_space(free_bytes=10**12, free_inodes=10**6):
    return lambda path: FreeSpace(bytes=free_bytes, inodes=free_inodes)


def test_check_space_accepts_a_roomy_filesystem(monkeypatch, tmp_path):
    monkeypatch.setattr(isolation, "free_space", fake_free_space())
    check_space(tmp_path)


def test_check_space_refuses_low_disk_space_naming_the_numbers(monkeypatch, tmp_path):
    monkeypatch.setattr(isolation, "free_space", fake_free_space(free_bytes=100 * 1024 * 1024))
    with pytest.raises(InsufficientSpace, match=r"only 100 MB free .*need 512 MB"):
        check_space(tmp_path)


def test_check_space_refuses_when_inodes_run_out_even_with_space_left(monkeypatch, tmp_path):
    # The /tmp incident: 635 MB free but 10 inodes.
    monkeypatch.setattr(isolation, "free_space", fake_free_space(free_bytes=635 * 1024 * 1024, free_inodes=10))
    with pytest.raises(InsufficientSpace, match=r"only 10 inodes free .*need 20000"):
        check_space(tmp_path)


def test_filesystems_without_inode_counts_are_not_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(isolation, "free_space", fake_free_space(free_inodes=None))  # e.g. NTFS or btrfs reports none
    check_space(tmp_path)


def test_thresholds_can_be_lowered(monkeypatch, tmp_path):
    monkeypatch.setattr(isolation, "free_space", fake_free_space(free_bytes=100 * 1024 * 1024, free_inodes=50))
    check_space(tmp_path, min_bytes=50 * 1024 * 1024, min_inodes=10)


def test_the_temp_directory_is_created_inside_the_run_and_removed_after(tmp_path):
    with run_temp(tmp_path, "run-1") as temp:
        assert temp.path == tmp_path / "runs" / "run-1" / "tmp" and temp.path.is_dir()
        (temp.path / "junit-1").mkdir()
        (temp.path / "junit-1" / "a.txt").write_text("hello")
    assert not (tmp_path / "runs" / "run-1" / "tmp").exists()
    assert (tmp_path / "runs" / "run-1").is_dir()  # the run's logs live next to it


def test_the_temp_directory_is_removed_even_when_the_run_fails(tmp_path):
    with pytest.raises(RuntimeError, match="boom"), run_temp(tmp_path, "run-1") as temp:
        (temp.path / "left-behind").write_text("x")
        raise RuntimeError("boom")
    assert not (tmp_path / "runs" / "run-1" / "tmp").exists()


def test_leftovers_are_counted_and_logged(tmp_path):
    log = tmp_path / "logs" / "temp.log"
    with run_temp(tmp_path, "run-1", log_path=log) as temp:
        for name in ("a", "b"):
            (temp.path / name).mkdir()
            (temp.path / name / "f.txt").write_text("12345")
    text = log.read_text()
    assert text == f"removed {temp.path}: 4 files and directories, 10 bytes\n"
    assert temp.stats == TempStats(files=4, bytes=10)


def test_an_untouched_temp_directory_logs_nothing_left(tmp_path):
    log = tmp_path / "temp.log"
    with run_temp(tmp_path, "run-1", log_path=log) as temp:
        pass
    assert log.read_text() == f"removed {temp.path}: 0 files and directories, 0 bytes\n"


def test_space_is_checked_before_the_run_starts(monkeypatch, tmp_path):
    monkeypatch.setattr(isolation, "free_space", fake_free_space(free_inodes=5))
    with pytest.raises(InsufficientSpace), run_temp(tmp_path, "run-1"):
        raise AssertionError("must not start")
    assert not (tmp_path / "runs" / "run-1" / "tmp").exists()


def test_space_checking_can_be_switched_off_for_tests(monkeypatch, tmp_path):
    monkeypatch.setattr(isolation, "free_space", fake_free_space(free_inodes=5))
    with run_temp(tmp_path, "run-1", check=False) as temp:
        assert temp.path.is_dir()


def test_isolated_env_points_every_jvm_and_tool_at_the_run_directory(tmp_path):
    with run_temp(tmp_path, "run-1") as temp:
        env = isolated_env(None, {"PATH": "/usr/bin", "HOME": "/h"}, temp)
        assert env["TMPDIR"] == str(temp.path)
        assert env["JAVA_TOOL_OPTIONS"] == f"-Djava.io.tmpdir={temp.path}"
        assert (env["PATH"], env["HOME"]) == ("/usr/bin", "/h")


def test_isolated_env_keeps_the_developers_java_options_and_the_chosen_jdk(tmp_path):
    with run_temp(tmp_path, "run-1") as temp:
        jdk_env = {"PATH": "/jdk/bin:/usr/bin", "JAVA_HOME": "/jdk", "JAVA_TOOL_OPTIONS": "-Xmx2g", "TMPDIR": "/tmp"}
        env = isolated_env(jdk_env, {"PATH": "/ignored"}, temp)
        assert env["JAVA_TOOL_OPTIONS"] == f"-Xmx2g -Djava.io.tmpdir={temp.path}"
        assert (env["PATH"], env["JAVA_HOME"], env["TMPDIR"]) == ("/jdk/bin:/usr/bin", "/jdk", str(temp.path))
        assert jdk_env["TMPDIR"] == "/tmp"  # the caller's mapping is not modified


def test_a_state_directory_with_whitespace_is_quoted_because_java_options_would_split_on_it(tmp_path):
    with run_temp(tmp_path / "my state", "run-1") as temp:
        env = isolated_env(None, {}, temp)
        assert env["JAVA_TOOL_OPTIONS"] == f'-Djava.io.tmpdir="{temp.path}"'


def test_module_constants_document_the_thresholds():
    assert isolation.MIN_FREE_BYTES == 512 * 1024 * 1024 and isolation.MIN_FREE_INODES == 20_000
    assert Path(isolation.__file__).name == "isolation.py"
