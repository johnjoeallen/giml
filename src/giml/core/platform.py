"""The handful of things a process needs to do differently on Windows than on POSIX (Linux, macOS).

Everywhere else in giml uses ``pathlib``, ``shutil.which`` and ``os.pathsep``, which are already
cross-platform. Only four mechanisms are not: exclusive file locking, disabling git hooks,
killing a process's whole tree, and reading free disk space. Callers use the functions below and
never branch on ``sys.platform`` themselves.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO

_WINDOWS = sys.platform == "win32"

# Windows locks a byte range, not a whole file description; this offset sits far past anything
# giml ever writes into a lock file, so the lock never blocks a losing acquirer from reading the
# pid text at the start of the file.
_WINDOWS_LOCK_OFFSET = 1 << 20


@dataclass(frozen=True)
class FreeSpace:
    bytes: int
    inodes: int | None  # None where the filesystem does not expose a free-inode count (e.g. NTFS)


def acquire_exclusive_lock(handle: IO[bytes]) -> bool:
    """Try to take an exclusive, non-blocking lock on ``handle``'s file. True on success."""
    if _WINDOWS:
        import msvcrt

        handle.seek(_WINDOWS_LOCK_OFFSET)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        finally:
            handle.seek(0)
        return True
    import fcntl

    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def release_exclusive_lock(handle: IO[bytes]) -> None:
    if _WINDOWS:
        import msvcrt

        handle.seek(_WINDOWS_LOCK_OFFSET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.seek(0)
        return
    import fcntl

    fcntl.flock(handle, fcntl.LOCK_UN)


def no_hooks_path() -> Path:
    """An empty, process-wide directory to pass as ``core.hooksPath`` so no repository hook runs.

    An empty directory is the documented way to disable hooks via ``core.hooksPath`` and behaves
    the same on every platform, unlike the POSIX ``/dev/null`` trick it replaces.
    """
    path = Path(tempfile.gettempdir()) / "giml-no-hooks"
    path.mkdir(exist_ok=True)
    return path


def popen_in_new_group(argv: list[str], **kwargs: object) -> subprocess.Popen:
    """Start a process in its own group/session, so its whole tree can be killed later."""
    if _WINDOWS:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        return subprocess.Popen(argv, creationflags=flags, **kwargs)  # type: ignore[call-overload]
    return subprocess.Popen(argv, start_new_session=True, **kwargs)  # type: ignore[call-overload]


def terminate_process_tree(process: subprocess.Popen, grace_seconds: float) -> None:
    """Ask the process's whole tree to stop, then force it, however long that takes."""
    if _WINDOWS:
        try:
            os.kill(process.pid, signal.CTRL_BREAK_EVENT)
        except OSError:
            return
        try:
            process.wait(timeout=grace_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
        process.wait()
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=grace_seconds)
            return
        except subprocess.TimeoutExpired:
            continue


def free_space(path: Path) -> FreeSpace:
    """Free bytes and (where the filesystem exposes it) free inodes at ``path``."""
    if _WINDOWS:
        return FreeSpace(bytes=shutil.disk_usage(path).free, inodes=None)
    stat = os.statvfs(path)
    inodes = stat.f_favail if stat.f_files else None
    return FreeSpace(bytes=stat.f_bavail * stat.f_frsize, inodes=inodes)
