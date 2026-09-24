import datetime
import subprocess

import pytest

from giml import __version__
from giml.git.preflight import preflight
from giml.git.worktrees import (
    BranchExistsError,
    ForeignWorktreeError,
    IdentityError,
    WorktreeManager,
    commit_all,
    developer_identity,
    result_branch,
)
from tests.git.repo_helpers import SINGLE_POM, commit_files, fingerprint, git, install_marker_hooks, make_repo

WHEN = datetime.datetime(2026, 9, 24, 12, 30, 5, tzinfo=datetime.UTC)


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo", {"pom.xml": SINGLE_POM.format(version="2.9.8")})


@pytest.fixture
def manager(repo, tmp_path):
    return WorktreeManager(preflight(repo), tmp_path / "state", "20260924T123005Z-abc123")


def test_branch_names():
    sha = "0123456789abcdef" * 2 + "01234567"
    assert result_branch(sha, WHEN, rewind=False) == "giml/0123456/20260924T123005Z"
    assert result_branch(sha, WHEN, rewind=True) == "giml/rewind/0123456/20260924T123005Z"
    plus_two = datetime.timezone(datetime.timedelta(hours=2))
    assert result_branch(sha, WHEN.astimezone(plus_two), rewind=False).endswith("/20260924T123005Z")


def test_result_worktree_is_at_base_on_new_untracked_branch(repo, manager, tmp_path):
    before = fingerprint(repo)
    path = manager.create_result("giml/abc/20260924T123005Z")
    assert path == tmp_path / "state" / "worktrees" / manager.repo.project_key / "20260924T123005Z-abc123"
    assert git(path, "rev-parse", "HEAD") == manager.repo.base_sha
    assert git(path, "symbolic-ref", "--short", "HEAD") == "giml/abc/20260924T123005Z"
    upstream = subprocess.run(["git", "-C", str(path), "rev-parse", "--abbrev-ref", "@{upstream}"], capture_output=True)
    assert upstream.returncode != 0  # no tracking: nothing to push to
    assert fingerprint(repo) == before


def test_existing_branch_is_never_overwritten(repo, manager):
    git(repo, "branch", "giml/abc/taken")
    before = git(repo, "rev-parse", "giml/abc/taken")
    with pytest.raises(BranchExistsError, match="giml/abc/taken already exists"):
        manager.create_result("giml/abc/taken")
    assert git(repo, "rev-parse", "giml/abc/taken") == before


def test_trial_worktree_lifecycle(repo, manager):
    before = fingerprint(repo)
    trial = manager.create_trial(manager.repo.base_sha, 1)
    assert trial.name == "20260924T123005Z-abc123-trial-1"
    assert git(trial, "rev-parse", "HEAD") == manager.repo.base_sha
    manager.remove(trial)
    assert not trial.exists()
    assert str(trial) not in git(repo, "worktree", "list", "--porcelain")
    assert fingerprint(repo) == before


def test_removing_an_already_deleted_worktree_prunes_it(repo, manager):
    trial = manager.create_trial(manager.repo.base_sha, 2)
    subprocess.run(["rm", "-rf", str(trial)], check=True)
    manager.remove(trial)
    assert str(trial) not in git(repo, "worktree", "list", "--porcelain")


@pytest.mark.parametrize("target", ["repo", "root", "outside"])
def test_foreign_paths_are_never_removed(repo, manager, tmp_path, target):
    path = {"repo": repo, "root": manager.root, "outside": tmp_path / "elsewhere"}[target]
    with pytest.raises(ForeignWorktreeError, match="is not a giml worktree"):
        manager.remove(path)
    assert repo.exists()


def test_commit_uses_developer_identity_trailer_and_no_hooks(repo, manager, tmp_path):
    marker = tmp_path / "hooks-ran"
    install_marker_hooks(repo, marker)
    path = manager.create_result("giml/abc/x")
    (path / "pom.xml").write_text(SINGLE_POM.format(version="2.10.0"))
    sha = commit_all(path, "[giml] bump jackson", developer_identity(repo))
    assert git(path, "log", "-1", "--format=%an <%ae>|%cn <%ce>") == "Dev Eloper <dev@example.test>|Dev Eloper <dev@example.test>"
    assert git(path, "log", "-1", "--format=%B").rstrip() == f"[giml] bump jackson\n\nGenerated-by: giml {__version__}"
    assert sha == git(path, "rev-parse", "HEAD")
    assert not marker.exists()
    assert git(repo, "rev-parse", "HEAD") == manager.repo.base_sha  # developer's branch did not move


def test_commit_is_never_signed_even_if_configured(repo, manager):
    git(repo, "config", "commit.gpgsign", "true")
    git(repo, "config", "gpg.program", "/bin/false")  # signing would fail loudly
    path = manager.create_result("giml/abc/signed")
    (path / "pom.xml").write_text("changed")
    commit_all(path, "change", developer_identity(repo))


@pytest.mark.parametrize("missing", ["user.name", "user.email"])
def test_missing_identity_is_reported(tmp_path, missing):
    repo = make_repo(tmp_path / "anon", identity=False)
    other = "user.email" if missing == "user.name" else "user.name"
    git(repo, "config", other, "someone")
    with pytest.raises(IdentityError, match=f"git {missing} is not configured"):
        developer_identity(repo)


def test_commits_on_developer_branch_after_worktree_do_not_interfere(repo, manager):
    path = manager.create_result("giml/abc/y")
    commit_files(repo, {"README": "later\n"}, "developer keeps working")
    assert git(path, "rev-parse", "HEAD") == manager.repo.base_sha
