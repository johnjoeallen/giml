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

- **Linux, macOS and Windows:** all supported, verified in CI on `ubuntu-latest`, `macos-latest`
  and `windows-latest` (`.github/workflows/tests.yml`).
- Four mechanisms need a platform-specific implementation, all behind `giml.core.platform`
  (callers never branch on `sys.platform` themselves):
  - the per-project run lock (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows);
  - disabling git hooks (`core.hooksPath` pointed at a real empty directory on every platform —
    this replaced the POSIX `os.devnull` trick, which was never actually the documented mechanism);
  - killing a build's or the smoke-launched application's whole process tree (`os.killpg` +
    `SIGTERM`/`SIGKILL` on POSIX; `CTRL_BREAK_EVENT` then `taskkill /T /F` on Windows, launched
    with `CREATE_NEW_PROCESS_GROUP`);
  - free disk space (`os.statvfs` on POSIX; `shutil.disk_usage` on Windows, which reports no free
    inode count, so that half of the check is skipped there).
- A run's temp directory commonly contains whitespace on Windows (`C:\Users\Jane Doe\...`); the
  `-Djava.io.tmpdir=` value passed through `JAVA_TOOL_OPTIONS` is quoted when needed. Verified
  against a real JDK, not just asserted (`tests/maven/test_isolation_java.py`, run in CI on all
  three OSes since it needs a real `java` on `PATH`).

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
