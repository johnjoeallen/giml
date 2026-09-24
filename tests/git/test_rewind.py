import pytest

from giml.git.preflight import preflight
from giml.git.rewind import RewindError, apply_rewind, pom_path, resolve_rewind
from giml.git.worktrees import WorktreeManager
from giml.maven.project import discover_reactor
from tests.git.repo_helpers import SINGLE_POM, commit_files, fingerprint, git, make_repo


def reactor(state):
    return discover_reactor(state.project_dir)


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
    target = resolve_rewind(state, "HEAD~2", reactor(state))
    assert target.sha == old and (target.pom_paths, target.kept_paths) == (("pom.xml",), ())
    assert target.committed_at == "2025-01-02T03:04:05Z"
    sha = apply_rewind(worktree, target)

    assert git(worktree, "diff", "--name-only", f"{state.base_sha}..{sha}") == "pom.xml"
    assert (worktree / "pom.xml").read_text() == SINGLE_POM.format(version="2.9.8")
    assert (worktree / "src" / "A.java").read_text() == "class A { int x; }\n"  # source stays at base
    message = git(worktree, "log", "-1", "--format=%B")
    assert message.startswith(f"[giml-rewind] pom.xml from {old[:7]} (synthetic)\n")
    assert f"Rewound to the content at {old} (committed 2025-01-02T03:04:05Z):\n  pom.xml\n" in message
    assert "Kept at base" not in message
    assert fingerprint(repo) == before


def test_rewind_preserves_bytes_including_crlf(tmp_path):
    repo = make_repo(tmp_path / "repo")
    crlf = SINGLE_POM.format(version="1.0").replace("\n", "\r\n")
    (repo / "pom.xml").write_bytes(crlf.encode())
    git(repo, "add", "pom.xml")
    git(repo, "commit", "-q", "-m", "crlf")
    commit_files(repo, {"pom.xml": SINGLE_POM.format(version="2.0")}, "lf")
    state, worktree = workspace(repo, tmp_path)
    apply_rewind(worktree, resolve_rewind(state, "HEAD~1", reactor(state)))
    assert (worktree / "pom.xml").read_bytes() == crlf.encode()


def test_rewind_in_subdirectory_project(tmp_path):
    repo = make_repo(tmp_path / "mono")
    commit_files(repo, {"svc/pom.xml": SINGLE_POM.format(version="1.0"), "other/pom.xml": "x"}, "old")
    commit_files(repo, {"svc/pom.xml": SINGLE_POM.format(version="2.0"), "other/pom.xml": "y"}, "new")
    state = preflight(repo / "svc")
    assert pom_path(state) == "svc/pom.xml"
    worktree = WorktreeManager(state, tmp_path / "state", "run-1").create_result("giml/rewind/a/b")
    sha = apply_rewind(worktree, resolve_rewind(state, "HEAD~1", reactor(state)))
    assert git(worktree, "diff", "--name-only", f"{state.base_sha}..{sha}") == "svc/pom.xml"


def test_unknown_commit(history):
    repo, _ = history
    with pytest.raises(RewindError, match="--rewind-to nope: does not resolve to a commit"):
        resolve_rewind(preflight(repo), "nope", reactor(preflight(repo)))


def test_non_ancestor_is_rejected(history):
    repo, _ = history
    git(repo, "checkout", "-q", "-b", "side", "HEAD~1")
    side = commit_files(repo, {"pom.xml": SINGLE_POM.format(version="3.0")}, "side")
    git(repo, "checkout", "-q", "main")
    with pytest.raises(RewindError, match=rf"{side[:7]} is not an ancestor of the base commit"):
        resolve_rewind(preflight(repo), side, reactor(preflight(repo)))


def test_commit_without_pom_is_rejected(tmp_path):
    repo = make_repo(tmp_path / "repo", {"README": "no pom yet\n"})
    commit_files(repo, {"pom.xml": SINGLE_POM.format(version="1.0")}, "add pom")
    with pytest.raises(RewindError, match="has no pom.xml"):
        resolve_rewind(preflight(repo), "HEAD~1", reactor(preflight(repo)))


@pytest.mark.parametrize("commit", ["HEAD", "HEAD~1"], ids=["base-itself", "unchanged-pom"])
def test_identical_pom_is_rejected_before_any_worktree_exists(history, commit):
    repo, _ = history
    commit_files(repo, {"README": "later docs\n"}, "docs only: HEAD~1 has the same pom.xml as HEAD")
    with pytest.raises(RewindError, match="every pom.xml at .* is identical to the base commit's"):
        resolve_rewind(preflight(repo), commit, reactor(preflight(repo)))


MODULE_POM = '<project xmlns="http://maven.apache.org/POM/4.0.0"><artifactId>{}</artifactId><version>{}</version></project>'
PARENT_POM = ('<project xmlns="http://maven.apache.org/POM/4.0.0"><artifactId>parent</artifactId><version>{}</version>'
              "<modules>{}</modules></project>")  # fmt: skip


def test_rewind_restores_every_reactor_pom_and_keeps_newer_modules(tmp_path):
    repo = make_repo(tmp_path / "reactor")
    old = commit_files(repo, {
        "pom.xml": PARENT_POM.format("1", "<module>core</module>"),
        "core/pom.xml": MODULE_POM.format("core", "1"),
        "core/src/A.java": "class A {}\n",
    }, "old")  # fmt: skip
    commit_files(repo, {
        "pom.xml": PARENT_POM.format("2", "<module>core</module><module>server</module>"),
        "core/pom.xml": MODULE_POM.format("core", "2"),
        "server/pom.xml": MODULE_POM.format("server", "2"),
    }, "add server module")  # fmt: skip
    state = preflight(repo)
    target = resolve_rewind(state, "HEAD~1", reactor(state))
    assert (target.pom_paths, target.kept_paths) == (("pom.xml", "core/pom.xml"), ("server/pom.xml",))
    worktree = WorktreeManager(state, tmp_path / "state", "run-1").create_result("giml/rewind/r/1")
    sha = apply_rewind(worktree, target)
    assert git(worktree, "diff", "--name-only", f"{state.base_sha}..{sha}").splitlines() == ["core/pom.xml", "pom.xml"]
    assert (worktree / "server" / "pom.xml").read_text() == MODULE_POM.format("server", "2")
    message = git(worktree, "log", "-1", "--format=%B")
    assert f"Kept at base (not present at {old[:7]}):\n  server/pom.xml" in message


def test_rewind_counts_as_identical_only_if_every_module_pom_is(tmp_path):
    repo = make_repo(tmp_path / "reactor")
    commit_files(repo, {"pom.xml": PARENT_POM.format("1", "<module>core</module>"),
                        "core/pom.xml": MODULE_POM.format("core", "1")}, "old")  # fmt: skip
    commit_files(repo, {"core/pom.xml": MODULE_POM.format("core", "2")}, "module only")
    state = preflight(repo)
    assert resolve_rewind(state, "HEAD~1", reactor(state)).pom_paths == ("pom.xml", "core/pom.xml")
