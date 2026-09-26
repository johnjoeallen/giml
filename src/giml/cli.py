"""Command-line entry point (spec section 13)."""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import enum
import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from giml import __version__
from giml.core.model import Coordinate, SnapshotInfo
from giml.data import central, osv
from giml.data.central import MetadataError
from giml.data.http import Fetcher, FetchError, UrlLibFetcher
from giml.data.snapshots import read_manifest
from giml import workspace
from giml.core.config import (
    MAJOR_UPDATE_MODES, PLANNING_SCOPES, PLANNING_STRATEGIES, TEST_SCOPE_MODES, ConfigError, default_gate_config_path, load_gate_config,
)  # fmt: skip
from giml.gate.assess import JavaRunner, MavenRunner, PrerequisiteError, UnknownTierError, run_java
from giml.gate.assess import assess as assess_project
from giml.gate.reports import ReportError
from giml.maven.isolation import InsufficientSpace
from giml.maven.jdk import catalog
from giml.maven.tree import ResolutionError
from giml.maven.runner import MavenNotFound, run_maven
from giml.plan.analysis import MissingSnapshotError, Sources, dry_run
from giml.plan.baseline_run import BaselineInfrastructureError, BaselineRun, run_baseline
from giml.plan.planner import PlanRun, render_plan_summary, run_planning
from giml.plan.report import render_summary
from giml.store.examples import example_lines
from giml.git.lock import LockHeld
from giml.git.preflight import PreflightRefusal
from giml.git.rewind import RewindError
from giml.git.runner import GitError
from giml.git.worktrees import BranchExistsError, ForeignWorktreeError, IdentityError
from giml.maven.pom_coordinates import PomError, read_pom_coordinates
from giml.maven.project import UnsupportedProjectError
from giml.store.sqlite_store import SqliteStateStore, StoreError


class ExitCode(enum.IntEnum):
    SUCCESS = 0
    NO_IMPROVEMENT = 1
    PREFLIGHT_REFUSAL = 2
    INELIGIBLE = 3
    INFRASTRUCTURE = 4
    CONFIGURATION = 5


class UsageError(ValueError):
    """Invalid arguments or configuration; exits with CONFIGURATION."""


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


@dataclass
class Environment:
    """Everything the CLI touches outside its arguments, injectable for tests."""

    fetcher: Fetcher = field(default_factory=UrlLibFetcher)
    clock: Callable[[], datetime.datetime] = _utc_now
    osv_url: str = osv.OSV_MAVEN_URL
    central_url: str = central.CENTRAL_URL
    environ: dict[str, str] = field(default_factory=lambda: dict(os.environ))
    maven: MavenRunner = run_maven
    java: JavaRunner = run_java
    sources: Sources = field(default_factory=Sources)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="giml", description="Gated Increments (ML)")
    parser.add_argument("--version", action="version", version=f"giml {__version__}")
    parser.add_argument("--state-dir", type=Path, help="state directory (default: $GIML_STATE_DIR or ~/.giml)")
    parser.add_argument("--config", type=Path, metavar="FILE", help="global config (default: ~/.giml/config.yml)")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    sync = commands.add_parser("sync", help="fetch/refresh data snapshots (network)")
    sync.add_argument("--osv", action="store_true", help="refresh the OSV advisory snapshot")
    sync.add_argument("--central", action="store_true", help="refresh the Maven Central metadata snapshot")
    sync.add_argument("path", nargs="?", type=Path, help="project directory or pom.xml whose coordinates to sync")
    sync.add_argument("--coordinate", action="append", default=[], metavar="G:A",
                      help="additional groupId:artifactId to sync (repeatable)")  # fmt: skip

    commands.add_parser("status", help="show snapshot ages, the state directory and crashed runs")

    assess = commands.add_parser("assess", help="measure test quality and earn a tier (runs Maven)")
    assess.add_argument("path", type=Path, help="project directory containing pom.xml")
    assess.add_argument("--declared-tier", metavar="TIER", help="tier the project aims for; misses are reported")
    assess.add_argument("--gate-config", type=Path, metavar="FILE", help="gate config (default: giml's own)")

    plan = commands.add_parser("plan", help="plan upgrades for a project (M2: sets up the workspace only)")
    plan.add_argument("path", type=Path, help="project directory containing pom.xml")
    plan.add_argument("--allow-detached", action="store_true", help="allow a detached HEAD as the base")
    plan.add_argument("--compare-naive", action="store_true",
                      help="also build each dependency's bump to its newest release on its own, to compare with the plan (costs builds)")
    plan.add_argument("--rewind-to", metavar="COMMIT", help="start from pom.xml as it was at COMMIT (synthetic)")
    plan.add_argument("--dry-run", action="store_true",
                      help="analyse only: resolve, match CVEs, list candidates and write a report; builds nothing")  # fmt: skip
    plan.add_argument("--strategy", choices=PLANNING_STRATEGIES, help="how far versions move (default: gate config)")
    plan.add_argument("--scope", choices=PLANNING_SCOPES, help="what may move: cve or general (default: gate config)")
    plan.add_argument("--major-updates", choices=MAJOR_UPDATE_MODES, help="major updates: disallowed, allowed or ml")
    plan.add_argument("--major-updates-test-scope", choices=TEST_SCOPE_MODES,
                      help="let major changes in test-only dependencies pass the major-update gate: allowed or disallowed")  # fmt: skip
    plan.add_argument("--gate-config", type=Path, metavar="FILE", help="gate config (default: giml's own)")

    export = commands.add_parser("export-examples", help="write the logged training examples as JSONL (spec 15)")
    export.add_argument("--out", type=Path, metavar="FILE", help="file to write (default: standard output)")

    clean = commands.add_parser("clean", help="remove giml worktrees (and optionally result branches)")
    clean.add_argument("path", nargs="?", type=Path, help="project directory (default: current directory)")
    clean.add_argument("--all", action="store_true", help="clean every project giml knows")
    clean.add_argument("--branches", action="store_true", help="also delete giml result branches")
    return parser


