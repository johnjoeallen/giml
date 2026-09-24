import os

import pytest

from giml.git.runner import ForbiddenGitCommand, Git, GitError, check_allowed
from tests.git.repo_helpers import git, install_marker_hooks, make_repo


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo", {"README": "hi\n"})


@pytest.mark.parametrize(
    "args",
    [("push",), ("push", "origin", "main"), ("push", "--force"), ("push", "--dry-run")],
)
def test_push_is_always_forbidden(repo, args):
    with pytest.raises(ForbiddenGitCommand, match="git push is never allowed"):
        Git(repo).run(*args)


@pytest.mark.parametrize(
    ("subcommand", "args", "message"),
    [
        ("-c", ("alias.x=push", "x"), "not on giml's allowlist"),
        ("send-email", (), "not on giml's allowlist"),
        ("fetch", (), "not on giml's allowlist"),
        ("remote", ("set-url", "origin", "x"), "only allowed as: get-url"),
        ("remote", (), "only allowed as: get-url"),
        ("config", ("user.name", "x"), "only allowed as: --get"),
    ],
)
def test_non_allowlisted_commands_are_rejected_before_running(repo, subcommand, args, message):
    with pytest.raises(ForbiddenGitCommand, match=message):
        Git(repo).run(subcommand, *args)


def test_allowlisted_command_runs_and_returns_output(repo):
    assert Git(repo).out("rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert Git(repo).out("config", "--get", "user.name") == "Dev Eloper"


def test_failure_raises_git_error_with_details(repo):
    with pytest.raises(GitError) as exc:
        Git(repo).run("rev-parse", "--verify", "no-such-ref")
    assert exc.value.returncode != 0
    assert exc.value.args_ == ("rev-parse", "--verify", "no-such-ref")
    assert "fatal" in exc.value.stderr
    assert str(exc.value).startswith("git rev-parse --verify no-such-ref failed (")


def test_check_false_returns_result_instead_of_raising(repo):
    result = Git(repo).run("rev-parse", "--verify", "-q", "no-such-ref", check=False)
    assert result.returncode == 1


def test_git_environment_variables_cannot_redirect_git(repo, tmp_path, monkeypatch):
    other = make_repo(tmp_path / "other", {"OTHER": "x\n"})
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    assert Git(repo).out("ls-files") == "README"


def test_extra_environment_is_applied_after_scrubbing(repo, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Leaked From Shell")
    (repo / "README").write_text("changed\n")
    g = Git(repo)
    g.run("add", "README")
    g.run("commit", "-q", "-m", "change", env={"GIT_AUTHOR_NAME": "Explicit", "GIT_AUTHOR_EMAIL": "e@x"})
    assert g.out("log", "-1", "--format=%an <%ae>") == "Explicit <e@x>"


def test_hooks_never_run(repo, tmp_path):
    marker = tmp_path / "hooks-ran"
    install_marker_hooks(repo, marker)
    (repo / "README").write_text("changed\n")
    g = Git(repo)
    g.run("add", "README")
    g.run("commit", "-q", "-m", "change", env={"GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x",
                                              "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@x"})  # fmt: skip
    g.run("worktree", "add", "-q", "--detach", str(tmp_path / "wt"), "HEAD")
    assert not marker.exists()
    # Sanity check: the same hooks do run for plain git.
    git(repo, "worktree", "add", "-q", "--detach", str(tmp_path / "wt2"), "HEAD")
    assert marker.read_text().strip() == "post-checkout"


def test_check_allowed_accepts_any_arguments_for_unrestricted_subcommands():
    check_allowed("worktree", ("add", "x"))
    check_allowed("status", ())


def test_output_is_not_localised(repo, monkeypatch):
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    result = Git(repo).run("rev-parse", "--verify", "nope", check=False)
    assert "fatal: Needed a single revision" in result.stderr
    assert os.environ["LC_ALL"] == "de_DE.UTF-8"  # the caller's environment is not modified
