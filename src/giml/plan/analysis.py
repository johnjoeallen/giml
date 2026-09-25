"""`giml plan --dry-run` (spec sections 13 and 17, M5a): analyse a project and write the report.

Runs in a throwaway worktree at the base commit. It resolves the reactor's dependency trees (Maven
resolves, nothing is compiled or tested), matches them against the recorded OSV snapshot, lists
each dependency's candidates from the recorded Central snapshot, and writes report.json and
report.md. It builds nothing, edits no POM, creates no branch and fetches no data (spec 7.3).
"""

from __future__ import annotations

import contextlib
import datetime
import itertools
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from giml import workspace
from giml.core.config import GateConfig, PlanningSettings, load_project_settings
from giml.core.interfaces import AdvisorySource, ArtifactMetadataSource
from giml.core.model import Coordinate, GateResultRecord, ProjectRecord, RunRecord, SnapshotInfo, VersionRelease
from giml.data import central, osv
from giml.data.central import LocalCentralMetadataSource
from giml.data.osv import LocalOsvAdvisorySource
from giml.git.lock import ProjectLock
from giml.git.preflight import preflight
from giml.git.worktrees import WorktreeManager
from giml.maven.declarations import Declarations, read_declarations
from giml.maven.isolation import isolated_env, run_temp
from giml.maven.jdk import JdkCatalog, catalog, resolve_jdk
from giml.maven.project import discover_reactor
from giml.maven.runner import MavenRunner, run_maven
from giml.maven.tree import ModuleTree, ResolutionError, resolve_reactor
from giml.maven.version import ComparableVersion
from giml.plan.candidates import DependencyPlan, is_prerelease, plan_dependency
from giml.plan.exposure import TreeExposure, resolve_exposure
from giml.plan.parents import ChangeUnit, UnitPlan, change_units, not_evaluated, plan_change_unit, with_version
from giml.plan.report import ReportInputs, TierStatus, build_report, render_markdown

STOP_DRY_RUN = "dry_run"
STOP_RESOLUTION_FAILED = "resolution_failed"


class MissingSnapshotError(ValueError):
    """A snapshot the analysis needs has not been synced (exit 5)."""


@dataclass(frozen=True)
class Sources:
    """How the recorded snapshots are opened; tests replace them with fakes."""

    advisories: Callable[[SnapshotInfo], AdvisorySource] = LocalOsvAdvisorySource
    metadata: Callable[[SnapshotInfo], ArtifactMetadataSource] = LocalCentralMetadataSource


@dataclass(frozen=True)
class DryRun:
    run_id: str
    report: dict
    json_path: Path
    markdown_path: Path


def tier_status(record: GateResultRecord | None, config: GateConfig, now: datetime.datetime, base_sha: str) -> TierStatus:
    """Whether the stored assessment lets the planner propose anything (spec 6.4)."""
    if record is None:
        return TierStatus(None, None, None, None, False, "no assessment; run `giml assess <path>`")
    declared = json.loads(record.json).get("declared_tier")
    common = (record.earned_tier, declared, record.measured_at.isoformat(), record.expires_at.isoformat())
    if record.config_version != config.version:
        note = f"assessed under gate config version {record.config_version}, the current one is {config.version}; run `giml assess`"
    elif record.expires_at < now:
        note = f"the assessment expired on {record.expires_at:%Y-%m-%d}; run `giml assess`"
    elif record.earned_tier is None:
        note = "the last assessment earned no tier"
    elif record.earned_tier not in config.tiers:
        note = f"the earned tier {record.earned_tier} is not in the gate config; run `giml assess`"
    else:
        other = f"assessed at {record.base_sha[:7]}, the base commit is {base_sha[:7]}" if record.base_sha != base_sha else ""
        return TierStatus(*common, True, other)
    return TierStatus(*common, False, note)


def newest_release(releases: list[VersionRelease] | None) -> str | None:
    """The newest release, ignoring prereleases unless there is nothing else."""
    if not releases:
        return None
    stable = [r for r in releases if not is_prerelease(r.version)] or list(releases)
    return max(stable, key=lambda r: (ComparableVersion(r.version), r.version)).version