def state_dir(args: argparse.Namespace, env: Environment) -> Path:
    if args.state_dir:
        return args.state_dir
    if env.environ.get("GIML_STATE_DIR"):
        return Path(env.environ["GIML_STATE_DIR"])
    return Path.home() / ".giml"


def _central_coordinates(args: argparse.Namespace) -> set[Coordinate]:
    wanted: set[Coordinate] = set()
    if args.path:
        pom = args.path / "pom.xml" if args.path.is_dir() else args.path
        found = read_pom_coordinates(pom)
        for description in found.unresolved:
            print(f"warning: {pom}: skipped unresolvable {description}", file=sys.stderr)
        wanted |= found.coordinates
    for text in args.coordinate:
        try:
            wanted.add(Coordinate.parse(text))
        except ValueError as exc:
            raise UsageError(f"--coordinate: {exc}") from exc
    return wanted


def cmd_sync(args: argparse.Namespace, env: Environment, store: SqliteStateStore, root: Path) -> int:
    do_osv = args.osv or not args.central
    do_central = args.central or not args.osv
    if (args.path or args.coordinate) and not do_central:
        raise UsageError("a project path or --coordinate applies only to --central")
    wanted: set[Coordinate] = set()
    previous = store.latest_snapshot(central.SOURCE)
    if do_central:
        wanted = _central_coordinates(args)
        if not wanted and previous is None:
            raise UsageError("nothing to sync from Central: pass a project path or --coordinate")
    if do_osv:
        info = osv.sync(root, env.fetcher, env.clock, env.osv_url)
        store.record_snapshot(info)
        stats = read_manifest(info.path)["stats"]
        print(f"osv: {info.id} ({stats['advisories']} advisories, {stats['withdrawn']} withdrawn, "
              f"{stats['malformed']} malformed)")  # fmt: skip
    if do_central:
        info = central.sync(root, env.fetcher, env.clock, wanted, previous, env.central_url)
        store.record_snapshot(info)
        stats = read_manifest(info.path)["stats"]
        print(f"central: {info.id} ({stats['coordinates']} coordinates, {stats['versions']} versions, "
              f"{stats['not_found']} not found, {stats['release_dates_missing']} release dates unknown)")  # fmt: skip
    return ExitCode.SUCCESS


def _age(delta: datetime.timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def _describe(info: SnapshotInfo, now: datetime.datetime) -> str:
    stats = read_manifest(info.path)["stats"]
    counts = ", ".join(f"{k} {v}" for k, v in stats.items() if isinstance(v, int))
    fetched = info.fetched_at.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{info.id}  fetched {fetched} ({_age(now - info.fetched_at)})  hash {info.content_hash[:12]}  {counts}"


def cmd_status(env: Environment, store: SqliteStateStore, root: Path) -> int:
    now = env.clock()
    print(f"state dir: {root}")
    for source in (osv.SOURCE, central.SOURCE):
        info = store.latest_snapshot(source)
        if info is None:
            print(f"{source}: no snapshot (run `giml sync --{source}`)")
        else:
            print(f"{source}: {_describe(info, now)}")
    stale = workspace.stale_runs(root, store)
    print(f"crashed runs: {len(stale)}" + (" (remove with `giml clean <project>`)" if stale else ""))
    for entry in stale:
        started = entry.run.started_at.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"  {entry.run.id}  {entry.project_path}  started {started}  worktree {entry.run.worktree_path}")
    return ExitCode.SUCCESS


