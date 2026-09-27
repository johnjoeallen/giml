"""Run Maven in a giml worktree (minimal runner for M3; M4 grows it into the BuildRunner).

Maven runs with the developer's own settings and local repository (spec section 9.2). Every
invocation writes a full log, has a hard timeout, and runs in its own process group so the whole
tree (forked test JVMs included) is killed on timeout or error.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from giml.core.platform import BashNotFound, native_argv, popen_in_new_group, terminate_process_tree

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
    try:
        command = native_argv(mvn, ["-B", "-ntp", *args])
    except BashNotFound as exc:
        raise MavenNotFound(str(exc)) from exc
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    timed_out = False
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"$ cd {worktree} && {' '.join(command)}\n")
        log.flush()
        process = popen_in_new_group(command, cwd=worktree, stdout=log, stderr=subprocess.STDOUT,
                                     env=dict(env) if env is not None else None)  # fmt: skip
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            # Always clear the group: Maven may leave forked JVMs even after it exits.
            terminate_process_tree(process, _TERMINATE_GRACE_SECONDS)
        if timed_out:
            log.write(f"\n[giml] timed out after {timeout_seconds}s; process group killed\n")
    return MavenResult(tuple(args), None if timed_out else process.returncode,
                       time.monotonic() - started, log_path, timed_out)  # fmt: skip
