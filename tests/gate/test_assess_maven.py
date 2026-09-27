"""End to end with real Maven, JaCoCo, PIT and the enforcer on the mini-reactor fixture.

Slow (about half a minute, more on first run while Maven downloads plugins). Needs mvn and java on
PATH and network access to Maven Central unless the local repository already has the plugins.
Run with `pytest -m slow`.
"""

import datetime
import shutil
from pathlib import Path

import pytest

from giml.core.config import default_gate_config_path, load_gate_config
from giml.gate.assess import assess
from giml.store.sqlite_store import SqliteStateStore
from tests.git.repo_helpers import fingerprint, git, make_repo

pytestmark = pytest.mark.slow

MINI = Path(__file__).resolve().parents[1] / "fixtures" / "maven" / "mini-reactor"

# Captured at import time, before any test's autouse fixture can monkeypatch HOME.
_REAL_HOME = Path.home()


@pytest.fixture
def real_home(monkeypatch):
    # Maven must use the developer's real ~/.m2 (spec 9.2); the autouse isolated HOME would
    # force a full re-download of every plugin.
    monkeypatch.setenv("HOME", str(_REAL_HOME))


def test_real_assessment_of_the_mini_reactor(tmp_path, real_home):
    if shutil.which("mvn") is None or shutil.which("java") is None:
        pytest.skip("mvn and java are required")
    repo = tmp_path / "mini"
    shutil.copytree(MINI, repo)
    make_repo(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    before = fingerprint(repo)
    config = load_gate_config(default_gate_config_path())
    with SqliteStateStore(tmp_path / "state" / "state.db") as store:
        outcome = assess(repo, tmp_path / "state", store, lambda: datetime.datetime.now(datetime.UTC), config, "B")
    result = outcome.result
    assert result["measured"] == {"unit_line_coverage": 50.0, "unit_branch_coverage": 16.67,
                                  "pit_test_strength": 100.0, "pit_mutation_coverage": 62.5}  # fmt: skip
    assert result["mutations"] == {"KILLED": 5, "NO_COVERAGE": 3}
    assert result["untested_modules"] == ["app"] and result["passed_tier"] is None
    assert result["flaky_tests"] == [] and result["excluded_share"] == 0.0
    assert result["enforcer"]["status"] == "passed"
    assert git(outcome.worktree, "diff", "--name-only", "HEAD~1..HEAD") == "pom.xml"
    assert fingerprint(repo) == before
    # every JVM Maven started used the run's own temp directory, which is gone now
    run_dir = tmp_path / "state" / "runs" / outcome.run_id
    assert not (run_dir / "tmp").exists() and (run_dir / "logs" / "temp.log").read_text().startswith("removed ")
    logs = "".join(p.read_text(errors="replace") for p in (run_dir / "logs").glob("*.log"))
    assert f"-Djava.io.tmpdir={run_dir / 'tmp'}" in logs  # the JVMs announce JAVA_TOOL_OPTIONS on stderr