def cmd_dry_run(args: argparse.Namespace, env: Environment, store: SqliteStateStore, root: Path) -> int:
    if args.rewind_to:
        raise UsageError("--rewind-to is not supported with --dry-run yet")
    config = load_gate_config(args.gate_config or default_gate_config_path())
    overrides = {"strategy": args.strategy, "scope": args.scope, "major_updates": args.major_updates,
                 "major_updates_test_scope": args.major_updates_test_scope}  # fmt: skip
    options = dataclasses.replace(config.planning, **{k: v for k, v in overrides.items() if v})
    result = dry_run(args.path, root, store, env.clock, config, options, maven=env.maven, jdks=catalog(args.config),
                     environ=env.environ, sources=env.sources, allow_detached=args.allow_detached)  # fmt: skip
    for warning in [result.report["tier"]["note"], *result.report["warnings"]]:
        if warning:
            print(f"warning: {warning}", file=sys.stderr)
    print(render_summary(result.report), end="")
    print(f"report: {result.markdown_path}")
    return ExitCode.SUCCESS


def cmd_plan(args: argparse.Namespace, env: Environment, store: SqliteStateStore, root: Path) -> int:
    if args.dry_run:
        return cmd_dry_run(args, env, store, root)
    config = load_gate_config(args.gate_config or default_gate_config_path())
    overrides = {"strategy": args.strategy, "scope": args.scope, "major_updates": args.major_updates,
                 "major_updates_test_scope": args.major_updates_test_scope}  # fmt: skip
    options = dataclasses.replace(config.planning, **{k: v for k, v in overrides.items() if v})
    verified: list[BaselineRun] = []
    planned: list[PlanRun] = []

    def verify(ws: workspace.Workspace, temp) -> str | None:
        verified.append(run_baseline(ws, temp, root, config, catalog(args.config), env.environ, env.maven, store))
        if not verified[0].baseline.upgradeable:
            return verified[0].baseline.stop_reason
        planned.append(run_planning(ws, verified[0], root, store, config, options, env.maven, env.sources, env.clock, env.java, args.compare_naive))
        return planned[0].stop_reason

    ws = workspace.set_up(args.path, root, store, env.clock, args.allow_detached, args.rewind_to, verify)
    for run in ws.crashed_runs:
        print(f"warning: earlier run {run.id} never finished (crashed); its worktree {run.worktree_path} "
              "is left for inspection, remove it with `giml clean`", file=sys.stderr)  # fmt: skip
    print(f"run: {ws.run_id}")
    print(f"branch: {ws.branch}")
    print(f"worktree: {ws.worktree}")
    if ws.rewind is not None:
        print(f"rewound {len(ws.rewind.pom_paths)} pom.xml file(s) to {ws.rewind.sha[:7]} "
              f"(committed {ws.rewind.committed_at}); synthetic")  # fmt: skip
        for path in ws.rewind.kept_paths:
            print(f"kept at base (not present at {ws.rewind.sha[:7]}): {path}")
    baseline = verified[0]
    print_baseline(baseline)
    raise_if_stopped(baseline, ws.rewind.sha[:7] if ws.rewind else None)
    run = planned[0]
    print(render_plan_summary(run.report), end="")
    print(f"report: {run.markdown_path}")
    print(f"review: {run.report['result']['review']}")
    print(f"cleanup: giml clean {ws.repo.project_dir}")
    return ExitCode.SUCCESS if run.succeeded else ExitCode.NO_IMPROVEMENT


def print_baseline(run: BaselineRun) -> None:
    baseline, cache = run.baseline, run.report["cache"]
    for stage in baseline.stages:
        detail = f"{stage.outcome.duration_seconds:.1f} s" if stage.outcome else "not run"
        if stage.outcome is not None and not stage.outcome.passed:
            detail += f", {stage.outcome.failure_class} {stage.outcome.signature}"
        print(f"baseline {stage.stage}: {stage.status} ({detail}{', cached' if stage.outcome and stage.outcome.cache_hit else ''})")
    if baseline.enforcer_mode == "reference":
        subjects = ", ".join(v.identity for v in baseline.reference_violations)
        print(f"enforcer: {len(baseline.reference_violations)} violation(s) at the baseline become the reference set: {subjects}")
    print(f"cache: {cache['hits']} hit(s), {cache['misses']} miss(es), {cache['seconds_saved']:.1f} s saved")
    print(f"baseline report: {run.report_path}")


