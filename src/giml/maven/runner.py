"""Run Maven in a giml worktree (minimal runner for M3; M4 grows it into the BuildRunner).

Maven runs with the developer's own settings and local repository (spec section 9.2). Every
invocation writes a full log, has a hard timeout, and runs in its own process group so the whole
tree (forked test JVMs included) is killed on timeout or error.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

_TERMINATE_GRACE_SECONDS = 10

Env = Mapping[str, str] | None  # None: inherit giml's own environment


class MavenNotFound(RuntimeError):
    """No mvn executable on PATH."""


@dataclass(frozen=True)
class MavenResult:
    args: tuple[str, ...]
    exit_code: int | None  # None when the run timed out
    duration_seconds: float
    log_path: Path
    timed_out: bool

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


MavenRunner = Callable[[Path, list[str], Path, float, Env], MavenResult]


def _kill_group(process: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=_TERMINATE_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            continue


def run_maven(
    worktree: Path,
    args: Sequence[str],
    log_path: Path,
    timeout_seconds: float,
    env: Env = None,
) -> MavenResult:
    mvn = shutil.which("mvn", path=(env or os.environ).get("PATH"))
    if mvn is None:
        raise MavenNotFound("mvn is not on PATH; giml needs a working Maven installation")
    command = [mvn, "-B", "-ntp", *args]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    timed_out = False
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"$ cd {worktree} && {' '.join(command)}\n")
        log.flush()
        process = subprocess.Popen(command, cwd=worktree, stdout=log, stderr=subprocess.STDOUT,
                                   env=dict(env) if env is not None else None, start_new_session=True)  # fmt: skip
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            # Always clear the group: Maven may leave forked JVMs even after it exits.
            _kill_group(process)
        if timed_out:
            log.write(f"\n[giml] timed out after {timeout_seconds}s; process group killed\n")
    return MavenResult(tuple(args), None if timed_out else process.returncode,
                       time.monotonic() - started, log_path, timed_out)  # fmt: skip
