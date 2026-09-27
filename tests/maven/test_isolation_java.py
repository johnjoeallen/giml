"""A real JVM honours the isolated environment (slow: starts java). Run with `pytest -m slow`."""

import shutil
import subprocess

import pytest

from giml.maven.isolation import isolated_env, run_temp

pytestmark = pytest.mark.slow


def test_a_real_jvm_uses_the_run_temp_directory(tmp_path):
    java = shutil.which("java")
    if java is None:
        pytest.skip("java is required")
    with run_temp(tmp_path, "run-1") as temp:
        env = isolated_env(None, {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}, temp)
        result = subprocess.run([java, "-XshowSettings:properties", "-version"], env=env, capture_output=True, text=True, check=True)
        assert f"java.io.tmpdir = {temp.path}" in result.stderr
    assert not temp.path.exists()


def test_a_real_jvm_honours_a_quoted_tmpdir_containing_whitespace(tmp_path):
    """Windows state directories commonly contain spaces (``C:\\Users\\Jane Doe\\...``); confirms the
    JDK's JAVA_TOOL_OPTIONS parser really does honour a quoted token rather than splitting on the space."""
    java = shutil.which("java")
    if java is None:
        pytest.skip("java is required")
    state_dir = tmp_path / "state with space"
    with run_temp(state_dir, "run-1") as temp:
        assert " " in str(temp.path)  # otherwise this test isn't exercising the quoting at all
        env = isolated_env(None, {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}, temp)
        result = subprocess.run([java, "-XshowSettings:properties", "-version"], env=env, capture_output=True, text=True, check=True)
        assert f"java.io.tmpdir = {temp.path}" in result.stderr
