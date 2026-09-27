# Platform notes

What giml needs from the machine it runs on, and what is and is not supported.

## Requirements

| Tool | Tested with | Notes |
|---|---|---|
| Python | 3.13.5 | 3.12 or newer (`pyproject.toml`). |
| git | 2.47.3 | Uses `worktree add/remove/prune`, `restore --source`, `status --porcelain=v2`. On Windows this must be Git for Windows specifically (not just any `git.exe`), with its bundled Git Bash component installed (the default) — see below. |
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

  `windows-latest` in CI failed the wider fast suite (29 tests as of 2026-09-27); the fixable-from-
  source items below were fixed on 2026-09-27 (verified only by reading and by the Linux fast suite,
  1,386 tests, still green — none of this is proven on real Windows until the next CI run):
  - `giml.maven.jdk.looks_like_jdk` now also accepts `bin/javac.exe`, so `resolve_jdk`,
    `JdkCatalog.configured()` and `.toolchains()` stop misreporting a real Windows JDK home as
    not a JDK.
  - a real, previously-undiscovered production bug, not just a test gap: `shutil.which("mvn")`
    resolves to `mvn.cmd` on Windows, and `CreateProcess` cannot launch a `.bat`/`.cmd` directly
    under `shell=False` (`%1 is not a valid Win32 application`). Fixed by requiring Git for Windows'
    Git Bash on Windows — a POSIX-compatible git is already a hard requirement, and the default Git
    for Windows install always includes it — rather than trying to make Windows batch files behave:
    `giml.core.platform.native_argv(resolved, args)` runs a resolved `.exe` directly (real `java`,
    `git`, `psql`) and, on Windows, runs a resolved `.cmd`/`.bat` (real `mvn.cmd`, or a script-based
    test double) through its POSIX sibling script of the same name via `bash` instead
    (`posix_script_argv`, which raises `BashNotFound` — caught and surfaced as `MavenNotFound` by
    `run_maven`, as "unknown" by `maven_version`, and left to propagate as itself elsewhere, added to
    the CLI's infrastructure-error tuple). Both real `mvn` call sites
    (`giml.maven.runner.run_maven`, `giml.maven.cache.maven_version`) and `giml.gate.assess.run_java`
    go through it. Covered by `tests/core/test_platform.py` with a faked-Windows branch, same pattern
    as the other four mechanisms; unverified against a real Windows `mvn.cmd`.
  - several `pytest.raises(..., match=...)` regexes were built directly from a real filesystem path
    without `re.escape`, so a Windows path's backslashes would be read as regex escape sequences
    (`tests/core/test_config.py`, `tests/data/test_snapshots.py`, `tests/gate/test_assess.py`);
    fixed. Two more in `tests/maven/test_jdk.py` hard-coded a POSIX `/` inside the pattern where the
    actual separator is native, so they never matched a Windows path either; fixed by dropping the
    assumption instead of hard-coding a separator.
  - three tests read a report's markdown/JSON back with `.read_text()` and no `encoding=`, which
    would raise `UnicodeDecodeError` on the `→` arrow under Windows' default (non-UTF-8) locale
    encoding (`tests/plan/test_planner_run.py`, `tests/plan/test_analysis.py`,
    `tests/plan/test_plan_fixture_maven.py`); fixed by passing `encoding="utf-8"`, matching what
    giml's own writers already do.

  Also fixed, same day, after deciding to require Git Bash on Windows (git is already mandatory,
  and Git for Windows bundles it by default) rather than adding more Windows-batch-file special
  cases: `tests/conftest.py`'s new `write_posix_script(path, body)` writes a `#!/bin/sh` fake
  executable plus, on Windows, a `.cmd` sibling shimming it through `bash` — mirroring how the real
  Apache Maven distribution ships both `mvn` and `mvn.cmd` side by side, so `shutil.which` finds it
  the same way on every platform. The fake `mvn`/`java` test doubles in `tests/maven/test_runner.py`,
  `tests/maven/test_cache.py` and `tests/gate/test_assess.py` were switched to it.

  That same decision also closes `tests/maven/test_runner.py`'s `fake_mvn`-based process-group
  tests, previously flagged as needing a bigger, riskier rewrite: `FAKE_MVN`'s `sleep 30 &`/`$!`
  backgrounding is plain POSIX shell, which Git Bash runs unchanged once the script is launched
  through it. The pid-liveness check moved into its own module, `tests/process_liveness.py`
  (`pid_alive`), the one place this checks branches on platform: POSIX keeps `os.kill(pid, 0)` as
  a fast existence check with `ps -o stat=` as a zombie-detecting fallback, unchanged from before;
  Windows has no equivalent to `os.kill(pid, 0)`, so it uses `ps -o stat=` alone there, routed
  through `bash -c` since `ps` (from Git for Windows' bundled MSYS/procps-ng) lives in Git's
  `usr/bin`, which the plain Windows PATH doesn't necessarily include even though `bash.exe`
  itself does. Still
  unverified on real Windows — in particular, whether MSYS `ps` reports Windows PIDs the way giml's
  own Windows process-tree code (`taskkill /PID`) expects, and whether `usr/bin` needs adding to
  PATH explicitly for a non-bash caller (giml itself never calls `ps`, only this test does).

  Still open, deliberately not attempted yet (larger and riskier to get right unverified from a
  Linux sandbox):
  - git preflight/LFS hook tests may or may not actually be broken by the same shell-script
    mechanism: Git for Windows runs a shebang hook through its own bundled `sh.exe`, so
    `tests/git/repo_helpers.py`'s `install_marker_hooks` might already work unmodified on Windows;
    not touched without evidence either way.
  - a report's `→` arrow corruption risk in giml's own runtime code (not just tests) is not fully
    ruled out; giml's writers pass `encoding="utf-8"` everywhere checked, but this has not been
    verified against a real Windows report reader.

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
