"""Builders for real git repositories used as test fixtures (created in tmp_path, never committed)."""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

SINGLE_POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>org.example</groupId>
  <artifactId>demo</artifactId>
  <version>1.0</version>
  <dependencies>
    <dependency><groupId>com.fasterxml.jackson.core</groupId><artifactId>jackson-databind</artifactId><version>{version}</version></dependency>
  </dependencies>
</project>
"""

IDENTITY = {"user.name": "Dev Eloper", "user.email": "dev@example.test"}


def git(repo: Path, *args: str, env: dict | None = None) -> str:
    """Run git as the *developer* would (plain git, hooks enabled), for building fixtures."""
    base = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    base.update({"GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"})
    base.update(env or {})
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=base, check=True)
    return result.stdout.rstrip("\n")


def make_repo(root: Path, files: dict[str, str] | None = None, identity: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    if identity:
        for key, value in IDENTITY.items():
            git(root, "config", key, value)
    if files is not None:
        commit_files(root, files, "initial")
    return root


def commit_files(repo: Path, files: dict[str, str], message: str, date: str = "2026-01-01T00:00:00Z") -> str:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message, env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    return git(repo, "rev-parse", "HEAD")


def install_marker_hooks(repo: Path, marker: Path, hooks=("post-checkout", "pre-commit", "commit-msg")) -> None:
    """Hooks that record that they ran; used to prove giml disables hooks."""
    hooks_dir = repo / ".git" / "hooks"
    for hook in hooks:
        path = hooks_dir / hook
        path.write_text(f"#!/bin/sh\necho {hook} >> '{marker}'\n", encoding="utf-8")
        path.chmod(0o755)


@dataclass(frozen=True)
class Fingerprint:
    """Everything that must stay identical in the developer's checkout (hard rule 2)."""

    head: str
    branch: str
    index: bytes
    files: tuple[tuple[str, str], ...]
    status: str


def fingerprint(repo: Path) -> Fingerprint:
    files = []
    for path in sorted(repo.rglob("*")):
        if ".git" in path.relative_to(repo).parts or not path.is_file():
            continue
        files.append((str(path.relative_to(repo)), hashlib.sha256(path.read_bytes()).hexdigest()))
    index = repo / ".git" / "index"
    return Fingerprint(
        head=git(repo, "rev-parse", "HEAD"),
        branch=git(repo, "symbolic-ref", "-q", "HEAD") if _on_branch(repo) else "",
        index=index.read_bytes() if index.exists() else b"",
        files=tuple(files),
        status=git(repo, "status", "--porcelain=v2", "--branch", "--untracked-files=all"),
    )


def _on_branch(repo: Path) -> bool:
    result = subprocess.run(["git", "-C", str(repo), "symbolic-ref", "-q", "HEAD"], capture_output=True)
    return result.returncode == 0