def raise_if_stopped(run: BaselineRun, rewind_sha: str | None) -> None:
    baseline, log = run.baseline, run.last_log
    if baseline.upgradeable:
        return
    if baseline.stop == "infrastructure":
        raise BaselineInfrastructureError(f"infrastructure failure at the baseline (not the project's fault, try again); see {log}")
    what = "build" if baseline.stop == "build_failed" else "unit tests"
    if rewind_sha:
        raise PrerequisiteError(f"rewind point {rewind_sha} is unusable: the {what} fail with its pom.xml files (spec 5.3); see {log}")
    raise PrerequisiteError(f"the {what} fail at the base commit with giml's setup applied; see {log}")


def cmd_assess(args: argparse.Namespace, env: Environment, store: SqliteStateStore, root: Path) -> int:
    config = load_gate_config(args.gate_config or default_gate_config_path())
    for warning in config.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    jdks = catalog(args.config)
    outcome = assess_project(args.path, root, store, env.clock, config, args.declared_tier,
                             maven=env.maven, java=env.java, jdks=jdks, environ=env.environ)  # fmt: skip
    result = outcome.result
    print(json.dumps(result, indent=2, sort_keys=True))
    print(f"branch: {outcome.branch}", file=sys.stderr)
    print(f"worktree: {outcome.worktree}", file=sys.stderr)
    print(f"report: {outcome.report_path}", file=sys.stderr)
    tier = result["passed_tier"] or "none (propose nothing)"
    print(f"earned tier: {tier}", file=sys.stderr)
    if result["setup_commit"]:
        print(f"giml added quality tooling in commit {result['setup_commit'][:7]}; cherry-pick it to keep it",
              file=sys.stderr)  # fmt: skip
    return ExitCode.SUCCESS


def cmd_export_examples(args: argparse.Namespace, store: SqliteStateStore) -> int:
    lines = list(example_lines(store))
    text = "".join(f"{line}\n" for line in lines)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"{len(lines)} example(s) written to {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
        print(f"{len(lines)} example(s)", file=sys.stderr)
    return ExitCode.SUCCESS


def cmd_clean(args: argparse.Namespace, env: Environment, store: SqliteStateStore, root: Path) -> int:
    if args.all and args.path:
        raise UsageError("pass a project path or --all, not both")
    for line in workspace.clean(root, store, env.clock, args.path, args.all, args.branches):
        print(line)
    return ExitCode.SUCCESS


def main(argv: Sequence[str] | None = None, env: Environment | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return ExitCode.CONFIGURATION
    env = env or Environment()
    root = state_dir(args, env)
    try:
        with SqliteStateStore(root / "state.db") as store:
            if args.command == "sync":
                return cmd_sync(args, env, store, root)
            if args.command == "assess":
                return cmd_assess(args, env, store, root)
            if args.command == "plan":
                return cmd_plan(args, env, store, root)
            if args.command == "clean":
                return cmd_clean(args, env, store, root)
            if args.command == "export-examples":
                return cmd_export_examples(args, store)
            return cmd_status(env, store, root)
    except (PreflightRefusal, LockHeld) as exc:
        print(f"giml: refused: {exc}", file=sys.stderr)
        return ExitCode.PREFLIGHT_REFUSAL
    except UnsupportedProjectError as exc:
        print(f"giml: unsupported: {exc}", file=sys.stderr)
        return ExitCode.INELIGIBLE
    except (UsageError, PomError, StoreError, RewindError, IdentityError, ConfigError, PrerequisiteError,
            UnknownTierError, MissingSnapshotError, ResolutionError) as exc:  # fmt: skip
        print(f"giml: error: {exc}", file=sys.stderr)
        return ExitCode.CONFIGURATION
    except (FetchError, MetadataError, GitError, BranchExistsError, ForeignWorktreeError, MavenNotFound,
            ReportError, InsufficientSpace, BaselineInfrastructureError, subprocess.TimeoutExpired, zipfile.BadZipFile,
            sqlite3.Error, OSError) as exc:  # fmt: skip
        print(f"giml: error: {exc}", file=sys.stderr)
        return ExitCode.INFRASTRUCTURE
