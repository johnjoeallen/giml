"""`giml assess` (spec section 6.3): measure test quality on the base commit and earn a tier.

Runs in a giml worktree on a `giml/assess/...` branch. Missing quality tooling is added first as a
`[giml-setup]` commit (spec section 3), so a developer who scores low can take that commit back.
"""

from __future__ import annotations

import datetime
import json
import re
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from giml import workspace
from giml.core.config import GateConfig, load_project_settings
from giml.core.model import GateResultRecord, ProjectRecord, RunRecord
from giml.gate.modules import ModuleFacts, declares_failsafe, module_facts
from giml.gate.reports import Mutations, ReportError, flaky_tests, read_jacoco, read_pit, read_surefire
from giml.gate.setup import SetupResult, apply_setup, tooling
from giml.gate.tiers import Metrics, earned_tier, misses
from giml.git.lock import ProjectLock
from giml.git.preflight import RepoState, preflight
from giml.git.worktrees import WorktreeManager, require_identity
from giml.maven.jdk import JdkCatalog, catalog, resolve_jdk
from giml.maven.project import discover_reactor
from giml.maven.runner import MavenResult, run_maven

STOP_ASSESSED = "assessed"
STOP_TESTS_FAILED = "tests_failed"

Env = Mapping[str, str] | None  # None: inherit giml's own environment
MavenRunner = Callable[[Path, list[str], Path, float, Env], MavenResult]
JavaRunner = Callable[[list[str], float, Env], subprocess.CompletedProcess]


class PrerequisiteError(RuntimeError):
    """The project cannot be assessed: its build or unit tests fail at the base commit (exit 5)."""


class UnknownTierError(ValueError):
    """--declared-tier names a tier the gate config does not define (exit 5)."""


def run_java(args: list[str], timeout: float, env: Env = None) -> subprocess.CompletedProcess:
    java = shutil.which("java", path=(env or os.environ).get("PATH")) or "java"
    return subprocess.run([java, *args], capture_output=True, text=True, timeout=timeout, check=False,
                          env=dict(env) if env is not None else None)  # fmt: skip


@dataclass
class Assessment:
    run_id: str
    branch: str
    worktree: Path
    report_path: Path
    result: dict


class _Session:
    """One assessment: the worktree, its logs and the tools it calls."""

    def __init__(self, project_dir: Path, logs: Path, tools_dir: Path, maven: MavenRunner, java: JavaRunner,
                 timeout: float, env: Env) -> None:  # fmt: skip
        self.project_dir, self.logs, self.tools_dir = project_dir, logs, tools_dir
        self.maven, self.java, self.timeout, self.env = maven, java, timeout, env
        self.step = 0

    def mvn(self, name: str, args: list[str]) -> MavenResult:
        self.step += 1
        return self.maven(self.project_dir, args, self.logs / f"{self.step:02d}-{name}.log", self.timeout, self.env)

    def run_java(self, args: list[str]) -> subprocess.CompletedProcess:
        return self.java(args, self.timeout, self.env)

    def jacoco_cli(self) -> Path:
        cli = tooling()["jacoco_cli"]
        jar = self.tools_dir / f"{cli.artifact_id}-{cli.version}-{cli.classifier}.jar"
        if not jar.is_file():
            plugin = tooling()["dependency_plugin"]
            result = self.mvn("fetch-jacoco-cli", [f"{plugin.gav}:copy", f"-Dartifact={cli.gav}",
                                                  f"-DoutputDirectory={self.tools_dir}"])  # fmt: skip
            if not result.succeeded or not jar.is_file():
                raise ReportError(f"could not fetch {cli.gav}; see {result.log_path}")
        return jar


def _coverage(session: _Session, facts: list[ModuleFacts], out: Path) -> tuple[Metrics | None, dict]:
    """Whole-reactor coverage from JaCoCo's CLI over every module's classes (untested modules
    included), and the share of classes the project's own configuration excludes."""
    class_dirs = [f.classes_dir for f in facts if f.classes_dir.is_dir()]
    execs = [f.jacoco_exec for f in facts if f.jacoco_exec.is_file()]
    if not class_dirs:
        return None, {"reason": "no compiled classes"}
    args = ["-jar", str(session.jacoco_cli()), "report", *map(str, execs)]
    for directory in class_dirs:
        args += ["--classfiles", str(directory)]
    args += ["--xml", str(out)]
    result = session.run_java(args)
    if result.returncode != 0:
        return None, {"reason": f"jacoco cli failed: {result.stderr.strip()[-500:]}"}
    aggregate = read_jacoco(out)
    reported: set[str] = set()
    for fact in facts:
        if fact.jacoco_report.is_file():
            reported |= read_jacoco(fact.jacoco_report).classes
    tested_dirs = {f.classes_dir for f in facts if f.jacoco_report.is_file()}
    in_tested = {c for c in aggregate.classes if any((d / f"{c}.class").is_file() for d in tested_dirs)}
    excluded = sorted(in_tested - reported)
    share = 100.0 * len(excluded) / len(aggregate.classes) if aggregate.classes else 0.0
    return Metrics(aggregate.line_percent, aggregate.branch_percent, None, None, share), {"excluded": excluded}


