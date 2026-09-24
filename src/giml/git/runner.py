"""The only way giml runs git (hard rules 1 and 2).

Every command goes through ``Git.run``, which:
  * allows only an explicit set of local subcommands, and rejects ``push`` (and everything else)
    before any process starts;
  * disables hooks for every invocation with ``-c core.hooksPath=/dev/null``, so no repository
    hook runs inside giml's worktrees and the shared git config is never modified;
  * scrubs ``GIT_*`` variables from the environment so the developer's shell cannot redirect git
    at another repository, index or work tree.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

# Subcommand -> allowed first arguments (None: any). Everything here is local-only.
_ALLOWED: dict[str, tuple[str, ...] | None] = {
    "add": None,
    "branch": None,
    "cat-file": None,
    "commit": None,
    "config": ("--get",),
    "for-each-ref": None,
    "log": None,
    "ls-files": None,
    "merge-base": None,
    "remote": ("get-url",),
    "rev-parse": None,
    "show": None,
    "status": None,
    "symbolic-ref": None,
    "worktree": None,
}


class GitError(RuntimeError):
    """A git command failed. Carries the command, exit code and stderr."""

    def __init__(self, args: Sequence[str], returncode: int, stderr: str) -> None:
        super().__init__(f"git {' '.join(args)} failed ({returncode}): {stderr.strip()}")
        self.args_ = tuple(args)
        self.returncode = returncode
        self.stderr = stderr


class ForbiddenGitCommand(RuntimeError):
    """giml tried to run a git command it must never run."""


def _clean_environment(extra: Mapping[str, str] | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    env.update(extra or {})
    return env


def check_allowed(subcommand: str, args: Sequence[str]) -> None:
    if subcommand == "push":
        raise ForbiddenGitCommand("git push is never allowed: giml does not write to remotes (hard rule 1)")
    if subcommand not in _ALLOWED:
        raise ForbiddenGitCommand(f"git {subcommand} is not on giml's allowlist of local commands")
    allowed_first = _ALLOWED[subcommand]
    if allowed_first is not None and (not args or args[0] not in allowed_first):
        raise ForbiddenGitCommand(f"git {subcommand} is only allowed as: {', '.join(allowed_first)}")


class Git:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def run(
        self,
        subcommand: str,
        *args: str,
        check: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        check_allowed(subcommand, args)
        command = ["git", "-C", str(self.directory), "-c", f"core.hooksPath={os.devnull}", subcommand, *args]
        result = subprocess.run(command, capture_output=True, text=True, env=_clean_environment(env), check=False)
        if check and result.returncode != 0:
            raise GitError([subcommand, *args], result.returncode, result.stderr)
        return result

    def out(self, subcommand: str, *args: str) -> str:
        """Run a command and return its stdout without the trailing newline."""
        return self.run(subcommand, *args).stdout.rstrip("\n")
