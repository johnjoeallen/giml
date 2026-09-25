"""End-to-end tests for `giml plan` (M2 stub), `giml clean` and crashed-run reporting (spec 5, M2)."""

import datetime
import os
import re
import signal
import subprocess
import sys

import pytest

from giml.maven.runner import MavenResult
from giml.cli import Environment, ExitCode, main
from giml.git.lock import ProjectLock
from giml.git.preflight import preflight
from giml.store.sqlite_store import SqliteStateStore
from tests.git.repo_helpers import SINGLE_POM, commit_files, fingerprint, git, install_marker_hooks, make_repo

UTC = datetime.UTC
T0 = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime.datetime:
        self.now += datetime.timedelta(seconds=1)
        return self.now


@pytest.fixture
def repo(tmp_path):
    repo = make_repo(tmp_path / "repo")
    commit_files(repo, {"pom.xml": SINGLE_POM.format(version="2.9.8"), "src/App.java": "class App {}\n"}, "old")
    commit_files(repo, {"pom.xml": SINGLE_POM.format(version="2.18.4")}, "upgrade jackson")
    git(repo, "remote", "add", "origin", "https://user:s3cret-token@git.example.test/team/repo.git")
    return repo


class PassingMaven:
    """Every Maven run succeeds: these tests are about the workspace, not about builds."""

    def __call__(self, project, args, log, timeout, env):
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("[INFO] BUILD SUCCESS\n")
        return MavenResult(tuple(args), 0, 0.1, log, False)


@pytest.fixture
def cli(tmp_path):
    state = tmp_path / "state"
    env = Environment(clock=Clock(), environ={}, maven=PassingMaven())

    def run(*args):
        return main(["--state-dir", str(state), *map(str, args)], env)

    run.state = state
    run.env = env
    return run


def store(cli):
    return SqliteStateStore(cli.state / "state.db")


def output_value(out: str, key: str) -> str:
    return next(line.split(": ", 1)[1] for line in out.splitlines() if line.startswith(f"{key}: "))


def test_plan_sets_up_workspace_without_touching_the_checkout(repo, cli, capsys):
    before = fingerprint(repo)
    assert cli("plan", repo) == ExitCode.SUCCESS
    out = capsys.readouterr().out
    branch, worktree = output_value(out, "branch"), output_value(out, "worktree")
    base = git(repo, "rev-parse", "HEAD")
    assert branch.startswith(f"giml/{base[:7]}/20260924T1200")
    assert git(repo, "rev-parse", f"{branch}~1") == base  # the [giml-setup] commit sits directly on the base
    assert git(repo, "log", "-1", "--format=%s", branch).startswith("[giml-setup]")
    assert git(worktree, "symbolic-ref", "--short", "HEAD") == branch
    assert "stopped after baseline verification: planning arrives in milestone 5" in out
    assert f"review: git diff {base[:7]}..{branch}" in out
    assert fingerprint(repo) == before

    with store(cli) as s:
        [run] = s.list_runs()
        [project] = s.list_projects()
    assert (run.branch, run.stop_reason, run.rewind_from_sha) == (branch, "planning_not_implemented", None)
    assert run.finished_at is not None and str(run.worktree_path) == worktree
    assert project.path == repo.resolve()


def test_origin_url_is_stored_only_as_a_hash(repo, cli):
    assert cli("plan", repo) == ExitCode.SUCCESS
    with store(cli) as s:
        [project] = s.list_projects()
    assert len(project.remote_url_hash) == 64
    assert b"s3cret-token" not in (cli.state / "state.db").read_bytes()


def test_plan_with_rewind_commits_only_old_pom_and_runs_no_hooks(repo, cli, tmp_path, capsys):
    marker = tmp_path / "hooks-ran"
    install_marker_hooks(repo, marker)
    before = fingerprint(repo)
    old = git(repo, "rev-parse", "HEAD~1")
    assert cli("plan", repo, "--rewind-to", "HEAD~1") == ExitCode.SUCCESS
    out = capsys.readouterr().out
    branch, worktree = output_value(out, "branch"), output_value(out, "worktree")
    base = git(repo, "rev-parse", "HEAD")
    assert branch.startswith(f"giml/rewind/{base[:7]}/")
    assert git(repo, "diff", "--name-only", f"{base}..{branch}") == "pom.xml"
    assert git(repo, "log", "-1", "--format=%s", f"{branch}~1") == f"[giml-rewind] pom.xml from {old[:7]} (synthetic)"
    assert git(repo, "log", "-1", "--format=%s", branch).startswith("[giml-setup]")  # follows the rewind commit (spec 5.3 step 5)
    assert f"rewound 1 pom.xml file(s) to {old[:7]}" in out
    assert "2.9.8" in (tmp_path / worktree / "pom.xml").read_text()
    assert not marker.exists()
    assert fingerprint(repo) == before
    with store(cli) as s:
        assert s.list_runs()[0].rewind_from_sha == old