def _pit_excludes(reactor: list[Path]) -> list[str]:
    patterns = []
    for pom in reactor:
        text = pom.read_text(encoding="utf-8")
        for block in re.findall(r"<excludedClasses>(.*?)</excludedClasses>", text, re.S):
            patterns += re.findall(r"<param>\s*([^<]+?)\s*</param>", block)
    return patterns


def _enforcer(session: _Session) -> dict:
    result = session.mvn("enforcer", ["validate"])
    if result.succeeded:
        return {"status": "passed", "log": str(result.log_path)}
    lines = result.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    rules = sorted({m.group(1) for line in lines if (m := re.search(r"Rule \d+: [\w.]*?(\w+) failed", line))})
    return {"status": "baseline_failed", "failed_rules": rules, "log": str(result.log_path)}


def _startup_check(project_dir: Path) -> str:
    settings = project_dir / ".redkite" / "settings.yml"
    if settings.is_file() and re.search(r"^\s*profile\s*:", settings.read_text(encoding="utf-8"), re.M):
        return "configured"  # verified from M6
    return "not_configured"


def assess(
    path: Path,
    state_dir: Path,
    store,
    clock: Callable[[], datetime.datetime],
    config: GateConfig,
    declared_tier: str | None = None,
    maven_timeout: float = 3600,
    maven: MavenRunner = run_maven,
    java: JavaRunner = run_java,
    jdks: JdkCatalog | None = None,
    environ: Mapping[str, str] | None = None,
) -> Assessment:
    if declared_tier is not None and declared_tier not in config.tiers:
        raise UnknownTierError(f"--declared-tier {declared_tier}: not a tier in the gate config "
                               f"({', '.join(config.tiers)})")  # fmt: skip
    repo = preflight(path)
    reactor = discover_reactor(repo.project_dir)
    require_identity(repo.root)
    with ProjectLock(state_dir, repo.project_key):
        now = clock()
        workspace.mark_crashed(store, repo.project_key, now)
        run_id = workspace.new_run_id(now)
        branch = f"giml/assess/{repo.base_sha[:7]}/{now.astimezone(datetime.UTC).strftime('%Y%m%dT%H%M%SZ')}"
        manager = WorktreeManager(repo, state_dir, run_id)
        store.save_project(ProjectRecord(repo.project_key, repo.project_dir, workspace.remote_url_hash(repo.root), now))
        store.start_run(RunRecord(run_id, repo.project_key, repo.base_sha, branch, manager.root / run_id, now,
                                  kind="assess"))  # fmt: skip
        stop = workspace.STOP_SETUP_FAILED
        try:
            worktree = manager.create_result(branch)
            result = _measure(repo, reactor, worktree, state_dir, run_id, now, config, declared_tier,
                              maven_timeout, maven, java, jdks or catalog(),
                              os.environ if environ is None else environ)  # fmt: skip
            stop = STOP_ASSESSED
        except PrerequisiteError:
            stop = STOP_TESTS_FAILED
            raise
        finally:
            store.finish_run(run_id, clock(), stop)
        report = state_dir / "reports" / run_id / "assessment.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        store.save_gate_result(GateResultRecord(
            run_id, repo.project_key, repo.base_sha, config.version, result["passed_tier"],
            json.dumps(result, sort_keys=True), now, now + datetime.timedelta(days=config.shared.max_result_age_days),
        ))  # fmt: skip
    return Assessment(run_id, branch, worktree, report, result)


