"""Rewind mode (spec section 5.3): start a run from an older pom.xml out of the project's history.

The rewound pom.xml is committed as the first, clearly marked synthetic commit on the result
branch. Only pom.xml changes; every other file, and every verification rule, stays at the base
commit (hard rule 7).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from giml.git.preflight import RepoState
from giml.git.runner import Git
from giml.git.worktrees import commit_all


class RewindError(ValueError):
    """The requested rewind commit cannot be used (exit 5)."""


@dataclass(frozen=True)
class RewindTarget:
    sha: str
    committed_at: str  # ISO 8601, as recorded in the commit
    pom_path: str  # repository-relative path of the project's pom.xml


def pom_path(repo: RepoState) -> str:
    return f"{repo.subdir}/pom.xml" if repo.subdir else "pom.xml"


def resolve_rewind(repo: RepoState, commit: str) -> RewindTarget:
    git = Git(repo.root)
    resolved = git.run("rev-parse", "--verify", "-q", f"{commit}^{{commit}}", check=False)
    if resolved.returncode != 0:
        raise RewindError(f"--rewind-to {commit}: does not resolve to a commit")
    sha = resolved.stdout.strip()
    if git.run("merge-base", "--is-ancestor", sha, repo.base_sha, check=False).returncode != 0:
        raise RewindError(f"--rewind-to {commit}: {sha[:7]} is not an ancestor of the base commit {repo.base_sha[:7]}")
    path = pom_path(repo)
    if git.run("cat-file", "-e", f"{sha}:{path}", check=False).returncode != 0:
        raise RewindError(f"--rewind-to {commit}: {sha[:7]} has no {path}")
    if git.out("rev-parse", f"{sha}:{path}") == git.out("rev-parse", f"{repo.base_sha}:{path}"):
        raise RewindError(f"--rewind-to {commit}: {path} at {sha[:7]} is identical to the base commit's")
    return RewindTarget(sha, git.out("log", "-1", "--format=%cI", sha), path)


def apply_rewind(worktree: Path, target: RewindTarget, identity: dict[str, str]) -> str:
    """Replace pom.xml in a result worktree with its rewound content and commit it; returns the SHA."""
    git = Git(worktree)
    # restore (not a Python read/write) keeps the file byte-for-byte, line endings included.
    git.run("restore", "--source", target.sha, "--worktree", "--", target.pom_path)
    message = (
        f"[giml-rewind] pom.xml from {target.sha[:7]} (synthetic)\n\n"
        f"Rewound {target.pom_path} to its content at {target.sha} (committed {target.committed_at}).\n"
        "Synthetic: this recreates an older dependency state from history for testing and training\n"
        "(giml spec 5.3). Every other file is unchanged from the base commit."
    )
    return commit_all(worktree, message, identity)
