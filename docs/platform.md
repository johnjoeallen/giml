# Platform notes

What giml needs from the machine it runs on, and what is and is not supported.

## Requirements

| Tool | Tested with | Notes |
|---|---|---|
| Python | 3.13.5 | 3.12 or newer (`pyproject.toml`). |
| git | 2.47.3 | Uses `worktree add/remove/prune`, `restore --source`, `status --porcelain=v2`. |
| Maven | 3.9.11 | Only for the slow version-ordering differential test in M1; builds start in M4. |
| JDK | 25 (Zulu) | Same as Maven. |

## Operating systems

- **Linux and macOS:** supported, verified in CI on `ubuntu-latest` and `macos-latest`
  (`.github/workflows/tests.yml`), fast suite plus the real-JDK isolation tests, both green.
- **Windows: partially supported, not yet verified green.** The four mechanisms giml itself needs
  per-platform are implemented and covered by `tests/core/test_platform.py` (real on POSIX, faked
  on Windows since this project is developed on Linux):
  - the per-project run lock (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows);
  - disabling git hooks (`core.hooksPath` pointed at a real empty directory on every platform —
    this replaced the POSIX `os.devnull` trick, which was never actually the documented mechanism);
  - killing a build's or the smoke-launched application's whole process tree (`os.killpg` +
    `SIGTERM`/`SIGKILL` on POSIX; `CTRL_BREAK_EVENT` then `taskkill /T /F` on Windows, launched
    with `CREATE_NEW_PROCESS_GROUP`);
  - free disk space (`os.statvfs` on POSIX; `shutil.disk_usage` on Windows, which reports no free
    inode count, so that half of the check is skipped there);
  - a run temp directory containing whitespace (common on Windows, `C:\Users\Jane Doe\...`): the
    `-Djava.io.tmpdir=` value passed through `JAVA_TOOL_OPTIONS` is quoted, verified against a
    real JDK (`tests/maven/test_isolation_java.py`), not just asserted.

  But `windows-latest` in CI still fails the wider fast suite (29 tests as of 2026-09-27), for
  reasons unrelated to those four mechanisms — this is test-infrastructure and validation-logic
  debt, not yet fixed:
  - several tests fake `mvn`/`java` by writing `#!/bin/sh` scripts and `chmod`-ing them executable
    (e.g. `tests/maven/test_cache.py`, `tests/gate/test_assess.py`); these don't run at all on
    native Windows (no shebang support, `chmod` is a no-op) and need a portable test double;
  - several tests build a `pytest.raises(..., match=...)` regex directly from a real filesystem
    path without `re.escape`; a Windows path's backslashes get read as regex escape sequences;
  - `giml.core.config`'s JDK-home path validation only accepts POSIX-absolute paths or `~`, not
    `C:\...`;
  - `giml.maven.jdk` doesn't yet account for `javac.exe`/`java.exe` on Windows;
  - a report's `→` arrow character came back corrupted in at least one Windows test, consistent
    with a read-back somewhere lacking `encoding="utf-8"` (Windows' default text encoding is not
    UTF-8) — giml's own writers already pass `encoding="utf-8"` everywhere checked, so this looks
    like a test-helper gap rather than a user-facing one, but it is not yet confirmed either way;
  - git preflight/LFS hook tests and the process-tree-kill tests in `tests/maven/test_runner.py`
    also fail on Windows, likely for the same shell-script-test-double reason above.

  `.github/workflows/tests.yml`'s `windows-latest` job is left failing (not masked with
  `continue-on-error`) so this stays visible until it's actually fixed, tracked as follow-up work
  rather than claimed as done.
- Known gap: `tests/smoke/test_runner_real.py` (real JVM, real grandchild process, `-m slow`) is
  Linux-only, not just POSIX — it launches a real `sleep 300` grandchild and reads
  `/proc/<pid>/stat` to poll liveness, neither of which exists on Windows or macOS. It is not run
  by `.github/workflows/tests.yml` (which only runs the fast suite plus the one targeted real-JDK
  isolation test) and is not yet made portable; the process-tree-kill *logic* it exercises is
  covered cross-platform by `tests/core/test_platform.py`'s faked-Windows-branch tests instead.

## How giml stays out of the developer's way

- All git access goes through `giml.git.runner.Git`. Only an allowlist of local subcommands can run;
  `push` is rejected before any process starts.
- Hooks are disabled per invocation (`-c core.hooksPath=<empty directory>`, see above); the
  repository's shared config is never written.
- `GIT_*` environment variables are removed before every git call, so a shell that sets `GIT_DIR`
  or `GIT_WORK_TREE` cannot redirect giml to another repository.
- giml works only in worktrees under `<state>/worktrees/` (default `~/.giml/worktrees/`). Creating
  them adds entries under the repository's `.git/worktrees/` and new `giml/...` branches; the
  developer's working tree, index and current branch are never changed.
- Commits use the developer's `user.name` / `user.email` from git config, carry a
  `Generated-by: giml <version>` trailer and are never GPG-signed (a signing prompt would block
  an unattended run).
