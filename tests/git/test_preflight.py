import subprocess

import pytest

from giml.git.preflight import PreflightRefusal, preflight
from tests.git.repo_helpers import SINGLE_POM, commit_files, fingerprint, git, make_repo

POM = SINGLE_POM.format(version="2.9.8")


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo", {"pom.xml": POM, ".gitignore": "target/\n"})


def refused(path, match, **kwargs):
    with pytest.raises(PreflightRefusal, match=match):
        preflight(path, **kwargs)


def test_clean_repository_passes(repo):
    state = preflight(repo)
    assert state.root == repo.resolve()
    assert state.subdir == ""
    assert state.branch == "main"
    assert state.base_sha == git(repo, "rev-parse", "HEAD")
    assert state.project_dir == repo.resolve()


def test_project_in_subdirectory(tmp_path):
    repo = make_repo(tmp_path / "mono", {"services/api/pom.xml": POM})
    state = preflight(repo / "services" / "api")
    assert state.subdir == "services/api"
    assert state.project_dir == repo.resolve() / "services" / "api"


def test_project_key_is_stable_and_distinguishes_subdirectories(tmp_path):
    repo = make_repo(tmp_path / "mono", {"a/pom.xml": POM, "b/pom.xml": POM})
    key_a, key_b = preflight(repo / "a").project_key, preflight(repo / "b").project_key
    assert key_a != key_b and key_a.startswith("mono-") and len(key_a) == len("mono-") + 8
    assert preflight(repo / "a").project_key == key_a


def test_unpushed_commits_are_allowed(repo):
    commit_files(repo, {"pom.xml": POM.replace("2.9.8", "2.9.9")}, "local only")
    assert preflight(repo).base_sha == git(repo, "rev-parse", "HEAD")


def test_ignored_files_are_allowed(repo):
    (repo / "target").mkdir()
    (repo / "target" / "out.class").write_text("x")
    preflight(repo)


@pytest.mark.parametrize(
    "make_dirty",
    [
        lambda r: (r / "new.txt").write_text("untracked"),
        lambda r: (r / "pom.xml").write_text("changed"),
        lambda r: ((r / "staged.txt").write_text("s"), git(r, "add", "staged.txt")),
        lambda r: ((r / "deep" / "er").mkdir(parents=True), (r / "deep" / "er" / "f").write_text("u")),
    ],
    ids=["untracked", "unstaged", "staged", "untracked-nested"],
)
def test_dirty_tree_is_refused_and_left_alone(repo, make_dirty):
    make_dirty(repo)
    before = fingerprint(repo)
    refused(repo, "working tree is not clean")
    assert fingerprint(repo) == before


def test_dirty_message_is_truncated(repo):
    for i in range(7):
        (repo / f"f{i}").write_text("x")
    with pytest.raises(PreflightRefusal) as exc:
        preflight(repo)
    assert str(exc.value).endswith("(and 2 more)")
    assert str(exc.value).count("? f") == 5


def test_detached_head_needs_flag(repo):
    git(repo, "checkout", "-q", "--detach")
    refused(repo, "HEAD is detached")
    state = preflight(repo, allow_detached=True)
    assert state.branch is None


@pytest.mark.parametrize(
    ("marker", "operation"),
    [("MERGE_HEAD", "a merge"), ("rebase-merge", "a rebase"), ("rebase-apply", "a rebase or am"),
     ("CHERRY_PICK_HEAD", "a cherry-pick"), ("REVERT_HEAD", "a revert"), ("BISECT_LOG", "a bisect")],
)  # fmt: skip
def test_operation_in_progress_is_refused(repo, marker, operation):
    target = repo / ".git" / marker
    if marker.startswith("rebase"):
        target.mkdir()
    else:
        target.write_text(git(repo, "rev-parse", "HEAD"))
    refused(repo, f"{operation} is in progress")


def test_real_merge_conflict_is_refused(repo):
    git(repo, "checkout", "-q", "-b", "other")
    commit_files(repo, {"pom.xml": POM.replace("2.9.8", "1")}, "other")
    git(repo, "checkout", "-q", "main")
    commit_files(repo, {"pom.xml": POM.replace("2.9.8", "2")}, "main")
    result = subprocess.run(["git", "-C", str(repo), "merge", "other"], capture_output=True)
    assert result.returncode != 0
    refused(repo, "a merge is in progress")


def test_not_a_repository(tmp_path):
    (tmp_path / "plain").mkdir()
    refused(tmp_path / "plain", "not inside a git work tree")


def test_not_a_directory(tmp_path):
    refused(tmp_path / "missing", "not a directory")


def test_empty_repository(tmp_path):
    refused(make_repo(tmp_path / "empty"), "HEAD does not resolve")


def test_bare_repository(tmp_path):
    bare = tmp_path / "bare.git"
    bare.mkdir()
    git(bare, "init", "-q", "--bare")
    refused(bare, "not inside a git work tree")


def test_submodule_is_refused(tmp_path):
    sub = make_repo(tmp_path / "sub", {"x": "1"})
    repo = make_repo(tmp_path / "repo", {"pom.xml": POM})
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "lib")
    git(repo, "commit", "-q", "-m", "add submodule")
    refused(repo, "submodules are unsupported")


def test_gitmodules_file_alone_is_refused(tmp_path):
    repo = make_repo(tmp_path / "repo", {"pom.xml": POM, ".gitmodules": "[submodule \"x\"]\n"})
    refused(repo, "submodules are unsupported")


@pytest.mark.parametrize(
    "files",
    [
        {".gitattributes": "*.bin filter=lfs diff=lfs merge=lfs -text\n"},
        {"assets/.gitattributes": "*.png filter=lfs diff=lfs merge=lfs -text\n"},
        {".lfsconfig": "[lfs]\n"},
    ],
    ids=["root-attributes", "nested-attributes", "lfsconfig"],
)
def test_lfs_is_allowed(tmp_path, files):
    repo = make_repo(tmp_path / "repo", {"pom.xml": POM, **files})
    preflight(repo)


def test_gitattributes_without_lfs_is_fine(tmp_path):
    repo = make_repo(tmp_path / "repo", {"pom.xml": POM, ".gitattributes": "*.sh text eol=lf\n"})
    preflight(repo)


def test_exactly_five_dirty_entries_have_no_more_suffix(repo):
    for i in range(5):
        (repo / f"f{i}").write_text("x")
    with pytest.raises(PreflightRefusal) as exc:
        preflight(repo)
    assert str(exc.value).endswith("? f4")


def test_gitlink_without_gitmodules_is_refused(repo):
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sha},vendored")
    git(repo, "commit", "-q", "-m", "gitlink only")
    (repo / "vendored").mkdir()  # an uninitialised submodule is an empty directory
    refused(repo, "submodules are unsupported")