@pytest.mark.parametrize(
    ("setup", "args", "code", "message"),
    [
        (lambda r: (r / "stray.txt").write_text("x"), (), ExitCode.PREFLIGHT_REFUSAL, "giml: refused: .*not clean"),
        (lambda r: git(r, "checkout", "-q", "--detach"), (), ExitCode.PREFLIGHT_REFUSAL, "HEAD is detached"),
        (lambda r: commit_files(r, {"pom.xml": "<project><modules><module>ghost</module></modules></project>"}, "mm"),
         (), ExitCode.CONFIGURATION, "ghost/pom.xml: declared module has no pom.xml"),
        (lambda r: commit_files(r, {"pom.xml": "<project><modules><module>../x</module></modules></project>"}, "out"),
         (), ExitCode.INELIGIBLE, "giml: unsupported: .*module outside the project directory"),
        (lambda r: (git(r, "rm", "-q", "pom.xml"), git(r, "commit", "-q", "-m", "no pom")), (), ExitCode.CONFIGURATION,
         "no pom.xml"),
        (lambda r: None, ("--rewind-to", "nope"), ExitCode.CONFIGURATION, "does not resolve to a commit"),
        (lambda r: None, ("--rewind-to", "HEAD"), ExitCode.CONFIGURATION, "identical to the base commit's"),
        (lambda r: (git(r, "config", "--unset", "user.email")), (), ExitCode.CONFIGURATION, "user.email is not configured"),
    ],
    ids=["dirty", "detached", "missing-module", "module-outside", "no-pom", "bad-rewind", "identical-rewind", "no-identity"],
)  # fmt: skip
def test_refusals_leave_no_trace(repo, cli, capsys, setup, args, code, message):
    setup(repo)
    before = fingerprint(repo)
    assert cli("plan", repo, *args) == code
    assert re.search(message, capsys.readouterr().err)
    assert fingerprint(repo) == before
    assert "giml/" not in git(repo, "branch", "--list")
    assert not (cli.state / "worktrees").exists()


def test_detached_head_allowed_with_flag(repo, cli):
    git(repo, "checkout", "-q", "--detach")
    assert cli("plan", repo, "--allow-detached") == ExitCode.SUCCESS


def test_lock_held_by_another_run_is_refused(repo, cli, capsys):
    with ProjectLock(cli.state, preflight(repo).project_key):
        assert cli("plan", repo) == ExitCode.PREFLIGHT_REFUSAL
    assert f"another giml run (pid {os.getpid()})" in capsys.readouterr().err


def test_setup_failure_is_recorded_and_branch_never_overwritten(repo, cli, capsys):
    base = git(repo, "rev-parse", "HEAD")
    taken = f"giml/{base[:7]}/20260924T120001Z"  # the Clock's first tick
    git(repo, "branch", taken, "HEAD~1")
    assert cli("plan", repo) == ExitCode.INFRASTRUCTURE
    assert f"branch {taken} already exists" in capsys.readouterr().err
    assert git(repo, "rev-parse", taken) == git(repo, "rev-parse", "HEAD~1")
    with store(cli) as s:
        [run] = s.list_runs()
    assert run.stop_reason == "setup_failed"


CRASHING_RUN = """
import os, signal, sys, datetime
from pathlib import Path
from giml import workspace
from giml.git.worktrees import WorktreeManager
from giml.store.sqlite_store import SqliteStateStore

original = WorktreeManager.create_result
def create_then_die(self, branch):
    path = original(self, branch)
    os.kill(os.getpid(), signal.SIGKILL)  # crash after the worktree exists, before the run finishes
WorktreeManager.create_result = create_then_die
with SqliteStateStore(Path(sys.argv[2]) / "state.db") as store:
    workspace.set_up(Path(sys.argv[1]), Path(sys.argv[2]), store, lambda: datetime.datetime.now(datetime.UTC))
"""


