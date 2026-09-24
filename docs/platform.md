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

- **Linux and macOS:** supported.
- **Windows:** not supported in phase 1. The per-project run lock uses `fcntl.flock`, which does not
  exist on Windows, and hook disabling relies on `core.hooksPath=/dev/null` (`os.devnull`).

## How giml stays out of the developer's way

- All git access goes through `giml.git.runner.Git`. Only an allowlist of local subcommands can run;
  `push` is rejected before any process starts.
- Hooks are disabled per invocation (`-c core.hooksPath=/dev/null`); the repository's shared
  config is never written.
- `GIT_*` environment variables are removed before every git call, so a shell that sets `GIT_DIR`
  or `GIT_WORK_TREE` cannot redirect giml to another repository.
- giml works only in worktrees under `<state>/worktrees/` (default `~/.giml/worktrees/`). Creating
  them adds entries under the repository's `.git/worktrees/` and new `giml/...` branches; the
  developer's working tree, index and current branch are never changed.
- Commits use the developer's `user.name` / `user.email` from git config, carry a
  `Generated-by: giml <version>` trailer and are never GPG-signed (a signing prompt would block
  an unattended run).
