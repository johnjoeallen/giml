"""Verify the baseline of a run's worktree and record it (spec sections 5.3, 9.1 and 10).

Applies giml's quality tooling as the ``[giml-setup]`` commit (on top of the rewind commit for a
rewind run), then runs the baseline stages through the caching runner with the project's JDK and the
run's own temp directory. The result is written to ``reports/<run-id>/baseline.json``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from giml.core.config import GateConfig, load_project_settings
from giml.gate.modules import declares_failsafe, module_facts
from giml.gate.setup import apply_setup
from giml.maven.build import MavenBuildRunner
from giml.maven.cache import BuildEnvironment, CachingBuildRunner, maven_version, stage_key_parts, tooling_fingerprint
from giml.maven.isolation import RunTemp, isolated_env
from giml.maven.jdk import Jdk, JdkCatalog, resolve_jdk
from giml.maven.project import discover_reactor
from giml.maven.runner import MavenRunner
from giml.smoke.postgres import Unavailable, read_connection
from giml.smoke.runner import SmokeRunner
from giml.smoke.stage import StartupAwareRunner
from giml.plan.baseline import STAGE_ORDER, Baseline, verify_baseline
from giml.plan.outcome_log import RewindFacts, log_baseline, oracle_strength
from giml.store.result_cache import FileResultCache
from giml.workspace import Workspace

MAVEN_TIMEOUT_SECONDS = 3600


class BaselineInfrastructureError(RuntimeError):
    """The baseline could not be judged because the environment failed (network, disk, memory); exit 4."""


@dataclass(frozen=True)
class BaselineRun:
    baseline: Baseline
    report: dict
    report_path: Path
    last_log: Path | None  # the log of the stage that stopped the baseline, if any
    runner: CachingBuildRunner  # what the baseline ran on: candidate trials reuse it, so they share its cache and environment
    jdk: Jdk
    env: Mapping[str, str]  # the isolated environment every Maven call of this run uses


def verification_stages(config: GateConfig, project_dir: Path, has_tier: bool) -> list[str]:
    """The baseline's stages: the three that always run, integration tests when the project has them, PIT when it earned a tier."""
    stages = list(STAGE_ORDER)
    reactor = discover_reactor(project_dir)
    if config.verification.integration_tests == "when_present" and (
            sum(module_facts(pom).integration_tests for pom in reactor) or declares_failsafe(reactor)):  # fmt: skip
        stages.append("integration")
    if config.verification.startup_check == "when_configured" and load_project_settings(project_dir).smoke is not None:
        stages.append("startup")
    if config.verification.pit == "always" and has_tier:
        stages.append("pit")
    return stages


def sweep_leftover_schemas(smoke, smoke_runner, project_key: str, logs: Path) -> None:
    """Drop this project's schemas that crashed runs left behind; a database that cannot be reached is noted, not fatal."""
    if smoke is None or smoke.database is None or smoke_runner is None:
        return
    try:
        lifecycle = smoke_runner.lifecycle(read_connection(smoke.database, smoke_runner.environ))
        dropped = lifecycle.sweep(project_key)
        note = f"swept {len(dropped)} leftover schema(s): {', '.join(dropped)}" if dropped else "no leftover schemas"
    except Unavailable as exc:
        note = f"could not sweep leftover schemas: {exc}"
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / "smoke-sweep.log").open("a", encoding="utf-8") as handle:
        handle.write(note + "\n")


def run_baseline(
    ws: Workspace,
    temp: RunTemp,
    state_dir: Path,
    config: GateConfig,
    jdks: JdkCatalog,
    environ: Mapping[str, str] | None,
    maven: MavenRunner,
    store,
    timeout_seconds: int = MAVEN_TIMEOUT_SECONDS,
) -> BaselineRun:
    env_in = os.environ if environ is None else environ
    project_dir = ws.worktree / ws.repo.subdir if ws.repo.subdir else ws.worktree
    setup = apply_setup(ws.worktree, discover_reactor(project_dir))
    jdk = resolve_jdk(load_project_settings(project_dir), jdks, env_in)
    env = isolated_env(jdk.build_env(env_in), env_in, temp)
    build_environment = BuildEnvironment(jdk.version, maven_version(env), tooling_fingerprint(), config.version)
    logs = state_dir / "runs" / ws.run_id / "logs"
    smoke = load_project_settings(project_dir).smoke
    smoke_runner = SmokeRunner(smoke, env, logs, ws.repo.project_key, ws.run_id) if smoke is not None else None
    sweep_leftover_schemas(smoke, smoke_runner, ws.repo.project_key, logs)
    runner = CachingBuildRunner(StartupAwareRunner(MavenBuildRunner(maven, env, logs), smoke_runner), FileResultCache(state_dir / "cache"),
                                lambda worktree, stage: stage_key_parts(worktree, stage, build_environment), logs)  # fmt: skip
    record = store.latest_gate_result(ws.repo.project_key)
    baseline = verify_baseline(runner, project_dir, timeout_seconds, rewound=ws.rewind is not None,
                               stages_to_run=verification_stages(config, project_dir, bool(record and record.earned_tier)))  # fmt: skip
    stopped = next((s.outcome for s in baseline.stages if s.outcome is not None and baseline.stop and not s.outcome.passed), None)
    rewind = RewindFacts(ws.rewind.sha, ws.rewind.committed_at) if ws.rewind else None
    logged = log_baseline(store, ws.run_id, ws.repo.project_key, baseline, tier=record.earned_tier if record else None,
                          oracle=oracle_strength(record), rewind=rewind, jdk=jdk.version)  # fmt: skip
    report = {"run_id": ws.run_id, "base_sha": ws.repo.base_sha, "rewind_from": ws.rewind.sha if ws.rewind else None,
              "setup_commit": setup.commit, "tooling_added": setup.added, "tooling_kept": setup.kept, "jdk": jdk.record(),
              "baseline": baseline.as_dict(), "cache": runner.metrics().as_dict(),
              "logged": {"attempts": logged.attempts, "new_examples": logged.examples_new}}  # fmt: skip
    path = state_dir / "reports" / ws.run_id / "baseline.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return BaselineRun(baseline, report, path, stopped.log_path if stopped else None, runner, jdk, env)