def _snapshot_warnings(snapshots: Mapping[str, SnapshotInfo | None], limit: int, now: datetime.datetime) -> list[str]:
    return [f"{source} snapshot {info.id} is {(now - info.fetched_at).days} days old (limit {limit}); run `giml sync`"
            for source, info in snapshots.items() if info is not None
            and now - info.fetched_at > datetime.timedelta(days=limit)]  # fmt: skip


def _plan_units(units: list[ChangeUnit], releases, exposure: TreeExposure, tier: TierStatus, options: PlanningSettings,
                now: datetime.datetime, resolve_with: Callable[[ChangeUnit, str], TreeExposure]) -> list[UnitPlan]:  # fmt: skip
    """Parent and BOM change units, evaluated by resolution unless the tier forbids proposals."""
    plans = []
    for unit in units:
        if not tier.usable:
            plans.append(not_evaluated(unit, exposure.exposure, "report only: no usable tier"))
            continue
        available = releases.versions(unit.coordinate) if releases else None
        plans.append(plan_change_unit(unit, available, exposure, lambda version, unit=unit: resolve_with(unit, version),
                                      options, now))  # fmt: skip
    return plans


@dataclass(frozen=True)
class Analysis:
    """What a worktree's dependencies look like: resolved trees, where versions are declared, CVE exposure and candidates."""

    trees: list[ModuleTree]
    declarations: Declarations
    exposure: TreeExposure
    plans: list[DependencyPlan]  # one per resolved dependency, in exposure order
    unit_plans: list[UnitPlan]  # parent and BOM change units
    available: dict[Coordinate, list[VersionRelease] | None]  # Central versions per coordinate
    version_advisories: dict[tuple[Coordinate, str], frozenset[str]] = field(default_factory=dict)  # `latest` only: advisories per version


def analyse(
    project_dir: Path,
    poms: list[Path],
    logs: Path,
    timeout: float,
    maven: MavenRunner,
    maven_env: Mapping[str, str] | None,
    sources: Sources,
    osv_snapshot: SnapshotInfo,
    central_snapshot: SnapshotInfo | None,
    options: PlanningSettings,
    tier: TierStatus,
    now: datetime.datetime,
    log_name: str = "01-tree.log",
    units: bool = True,
) -> Analysis:
    """Resolve the reactor's trees, match them against the recorded snapshots and plan every dependency and change unit.

    Parent and BOM candidates are evaluated by writing each version into ``poms`` temporarily (restored
    afterwards), so the worktree must be one nobody else is using. ``units=False`` skips that evaluation
    (a re-check of the exposure after changes does not need it). Raises ResolutionError.
    """
    trees = resolve_reactor(project_dir, poms, logs / log_name, timeout, maven, maven_env)
    declarations = read_declarations(poms)
    steps = itertools.count(2)
    with contextlib.ExitStack() as stack:
        advisories = _open(stack, sources.advisories(osv_snapshot))
        releases = _open(stack, sources.metadata(central_snapshot)) if central_snapshot else None
        exposure = resolve_exposure(trees, advisories)
        available = {d.coordinate: releases.versions(d.coordinate) if releases else None for d in exposure.dependencies}
        version_advisories = _version_advisories(exposure, available, advisories) if options.strategy == "latest" else {}
        plans = [plan_dependency(d, declarations.for_coordinate(d.coordinate), available[d.coordinate], advisories, options, now)
                 for d in exposure.dependencies]  # fmt: skip

        def resolve_with(unit: ChangeUnit, version: str) -> TreeExposure:
            name = re.sub(r"[^\w.\-]", "_", f"{next(steps):02d}-{unit.kind}-{unit.coordinate.artifact_id}-{version}")
            with with_version(unit.site, version, expected=unit.version):
                candidate = resolve_reactor(project_dir, poms, logs / f"{name}.log", timeout, maven, maven_env)
            return resolve_exposure(candidate, advisories)

        unit_plans = _plan_units(change_units(declarations) if units else [], releases, exposure, tier, options, now, resolve_with)
    return Analysis(trees, declarations, exposure, plans, unit_plans, available, version_advisories)


