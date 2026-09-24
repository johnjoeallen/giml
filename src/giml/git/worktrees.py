"""Tool-owned worktrees and branches (spec section 5.2).

All of giml's work happens in worktrees under ``<state>/worktrees/<project key>/``; the developer's
own checkout is only ever read (hard rule 2). Result branches are created without upstream
tracking, and the git wrapper refuses ``push`` regardless (hard rule 1).
"""

from __future__ import annotations

import datetime
from pathlib import Path

from giml import __version__
from giml.git.preflight import RepoState
from giml.git.runner import Git


class IdentityError(ValueError):
    """The developer's repository has no user.name/user.email for giml's commits (exit 5)."""


class BranchExistsError(RuntimeError):
    """The result branch name is already taken; giml never overwrites a branch."""


class ForeignWorktreeError(ValueError):
    """A path giml was asked to remove is not one of its own worktrees."""


def result_branch(base_sha: str, when: datetime.datetime, rewind: bool) -> str:
    stamp = when.astimezone(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    prefix = "giml/rewind" if rewind else "giml"
    return f"{prefix}/{base_sha[:7]}/{stamp}"


def developer_identity(repo_root: Path) -> dict[str, str]:
    """Author and committer environment from the developer's git config."""
    git = Git(repo_root)
    values = {}
    for key in ("user.name", "user.email"):
        result = git.run("config", "--get", key, check=False)
        if result.returncode != 0 or not result.stdout.strip():
            raise IdentityError(f"{repo_root}: git {key} is not configured; giml commits under your identity")
        values[key] = result.stdout.strip()
    name, email = values["user.name"], values["user.email"]
    return {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}  # fmt: skip


def commit_all(worktree: Path, message: str, identity: dict[str, str]) -> str:
    """Commit every change in a giml worktree; returns the new commit SHA."""
    git = Git(worktree)
    git.run("add", "-A")
    full_message = f"{message.rstrip()}\n\nGenerated-by: giml {__version__}\n"
    # --no-gpg-sign: a signing prompt must never block an unattended run.
    git.run("commit", "-q", "--no-gpg-sign", "-m", full_message, env=identity)
    return git.out("rev-parse", "HEAD")


class WorktreeManager:
    def __init__(self, repo: RepoState, state_dir: Path, run_id: str) -> None:
        self.repo = repo
        self.run_id = run_id
        self.root = state_dir / "worktrees" / repo.project_key
        self._git = Git(repo.root)

    def create_result(self, branch: str) -> Path:
        """The run's result worktree on a new branch at the base commit."""
        exists = self._git.run("rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False)
        if exists.returncode == 0:
            raise BranchExistsError(f"branch {branch} already exists; giml never overwrites a branch")
        path = self.root / self.run_id
        path.parent.mkdir(parents=True, exist_ok=True)
        self._git.run("worktree", "add", "-q", "--no-track", "-b", branch, str(path), self.repo.base_sha)
        return path

    def create_trial(self, commit: str, number: int) -> Path:
        """A throwaway detached worktree for one candidate build (used from M4)."""
        path = self.root / f"{self.run_id}-trial-{number}"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._git.run("worktree", "add", "-q", "--detach", str(path), commit)
        return path

    def remove(self, path: Path) -> None:
        remove_worktree(self.repo.root, self.root, path)


def remove_worktree(repo_root: Path, owned_root: Path, path: Path) -> None:
    """Remove one giml worktree. Refuses anything outside ``owned_root``."""
    resolved = path.resolve()
    if not resolved.is_relative_to(owned_root.resolve()) or resolved == owned_root.resolve():
        raise ForeignWorktreeError(f"{path} is not a giml worktree under {owned_root}")
    git = Git(repo_root)
    if resolved.exists():
        git.run("worktree", "remove", "--force", str(resolved))
    git.run("worktree", "prune")
