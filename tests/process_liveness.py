"""Whether a pid is still alive, for tests that prove giml's process-tree-kill logic.

POSIX has `os.kill(pid, 0)` for a fast existence check, with `ps -o stat=` as a fallback to see
past a lingering zombie left behind by an exited parent. Windows has no equivalent to
`os.kill(pid, 0)`, so it goes through `ps` alone instead, run via `bash -c` since Git for Windows'
`ps` (from its bundled MSYS/procps-ng) lives in `usr/bin`, which the plain Windows PATH doesn't
necessarily include even though `bash.exe` itself does.
"""

from __future__ import annotations

import os
import subprocess
import sys

_WINDOWS = sys.platform == "win32"


def pid_alive(pid: int) -> bool:
    if _WINDOWS:
        return _ps_alive(["bash", "-c", 'ps -o stat= -p "$1" 2>/dev/null', "ps", str(pid)])
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return _ps_alive(["ps", "-o", "stat=", "-p", str(pid)])


def _ps_alive(argv: list[str]) -> bool:
    state = subprocess.run(argv, capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")
