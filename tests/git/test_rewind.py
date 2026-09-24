import pytest

from giml.git.preflight import preflight
from giml.git.rewind import RewindError, apply_rewind, pom_path, resolve_rewind
from giml.git.worktrees import WorktreeManager, developer_identity
from tests.git.repo_helpers import SINGLE_POM, commit_files, fingerprint, git, make_repo


@pytest.fixture
def history(tmp_path):
    """pom at 2.9.8 (old) -> README edit -> pom at 2.18.4 (base)."""
    repo = make_repo(tmp_path / "repo")
    old = commit_files(repo, {"pom.xml": SINGLE_POM.format(version="2.9.8"), "src/A.java": "class A {}\n"}, "old",
                       date="2025-01-02T03:04:05Z")  # fmt: skip
    commit_files(repo, {"README": "docs\n"}, "docs")
    commit_files(repo, {"pom.xml": SINGLE_POM.format(version="2.18.4"), "src/A.java": "class A { int x; }\n"}, "new")
    return repo, old


def workspace(repo, tmp_path):
    state = preflight(repo)
    manager = WorktreeManager(state, tmp_path / "state", "run-1")
    return state, manager.create_result("giml/rewind/x/y")


def test_rewind_commits_only_the_old_pom(history, tmp_path):
    repo, old = history
    before = fingerprint(repo)
    state, worktree = workspace(repo, tmp_path)
    target = resolve_rewind(state, "HEAD~2")
    assert target.sha == old and target.pom_path == "pom.xml"
    assert target.committed_at == "2025-01-02T03:04:05Z"
    sha = apply_rewind(worktree, target, developer_identity(repo))

    assert git(worktree, "diff", "--name-only", f"{state.base_sha}..{sha}") == "pom.xml"
    assert (worktree / "pom.xml").read_text() == SINGLE_POM.format(version="2.9.8")
    assert (worktree / "src" / "A.java").read_text() == "class A { int x; }\n"  # source stays at base
    message = git(worktree, "log", "-1", "--format=%B")
    assert message.startswith(f"[giml-rewind] pom.xml from {old[:7]} (synthetic)\n")
    assert f"to its content at {old} (committed 2025-01-02T03:04:05Z)" in message
    assert fingerprint(repo) == before


def test_rewind_preserves_bytes_including_crlf(tmp_path):
    repo = make_repo(tmp_path / "repo")
    crlf = SINGLE_POM.format(version="1.0").replace("\n", "\r\n")
    (repo / "pom.xml").write_bytes(crlf.encode())
    git(repo, "add", "pom.xml")
    git(repo, "commit", "-q", "-m", "crlf")
    commit_files(repo, {"pom.xml": SINGLE_POM.format(version="2.0")}, "lf")
    state, worktree = workspace(repo, tmp_path)
    apply_rewind(worktree, resolve_rewind(state, "HEAD~1"), developer_identity(repo))
    assert (worktree / "pom.xml").read_bytes() == crlf.encode()


def test_rewind_in_subdirectory_project(tmp_path):
    repo = make_repo(tmp_path / "mono")
    commit_files(repo, {"svc/pom.xml": SINGLE_POM.format(version="1.0"), "other/pom.xml": "x"}, "old")
    commit_files(repo, {"svc/pom.xml": SINGLE_POM.format(version="2.0"), "other/pom.xml": "y"}, "new")
    state = preflight(repo / "svc")
    assert pom_path(state) == "svc/pom.xml"
    worktree = WorktreeManager(state, tmp_path / "state", "run-1").create_result("giml/rewind/a/b")
    sha = apply_rewind(worktree, resolve_rewind(state, "HEAD~1"), developer_identity(repo))
    assert git(worktree, "diff", "--name-only", f"{state.base_sha}..{sha}") == "svc/pom.xml"


def test_unknown_commit(history):
    repo, _ = history
    with pytest.raises(RewindError, match="--rewind-to nope: does not resolve to a commit"):
        resolve_rewind(preflight(repo), "nope")


def test_non_ancestor_is_rejected(history):
    repo, _ = history
    git(repo, "checkout", "-q", "-b", "side", "HEAD~1")
    side = commit_files(repo, {"pom.xml": SINGLE_POM.format(version="3.0")}, "side")
    git(repo, "checkout", "-q", "main")
    with pytest.raises(RewindError, match=rf"{side[:7]} is not an ancestor of the base commit"):
        resolve_rewind(preflight(repo), side)


def test_commit_without_pom_is_rejected(tmp_path):
    repo = make_repo(tmp_path / "repo", {"README": "no pom yet\n"})
    commit_files(repo, {"pom.xml": SINGLE_POM.format(version="1.0")}, "add pom")
    with pytest.raises(RewindError, match="has no pom.xml"):
        resolve_rewind(preflight(repo), "HEAD~1")


@pytest.mark.parametrize("commit", ["HEAD", "HEAD~1"], ids=["base-itself", "unchanged-pom"])
def test_identical_pom_is_rejected_before_any_worktree_exists(history, commit):
    repo, _ = history
    commit_files(repo, {"README": "later docs\n"}, "docs only: HEAD~1 has the same pom.xml as HEAD")
    with pytest.raises(RewindError, match="pom.xml at .* is identical to the base commit's"):
        resolve_rewind(preflight(repo), commit)
