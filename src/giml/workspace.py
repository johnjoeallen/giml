"""Run workspace lifecycle: set up a run's result worktree, detect crashed runs, clean up.

M2 stops after workspace setup; planning (M5) will continue from the result worktree.
"""

from __future__ import annotations

import datetime
import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from giml.core.model import ProjectRecord, RunRecord
from giml.git.lock import LockHeld, ProjectLock, is_locked
from giml.git.preflight import RepoState, locate, preflight, project_key
from giml.git.rewind import RewindTarget, apply_rewind, resolve_rewind
from giml.git.runner import Git
from giml.git.worktrees import WorktreeManager, remove_worktree, require_identity, result_branch
from giml.maven.project import discover_reactor
from giml.store.sqlite_store import SqliteStateStore

STOP_PLANNING_NOT_IMPLEMENTED = "planning_not_implemented"
STOP_SETUP_FAILED = "setup_failed"
STOP_CRASHED = "crashed"


@dataclass
class Workspace:
    run_id: str
    repo: RepoState
    branch: str
    worktree: Path
    rewind: RewindTarget | None
    crashed_runs: list[RunRecord] = field(default_factory=list)


def new_run_id(now: datetime.datetime) -> str:
    # The random suffix only distinguishes runs; it never influences planning.
    return f"{now.astimezone(datetime.UTC).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


def remote_url_hash(repo_root: Path) -> str | None:
    result = Git(repo_root).run("remote", "get-url", "origin", check=False)
    url = result.stdout.strip()
    return hashlib.sha256(url.encode()).hexdigest() if result.returncode == 0 and url else None


def _worktree_root(state_dir: Path, key: str) -> Path:
    return state_dir / "worktrees" / key


def mark_crashed(store: SqliteStateStore, key: str, now: datetime.datetime) -> list[RunRecord]:
    """Unfinished runs of a project whose lock we hold can only be crashed runs."""
    crashed = store.list_runs(key, unfinished_only=True)
    for run in crashed:
        store.finish_run(run.id, now, STOP_CRASHED)
    return crashed


def set_up(
    path: Path,
    state_dir: Path,
    store: SqliteStateStore,
    clock: Callable[[], datetime.datetime],
    allow_detached: bool = False,
    rewind_to: str | None = None,
) -> Workspace:
    """Preflight, lock, record the run and create its result worktree (plus rewind commit)."""
    repo = preflight(path, allow_detached)
    reactor = discover_reactor(repo.project_dir)
    rewind = resolve_rewind(repo, rewind_to, reactor) if rewind_to else None
    require_identity(repo.root)

    with ProjectLock(state_dir, repo.project_key):
        now = clock()
        crashed = mark_crashed(store, repo.project_key, now)
        run_id = new_run_id(now)
        branch = result_branch(repo.base_sha, now, rewind is not None)
        manager = WorktreeManager(repo, state_dir, run_id)
        store.save_project(ProjectRecord(repo.project_key, repo.project_dir, remote_url_hash(repo.root), now))
        # Recorded before the worktree exists, so a crash during setup is still detectable.
        store.start_run(RunRecord(run_id, repo.project_key, repo.base_sha, branch, manager.root / run_id, now,
                                  rewind_from_sha=rewind.sha if rewind else None))  # fmt: skip
        try:
            worktree = manager.create_result(branch)
            if rewind is not None:
                apply_rewind(worktree, rewind)
        except BaseException:
            store.finish_run(run_id, clock(), STOP_SETUP_FAILED)
            raise
        store.finish_run(run_id, clock(), STOP_PLANNING_NOT_IMPLEMENTED)
    return Workspace(run_id, repo, branch, worktree, rewind, crashed)


@dataclass(frozen=True)
class StaleRun:
    run: RunRecord
    project_path: Path


def stale_runs(state_dir: Path, store: SqliteStateStore) -> list[StaleRun]:
    """Unfinished runs whose project lock is free: giml died during those runs."""
    stale = []
    for run in store.list_runs(unfinished_only=True):
        if not is_locked(state_dir, run.project_id):
            # run.project_id is a foreign key, so the project always exists.
            stale.append(StaleRun(run, store.get_project(run.project_id).path))
    return stale


def clean(
    state_dir: Path,
    store: SqliteStateStore,
    clock: Callable[[], datetime.datetime],
    path: Path | None,
    all_projects: bool,
    branches: bool,
) -> list[str]:
    """Remove giml's worktrees (and optionally result branches) recorded for the chosen projects.

    Only paths under <state>/worktrees and branches recorded in the run table are touched.
    Returns one line per action for the caller to print.
    """
    if all_projects:
        keys = [p.id for p in store.list_projects()]
    else:
        root, subdir = locate(path or Path.cwd())
        keys = [project_key(root, subdir)]
    actions: list[str] = []
    for key in keys:
        project = store.get_project(key)
        if project is None:
            actions.append(f"{key}: no giml runs recorded")
            continue
        try:
            lock = ProjectLock(state_dir, key)
            lock.acquire()
        except LockHeld as exc:
            raise LockHeld(f"cannot clean {project.path} while a run is active: {exc}") from None
        try:
            actions += _clean_project(state_dir, store, clock, project, branches)
        finally:
            lock.release()
    return actions


def _clean_project(state_dir, store, clock, project: ProjectRecord, branches: bool) -> list[str]:
    if not project.path.is_dir():
        return [f"{project.id}: skipped, {project.path} no longer exists"]
    mark_crashed(store, project.id, clock())
    root = _worktree_root(state_dir, project.id)
    git = Git(project.path)
    actions = []
    for run in store.list_runs(project.id):
        for worktree in [run.worktree_path, *sorted(root.glob(f"{run.id}-trial-*"))]:
            if worktree.exists():
                remove_worktree(project.path, root, worktree)
                actions.append(f"removed worktree {worktree}")
        if branches:
            if git.run("rev-parse", "--verify", "-q", f"refs/heads/{run.branch}", check=False).returncode != 0:
                continue
            deleted = git.run("branch", "-D", run.branch, check=False)
            if deleted.returncode == 0:
                actions.append(f"deleted branch {run.branch}")
            else:
                actions.append(f"kept branch {run.branch}: {deleted.stderr.strip()}")
    git.run("worktree", "prune")
    return actions