def _measure(repo: RepoState, reactor: list[Path], worktree: Path, state_dir: Path, run_id: str,
             now: datetime.datetime, config: GateConfig, declared_tier: str | None, timeout: float,
             maven: MavenRunner, java: JavaRunner, jdks: JdkCatalog, environ: Mapping[str, str]) -> dict:  # fmt: skip
    worktree_reactor = [worktree / pom.relative_to(repo.root) for pom in reactor]
    setup: SetupResult = apply_setup(worktree, worktree_reactor)
    facts = [module_facts(pom) for pom in worktree_reactor]
    project_dir = worktree / repo.subdir if repo.subdir else worktree
    jdk = resolve_jdk(load_project_settings(project_dir), jdks, environ)
    run_dir = state_dir / "runs" / run_id
    session = _Session(project_dir, run_dir / "logs", state_dir / "tools", maven, java, timeout,
                       jdk.build_env(environ))  # fmt: skip

    first = session.mvn("test", ["test", "-Denforcer.skip=true"])
    if not first.succeeded:
        raise PrerequisiteError(
            f"the build or unit tests fail at the base commit with giml's setup applied; see {first.log_path}"
        )
    runs = [_outcomes(facts)]
    for number in range(2, config.shared.flake_check_runs + 1):
        session.mvn(f"flake-{number}", ["test", "-Denforcer.skip=true", "-Djacoco.skip=true"])
        runs.append(_outcomes(facts))

    unavailable: dict[str, str] = {}
    coverage, coverage_info = _coverage(session, facts, run_dir / "jacoco-aggregate.xml")
    if coverage is None:
        unavailable["unit_line_coverage"] = unavailable["unit_branch_coverage"] = coverage_info["reason"]

    pit = session.mvn("pit", ["test-compile", "org.pitest:pitest-maven:mutationCoverage", "-Denforcer.skip=true",
                              "-DtimestampedReports=false"])  # fmt: skip
    mutations = Mutations()
    # A failed PIT run's reports are partial (or cut off mid-write when it was killed); never read them.
    for fact in facts if pit.succeeded else []:
        if fact.pit_report.is_file():
            mutations = mutations + read_pit(fact.pit_report)
    if not pit.succeeded or mutations.total == 0:
        reason = f"PIT failed; see {pit.log_path}" if not pit.succeeded else "PIT produced no mutations"
        unavailable["pit_test_strength"] = unavailable["pit_mutation_coverage"] = reason
        mutations = Mutations()

    untested = tuple(str(f.directory.relative_to(worktree)) for f in facts if f.untested)
    metrics = Metrics(
        coverage.unit_line_coverage if coverage else None,
        coverage.unit_branch_coverage if coverage else None,
        mutations.test_strength,
        mutations.mutation_coverage,
        coverage.excluded_share if coverage else 0.0,
        tuple(flaky_tests(runs)),
        untested,
        unavailable,
    )
    passed = earned_tier(metrics, config)
    return {
        "project": repo.project_dir.name,
        "declared_tier": declared_tier,
        "passed_tier": passed,
        "config_version": config.version,
        "tools": {"pit": tooling()["pitest"].version, "jacoco": tooling()["jacoco"].version,
                  "jdk": jdk.record()},  # fmt: skip
        "measured": {name: (round(value, 2) if value is not None else None) for name, value in (
            ("unit_line_coverage", metrics.unit_line_coverage),
            ("unit_branch_coverage", metrics.unit_branch_coverage),
            ("pit_test_strength", metrics.pit_test_strength),
            ("pit_mutation_coverage", metrics.pit_mutation_coverage),
        )},  # fmt: skip
        "mutations": dict(mutations.statuses),
        "unavailable": unavailable,
        "integration_tests": {"count": sum(f.integration_tests for f in facts),
                              "failsafe_declared": declares_failsafe(worktree_reactor)},  # fmt: skip
        "flaky_tests": list(metrics.flaky_tests),
        "untested_modules": list(untested),
        "excluded_share": round(metrics.excluded_share, 2),
        "excluded": {"jacoco_classes": coverage_info.get("excluded", []),
                     "pit_excluded_classes": _pit_excludes(worktree_reactor)},  # fmt: skip
        "startup_check": _startup_check(project_dir),
        "enforcer": _enforcer(session),
        "tooling_added": setup.added,
        "tooling_kept": setup.kept,
        "setup_commit": setup.commit,
        "failed_for_declared": misses(metrics, config.tiers[declared_tier]) if declared_tier else [],
        "autonomy": config.autonomy.get(passed) if passed else None,
        "base_sha": repo.base_sha,
        "measured_at": now.astimezone(datetime.UTC).isoformat(),
        "expires": (now + datetime.timedelta(days=config.shared.max_result_age_days)).astimezone(datetime.UTC).isoformat(),
    }


def _outcomes(facts: list[ModuleFacts]) -> dict[str, str]:
    outcomes: dict[str, str] = {}
    for fact in facts:
        if fact.surefire_reports.is_dir():
            outcomes.update(read_surefire(fact.surefire_reports))
    return outcomes

