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
    pom_paths: tuple[str, ...]  # repository-relative reactor pom.xml files restored from the commit
    kept_paths: tuple[str, ...]  # reactor pom.xml files that did not exist yet; they keep base content


def pom_path(repo: RepoState) -> str:
    """Repository-relative path of the project's root pom.xml."""
    return f"{repo.subdir}/pom.xml" if repo.subdir else "pom.xml"


def _exists_at(git: Git, sha: str, path: str) -> bool:
    return git.run("cat-file", "-e", f"{sha}:{path}", check=False).returncode == 0


def resolve_rewind(repo: RepoState, commit: str, reactor_poms: list[Path]) -> RewindTarget:
    """Validate a rewind commit against the reactor's pom.xml files as discovered at the base."""
    git = Git(repo.root)
    resolved = git.run("rev-parse", "--verify", "-q", f"{commit}^{{commit}}", check=False)
    if resolved.returncode != 0:
        raise RewindError(f"--rewind-to {commit}: does not resolve to a commit")
    sha = resolved.stdout.strip()
    if git.run("merge-base", "--is-ancestor", sha, repo.base_sha, check=False).returncode != 0:
        raise RewindError(f"--rewind-to {commit}: {sha[:7]} is not an ancestor of the base commit {repo.base_sha[:7]}")
    root = pom_path(repo)
    if not _exists_at(git, sha, root):
        raise RewindError(f"--rewind-to {commit}: {sha[:7]} has no {root}")
    paths = [pom.relative_to(repo.root).as_posix() for pom in reactor_poms]
    restorable = tuple(p for p in paths if _exists_at(git, sha, p))
    kept = tuple(p for p in paths if p not in restorable)
    if all(git.out("rev-parse", f"{sha}:{p}") == git.out("rev-parse", f"{repo.base_sha}:{p}") for p in restorable):
        raise RewindError(f"--rewind-to {commit}: every pom.xml at {sha[:7]} is identical to the base commit's")
    return RewindTarget(sha, git.out("log", "-1", "--format=%cI", sha), restorable, kept)


def apply_rewind(worktree: Path, target: RewindTarget) -> str:
    """Replace the reactor's pom.xml files in a result worktree with their rewound content and
    commit them; returns the SHA."""
    git = Git(worktree)
    # restore (not a Python read/write) keeps each file byte-for-byte, line endings included.
    git.run("restore", "--source", target.sha, "--worktree", "--", *target.pom_paths)
    restored = "\n".join(f"  {p}" for p in target.pom_paths)
    kept = ""
    if target.kept_paths:
        listed = "\n".join(f"  {p}" for p in target.kept_paths)
        kept = f"\nKept at base (not present at {target.sha[:7]}):\n{listed}\n"
    message = (
        f"[giml-rewind] pom.xml from {target.sha[:7]} (synthetic)\n\n"
        f"Rewound to the content at {target.sha} (committed {target.committed_at}):\n{restored}\n{kept}\n"
        "Synthetic: this recreates an older dependency state from history for testing and training\n"
        "(giml spec 5.3). Every other file is unchanged from the base commit."
    )
    return commit_all(worktree, message)