def test_crashed_run_is_detected_reported_and_cleanable(repo, cli, capsys):
    before = fingerprint(repo)
    child = subprocess.run([sys.executable, "-c", CRASHING_RUN, str(repo), str(cli.state)], capture_output=True)
    assert child.returncode == -signal.SIGKILL
    with store(cli) as s:
        [crashed] = s.list_runs(unfinished_only=True)
    assert crashed.worktree_path.exists()

    assert cli("status") == ExitCode.SUCCESS
    status = capsys.readouterr().out
    assert "crashed runs: 1 (remove with `giml clean <project>`)" in status
    assert f"  {crashed.id}  {repo.resolve()}  started " in status

    assert cli("plan", repo) == ExitCode.SUCCESS
    err = capsys.readouterr().err
    assert f"warning: earlier run {crashed.id} never finished (crashed)" in err
    with store(cli) as s:
        assert s.list_runs(unfinished_only=True) == []
        assert next(r for r in s.list_runs() if r.id == crashed.id).stop_reason == "crashed"
    assert cli("status") == ExitCode.SUCCESS
    assert "crashed runs: 0" in capsys.readouterr().out

    assert cli("clean", repo) == ExitCode.SUCCESS
    assert f"removed worktree {crashed.worktree_path}" in capsys.readouterr().out
    assert not crashed.worktree_path.exists()
    assert fingerprint(repo) == before


def test_status_does_not_report_a_run_that_is_still_active(repo, cli, capsys):
    with store(cli) as s:
        assert cli("plan", repo) == ExitCode.SUCCESS
        run = s.list_runs()[0]
        s._conn.execute("UPDATE run SET finished_at = NULL")  # looks unfinished...
    with ProjectLock(cli.state, run.project_id):  # ...but its project is locked, so it is active
        assert cli("status") == ExitCode.SUCCESS
    assert "crashed runs: 0" in capsys.readouterr().out


def test_clean_removes_worktrees_and_only_recorded_branches(repo, cli, capsys):
    assert cli("plan", repo) == ExitCode.SUCCESS
    assert cli("plan", repo, "--rewind-to", "HEAD~1") == ExitCode.SUCCESS
    capsys.readouterr()
    git(repo, "branch", "giml/manual/keep-me")
    before = fingerprint(repo)
    with store(cli) as s:
        runs = s.list_runs()

    assert cli("clean", repo) == ExitCode.SUCCESS
    out = capsys.readouterr().out.splitlines()
    assert out == [f"removed worktree {r.worktree_path}" for r in runs]
    assert all(git(repo, "rev-parse", "--verify", "-q", r.branch) for r in runs)  # branches kept

    assert cli("clean", repo, "--branches") == ExitCode.SUCCESS
    assert capsys.readouterr().out.splitlines() == [f"deleted branch {r.branch}" for r in runs]
    assert git(repo, "branch", "--list", "giml/*") == "  giml/manual/keep-me"
    assert fingerprint(repo) == before


def test_clean_never_deletes_a_checked_out_branch(repo, cli, capsys):
    assert cli("plan", repo) == ExitCode.SUCCESS
    branch = output_value(capsys.readouterr().out, "branch")
    assert cli("clean", repo) == ExitCode.SUCCESS
    git(repo, "checkout", "-q", branch)
    capsys.readouterr()
    assert cli("clean", repo, "--branches") == ExitCode.SUCCESS
    assert capsys.readouterr().out.startswith(f"kept branch {branch}: ")
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == branch


def test_clean_all_and_edge_cases(repo, cli, tmp_path, capsys):
    other = make_repo(tmp_path / "other", {"pom.xml": SINGLE_POM.format(version="1.0")})
    assert cli("plan", repo) == ExitCode.SUCCESS
    assert cli("plan", other) == ExitCode.SUCCESS
    capsys.readouterr()
    assert cli("clean", "--all") == ExitCode.SUCCESS
    assert len(capsys.readouterr().out.splitlines()) == 2

    assert cli("clean", repo, "--all") == ExitCode.CONFIGURATION
    assert "pass a project path or --all, not both" in capsys.readouterr().err

    unknown = make_repo(tmp_path / "unknown", {"pom.xml": "x"})
    assert cli("clean", unknown) == ExitCode.SUCCESS
    assert "no giml runs recorded" in capsys.readouterr().out

    with ProjectLock(cli.state, preflight(repo).project_key):
        assert cli("clean", repo) == ExitCode.PREFLIGHT_REFUSAL
    assert "cannot clean" in capsys.readouterr().err

    subprocess.run(["rm", "-rf", str(other)], check=True)
    assert cli("clean", "--all") == ExitCode.SUCCESS
    assert "no longer exists" in capsys.readouterr().out


def test_clean_defaults_to_current_directory(repo, cli, monkeypatch, capsys):
    assert cli("plan", repo) == ExitCode.SUCCESS
    capsys.readouterr()
    monkeypatch.chdir(repo)
    assert cli("clean") == ExitCode.SUCCESS
    assert capsys.readouterr().out.startswith("removed worktree ")