def dry_run(
    path: Path,
    state_dir: Path,
    store,
    clock: Callable[[], datetime.datetime],
    config: GateConfig,
    options: PlanningSettings,
    maven: MavenRunner = run_maven,
    jdks: JdkCatalog | None = None,
    environ: Mapping[str, str] | None = None,
    sources: Sources = Sources(),
    allow_detached: bool = False,
    timeout: float = 3600,
) -> DryRun:
    repo = preflight(path, allow_detached)
    reactor = discover_reactor(repo.project_dir)
    env = os.environ if environ is None else environ
    with ProjectLock(state_dir, repo.project_key):
        now = clock()
        workspace.mark_crashed(store, repo.project_key, now)
        run_id = workspace.new_run_id(now)
        manager = WorktreeManager(repo, state_dir, run_id)
        store.save_project(ProjectRecord(repo.project_key, repo.project_dir, workspace.remote_url_hash(repo.root), now))
        store.start_run(RunRecord(run_id, repo.project_key, repo.base_sha, "", manager.root / f"{run_id}-trial-0", now))
        stop, worktree = workspace.STOP_SETUP_FAILED, None
        try:
            with run_temp(state_dir, run_id, log_path=state_dir / "runs" / run_id / "logs" / "temp.log") as temp:
                osv_snapshot, central_snapshot = store.latest_snapshot(osv.SOURCE), store.latest_snapshot(central.SOURCE)
                if osv_snapshot is None:
                    raise MissingSnapshotError("no OSV snapshot; run `giml sync`")
                worktree = manager.create_trial(repo.base_sha, 0)
                worktree_poms = [worktree / pom.relative_to(repo.root) for pom in reactor]
                project_dir = worktree / repo.subdir if repo.subdir else worktree
                jdk = resolve_jdk(load_project_settings(project_dir), jdks or catalog(), env)
                tier = tier_status(store.latest_gate_result(repo.project_key), config, now, repo.base_sha)
                try:
                    analysis = analyse(project_dir, worktree_poms, state_dir / "runs" / run_id / "logs", timeout, maven,
                                       isolated_env(jdk.build_env(env), env, temp), sources, osv_snapshot, central_snapshot,
                                       options, tier, now)  # fmt: skip
                except ResolutionError:
                    stop = STOP_RESOLUTION_FAILED
                    raise
                declarations, exposure, plans, unit_plans, available = (
                    analysis.declarations, analysis.exposure, analysis.plans, analysis.unit_plans, analysis.available)  # fmt: skip
                report = build_report(ReportInputs(
                    project=repo.project_dir.name, base_sha=repo.base_sha, run_id=run_id, worktree=worktree, generated_at=now,
                    config_version=config.version, options=options,
                    tier=tier,
                    snapshots={"osv": osv_snapshot.id, "central": central_snapshot.id if central_snapshot else None},
                    jdk=jdk.record(), exposure=exposure, plans=plans,
                    latest_available={c: newest_release(v) for c, v in available.items()},
                    parents=declarations.parents, skipped=declarations.skipped, units=unit_plans,
                    warnings=_snapshot_warnings({"osv": osv_snapshot, "central": central_snapshot}, options.max_snapshot_age_days, now),
                ))  # fmt: skip
                stop = STOP_DRY_RUN
        finally:
            if worktree is not None and worktree.exists():
                manager.remove(worktree)
            store.finish_run(run_id, clock(), stop)
        directory = state_dir / "reports" / run_id
        directory.mkdir(parents=True, exist_ok=True)
        json_path, markdown_path = directory / "report.json", directory / "report.md"
        json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return DryRun(run_id, report, json_path, markdown_path)


def _version_advisories(exposure: TreeExposure, available, advisories) -> dict[tuple[Coordinate, str], frozenset[str]]:
    """The advisories of every released version of every resolved dependency, so a ladder can skip versions that are worse."""
    return {(d.coordinate, r.version): frozenset(f.advisory_id for f in advisories.affecting(d.coordinate, r.version))
            for d in exposure.dependencies for r in available[d.coordinate] or []}


def _open(stack: contextlib.ExitStack, source):
    """Close a snapshot source when the analysis is done, if it has anything to close."""
    if hasattr(source, "close"):
        stack.callback(source.close)
    return source
