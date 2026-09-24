"""One giml run per project at a time (spec section 5.1).

Uses ``fcntl.flock`` on ``<state>/locks/<project key>.lock``. The kernel releases the lock when the
holding process exits for any reason, so a crashed run never leaves a stale lock behind. POSIX only
(Linux and macOS); ``fcntl`` does not exist on Windows.

The lock lives as long as the ``ProjectLock`` object: keep a reference (or use ``with``) for the
whole run, because a garbage-collected lock closes its file and releases the lock.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


class LockHeld(RuntimeError):
    """Another giml process holds the project lock."""


class ProjectLock:
    def __init__(self, state_dir: Path, project_key: str) -> None:
        self.path = state_dir / "locks" / f"{project_key}.lock"
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            holder = handle.read().strip() or "unknown"
            handle.close()
            raise LockHeld(f"another giml run (pid {holder}) holds {self.path}") from None
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle, fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None

    def __enter__(self) -> ProjectLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def is_locked(state_dir: Path, project_key: str) -> bool:
    """Whether a giml run currently holds this project's lock."""
    probe = ProjectLock(state_dir, project_key)
    try:
        probe.acquire()
    except LockHeld:
        return True
    probe.release()
    return False