def test_push_cannot_happen_through_any_giml_command(repo, cli, monkeypatch):
    calls = []
    real_run = subprocess.run

    def spy(command, *args, **kwargs):
        calls.append(command)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    assert cli("plan", repo, "--rewind-to", "HEAD~1") == ExitCode.SUCCESS
    assert cli("clean", repo, "--branches") == ExitCode.SUCCESS
    git_calls = [c for c in calls if c and c[0] == "git"]
    assert git_calls and not any("push" in c for c in git_calls)


def test_run_ids_are_utc_timestamps_with_a_random_suffix():
    from giml.workspace import new_run_id

    plus_two = datetime.timezone(datetime.timedelta(hours=2))
    run_id = new_run_id(datetime.datetime(2026, 9, 24, 14, 5, 6, tzinfo=plus_two))
    assert re.fullmatch(r"20260924T120506Z-[0-9a-f]{6}", run_id)


def test_project_without_origin_has_no_remote_hash(tmp_path, cli):
    repo = make_repo(tmp_path / "local", {"pom.xml": SINGLE_POM.format(version="1.0")})
    assert cli("plan", repo) == ExitCode.SUCCESS
    with store(cli) as s:
        assert s.list_projects()[0].remote_url_hash is None


def test_planning_one_project_never_marks_another_projects_active_run(repo, cli, tmp_path):
    other = make_repo(tmp_path / "other", {"pom.xml": SINGLE_POM.format(version="1.0")})
    assert cli("plan", other) == ExitCode.SUCCESS
    other_key = preflight(other).project_key
    with store(cli) as s:
        s._conn.execute("UPDATE run SET finished_at = NULL, stop_reason = NULL")  # other's run is "active"
    with ProjectLock(cli.state, other_key):
        assert cli("plan", repo) == ExitCode.SUCCESS
    with store(cli) as s:
        [other_run] = s.list_runs(other_key)
    assert other_run.finished_at is None and other_run.stop_reason is None


def test_clean_marks_crashed_runs_itself(repo, cli):
    assert cli("plan", repo) == ExitCode.SUCCESS
    with store(cli) as s:
        s._conn.execute("UPDATE run SET finished_at = NULL, stop_reason = NULL")
    assert cli("clean", repo) == ExitCode.SUCCESS
    with store(cli) as s:
        assert [r.stop_reason for r in s.list_runs()] == ["crashed"]


def test_clean_branches_continues_past_already_deleted_ones(repo, cli, capsys):
    assert cli("plan", repo) == ExitCode.SUCCESS
    assert cli("plan", repo) == ExitCode.SUCCESS
    with store(cli) as s:
        first, second = s.list_runs()
    assert cli("clean", repo) == ExitCode.SUCCESS
    git(repo, "branch", "-D", first.branch)
    capsys.readouterr()
    assert cli("clean", repo, "--branches") == ExitCode.SUCCESS
    assert capsys.readouterr().out.splitlines() == [f"deleted branch {second.branch}"]


def test_lfs_content_is_never_fetched_in_giml_worktrees(tmp_path, cli, capsys):
    marker = tmp_path / "smudge-ran"
    repo = make_repo(tmp_path / "lfs")
    git(repo, "config", "filter.lfs.clean", "cat")  # identity clean: the stored blob is the file
    commit_files(repo, {"pom.xml": SINGLE_POM.format(version="1.0"), ".gitattributes": "*.bin filter=lfs\n",
                        "demo/video.bin": "version https://git-lfs.github.com/spec/v1\npointer\n"}, "lfs")  # fmt: skip
    commit_files(repo, {"pom.xml": SINGLE_POM.format(version="2.0")}, "bump")
    # A required smudge filter that fails and leaves a marker, standing in for an LFS download.
    git(repo, "config", "filter.lfs.smudge", f"sh -c 'echo ran >> {marker}; exit 1'")
    git(repo, "config", "filter.lfs.required", "true")
    sanity = subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(tmp_path / "plain")],
                            capture_output=True)  # fmt: skip
    assert sanity.returncode != 0 and marker.exists()  # plain git would try (and fail) to fetch
    marker.unlink()
    before = fingerprint(repo)

    assert cli("plan", repo, "--rewind-to", "HEAD~1") == ExitCode.SUCCESS
    worktree = output_value(capsys.readouterr().out, "worktree")
    assert (tmp_path / worktree / "demo" / "video.bin").read_text().startswith("version https://git-lfs")
    assert not marker.exists()
    assert cli("clean", repo, "--branches") == ExitCode.SUCCESS
    assert not marker.exists()
    assert fingerprint(repo) == before
