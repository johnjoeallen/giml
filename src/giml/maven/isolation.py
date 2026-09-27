"""Per-run isolation for the tools giml drives (spec section 9.2).

Maven, Surefire's forked test JVMs and PIT's minions all write temporary files, and PIT kills a test
JVM whenever a mutant times out, so its cleanup never runs. On a shared ``/tmp`` that once left
over a thousand directories and exhausted the inodes, failing unrelated builds. Every run therefore
gets its own temporary directory under the run's folder in the state directory, every child process
is pointed at it, and it is deleted when the run ends. Before a run starts the state directory's
filesystem is checked for free space and free inodes, so a full disk is reported as such instead of
surfacing as a failed build.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from giml.core.platform import free_space

MIN_FREE_BYTES = 512 * 1024 * 1024  # a worktree, PIT reports and build output for a real project
MIN_FREE_INODES = 20_000  # a test run that leaks temp directories can create tens of thousands

_WHITESPACE = re.compile(r"\s")


class InsufficientSpace(RuntimeError):
    """The filesystem cannot hold a run: the message says how much is free and how much is needed (exit 4)."""


@dataclass(frozen=True)
class TempStats:
    """What was still in a run's temp directory when it was removed."""

    files: int  # files and directories
    bytes: int


def check_space(path: Path, min_bytes: int = MIN_FREE_BYTES, min_inodes: int = MIN_FREE_INODES) -> None:
    """Raise InsufficientSpace unless ``path``'s filesystem has room. Inodes are skipped where not counted."""
    space = free_space(path)
    if space.bytes < min_bytes:
        raise InsufficientSpace(f"{path}: only {space.bytes // 2**20} MB free on this filesystem, need {min_bytes // 2**20} MB")
    if space.inodes is not None and space.inodes < min_inodes:
        raise InsufficientSpace(f"{path}: only {space.inodes} inodes free on this filesystem, need {min_inodes}")


class RunTemp:
    """A run's temporary directory; ``stats`` is filled in when the run ends."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.stats = TempStats(0, 0)


def _remove(path: Path) -> TempStats:
    entries = size = 0
    for directory, names, files in os.walk(path):
        entries += len(names) + len(files)
        size += sum((Path(directory) / name).lstat().st_size for name in files)
    if path.exists():
        shutil.rmtree(path)
    return TempStats(entries, size)


@contextlib.contextmanager
def run_temp(state_dir: Path, run_id: str, log_path: Path | None = None, check: bool = True) -> Iterator[RunTemp]:
    """Create the run's temp directory (after checking space) and remove it afterwards, even on failure."""
    run_dir = state_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if check:
        check_space(run_dir)
    temp = RunTemp(run_dir / "tmp")
    temp.path.mkdir(exist_ok=True)
    try:
        yield temp
    finally:
        temp.stats = _remove(temp.path)
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"removed {temp.path}: {temp.stats.files} files and directories, {temp.stats.bytes} bytes\n")


def isolated_env(jdk_env: Mapping[str, str] | None, environ: Mapping[str, str], temp: RunTemp) -> dict[str, str]:
    """The environment for every Maven and java call: the chosen JDK's (or the inherited one) plus the temp directory.

    JAVA_TOOL_OPTIONS reaches every JVM, including Surefire forks and PIT minions, without touching the
    project's own ``argLine`` (JaCoCo sets that). Options the developer already has are kept. A path
    containing whitespace (common on Windows, e.g. ``C:\\Users\\Jane Doe\\...``) is quoted: the JDK's
    environment-variable option parser honours quoted tokens (verified in CI, see
    ``tests/maven/test_isolation_java.py``).
    """
    env = dict(jdk_env if jdk_env is not None else environ)
    tmpdir = f'"{temp.path}"' if _WHITESPACE.search(str(temp.path)) else str(temp.path)
    option = f"-Djava.io.tmpdir={tmpdir}"
    existing = env.get("JAVA_TOOL_OPTIONS")
    env["JAVA_TOOL_OPTIONS"] = f"{existing} {option}" if existing else option
    env["TMPDIR"] = str(temp.path)
    return env
