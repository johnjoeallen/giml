"""Preflight hard stops (spec section 5.1). Any failure means giml does not start (exit 2)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from giml.git.runner import Git

_IN_PROGRESS = (
    ("MERGE_HEAD", "a merge"),
    ("rebase-merge", "a rebase"),
    ("rebase-apply", "a rebase or am"),
    ("CHERRY_PICK_HEAD", "a cherry-pick"),
    ("REVERT_HEAD", "a revert"),
    ("BISECT_LOG", "a bisect"),
)
_SHOWN_DIRTY_ENTRIES = 5


class PreflightRefusal(RuntimeError):
    """The repository is not in a state giml may work from."""


@dataclass(frozen=True)
class RepoState:
    root: Path  # repository top level (resolved)
    subdir: str  # project directory relative to root, POSIX style; "" for the root
    base_sha: str
    branch: str | None  # None when HEAD is detached

    @property
    def project_key(self) -> str:
        return project_key(self.root, self.subdir)

    @property
    def project_dir(self) -> Path:
        return self.root / self.subdir if self.subdir else self.root


def project_key(root: Path, subdir: str) -> str:
    digest = hashlib.sha256(f"{root}/{subdir}".encode()).hexdigest()[:8]
    return f"{root.name}-{digest}"


def locate(path: Path) -> tuple[Path, str]:
    """The repository top level and the project subdirectory for a path, without other checks."""
    path = path.resolve()
    if not path.is_dir():
        raise PreflightRefusal(f"{path}: not a directory")
    inside = Git(path).run("rev-parse", "--is-inside-work-tree", "--show-toplevel", check=False)
    lines = inside.stdout.splitlines()
    if inside.returncode != 0 or not lines or lines[0] != "true":
        raise PreflightRefusal(f"{path}: not inside a git work tree")
    root = Path(lines[1]).resolve()
    subdir = PurePosixPath(path.relative_to(root).as_posix()).as_posix()
    return root, "" if subdir == "." else subdir


def preflight(path: Path, allow_detached: bool = False) -> RepoState:
    root, subdir = locate(path)
    path = path.resolve()
    git = Git(path)

    head = git.run("rev-parse", "--verify", "-q", "HEAD^{commit}", check=False)
    if head.returncode != 0:
        raise PreflightRefusal(f"{root}: HEAD does not resolve to a commit (empty repository?)")

    branch_result = git.run("symbolic-ref", "-q", "--short", "HEAD", check=False)
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
    if branch is None and not allow_detached:
        raise PreflightRefusal(f"{root}: HEAD is detached; check out a branch or pass --allow-detached")

    for marker, operation in _IN_PROGRESS:
        if (path / git.out("rev-parse", "--git-path", marker)).exists():
            raise PreflightRefusal(f"{root}: {operation} is in progress; finish or abort it first")

    status = git.out("status", "--porcelain=v2", "--untracked-files=all", "--ignore-submodules=none")
    if status:
        entries = status.splitlines()
        shown = "; ".join(entries[:_SHOWN_DIRTY_ENTRIES])
        more = f" (and {len(entries) - _SHOWN_DIRTY_ENTRIES} more)" if len(entries) > _SHOWN_DIRTY_ENTRIES else ""
        raise PreflightRefusal(f"{root}: working tree is not clean: {shown}{more}")

    _refuse_submodules(Git(root))
    return RepoState(root, subdir, head.stdout.strip(), branch)


def _refuse_submodules(git: Git) -> None:
    staged = git.out("ls-files", "--stage")
    if any(line.startswith("160000 ") for line in staged.splitlines()) or ".gitmodules" in git.out(
        "ls-files", "--", ".gitmodules"
    ):
        raise PreflightRefusal(f"{git.directory}: git submodules are unsupported in phase 1")
