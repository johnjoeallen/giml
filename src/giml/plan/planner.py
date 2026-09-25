"""`giml plan` after the baseline: analyse, search, commit, and report (spec sections 8, 13 and 14).

The analysis runs on the result worktree itself (its edits to parent and BOM versions are always
restored), so the changes it produces name files there; trials rebase them onto their own worktrees.
The final enforcer state is measured on the result worktree, because a plan succeeds only if that
state has no violation (spec 8.5), and an untouched branch cannot claim that.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from giml.core.config import GateConfig, PlanningSettings
from giml.data import central, osv
from giml.git.worktrees import WorktreeManager
from giml.maven.enforcer import Violation
from giml.maven.project import discover_reactor
from giml.plan.analysis import Analysis, MissingSnapshotError, Sources, analyse, newest_release, tier_status
from giml.plan.baseline_run import MAVEN_TIMEOUT_SECONDS, BaselineRun
from giml.plan.execute import PlanOutcome, deferral_records, execute
from giml.plan.report import ReportInputs, build_report
from giml.plan.trial import TrialRunner
from giml.maven.runner import MavenRunner
from giml.workspace import Workspace

STOP_PLANNED = "planned"
STOP_NO_TIER = "no_usable_tier"


@dataclass(frozen=True)
class PlanRun:
    outcome: PlanOutcome | None  # None when the tier allows no proposals
    remaining_violations: tuple[Violation, ...]
    report: dict
    json_path: Path
    markdown_path: Path
    stop_reason: str

    @property
    def enforcer_clean(self) -> bool:
        return not self.remaining_violations

    @property
    def succeeded(self) -> bool:
        return self.outcome is not None and bool(self.outcome.committed) and self.enforcer_clean


def _final_violations(baseline_run: BaselineRun, project_dir: Path, committed: bool) -> tuple[Violation, ...]:
    baseline = baseline_run.baseline
    if not committed or baseline.enforcer_mode not in ("clean", "reference"):
        return baseline.reference_violations
    outcome = baseline_run.runner.run_stage(project_dir, "enforcer", MAVEN_TIMEOUT_SECONDS)
    return outcome.violations


def run_planning(ws: Workspace, baseline_run: BaselineRun, state_dir: Path, store, config: GateConfig, options: PlanningSettings,
                 maven: MavenRunner, sources: Sources, clock: Callable[[], datetime.datetime]) -> PlanRun:  # fmt: skip
    now = clock()
    osv_snapshot, central_snapshot = store.latest_snapshot(osv.SOURCE), store.latest_snapshot(central.SOURCE)
    if osv_snapshot is None:
        raise MissingSnapshotError("no OSV snapshot; run `giml sync`")
    project_dir = ws.worktree / ws.repo.subdir if ws.repo.subdir else ws.worktree
    poms = list(discover_reactor(project_dir))
    logs = state_dir / "runs" / ws.run_id / "logs"
    tier = tier_status(store.latest_gate_result(ws.repo.project_key), config, now, ws.repo.base_sha)

    def reanalyse(log_name: str, units: bool) -> Analysis:
        return analyse(project_dir, poms, logs, MAVEN_TIMEOUT_SECONDS, maven, baseline_run.env, sources, osv_snapshot,
                       central_snapshot, options, tier, clock(), log_name, units)  # fmt: skip

    first = reanalyse("01-tree.log", tier.usable)
    outcome = None
    if tier.usable:
        trial = TrialRunner(WorktreeManager(ws.repo, state_dir, ws.run_id), ws.worktree, ws.repo.subdir or "", baseline_run.runner,
                            baseline_run.baseline, MAVEN_TIMEOUT_SECONDS)  # fmt: skip
        outcome = execute(ws.worktree, ws.run_id, tier.earned or "", first, reanalyse, trial, options, clock, project_dir / "pom.xml")
        for record in deferral_records(outcome, ws.repo.project_key, ws.run_id, clock()):
            store.save_deferral(record)
    remaining = _final_violations(baseline_run, project_dir, bool(outcome and outcome.committed))
    report = build_report(ReportInputs(
        project=ws.repo.project_dir.name, base_sha=ws.repo.base_sha, run_id=ws.run_id, worktree=ws.worktree, generated_at=now,
        config_version=config.version, options=options, tier=tier,
        snapshots={"osv": osv_snapshot.id, "central": central_snapshot.id if central_snapshot else None},
        jdk=baseline_run.jdk.record(), exposure=first.exposure, plans=first.plans,
        latest_available={c: newest_release(v) for c, v in first.available.items()},
        parents=first.declarations.parents, skipped=first.declarations.skipped, units=first.unit_plans, warnings=[]))  # fmt: skip
    report["kind"] = "plan"
    report["result"] = result_section(ws, outcome, remaining)
    directory = state_dir / "reports" / ws.run_id
    json_path, markdown_path = directory / "plan.json", directory / "plan.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_plan_markdown(report), encoding="utf-8")
    return PlanRun(outcome, remaining, report, json_path, markdown_path, STOP_PLANNED if outcome else STOP_NO_TIER)


def _exposure_dict(exposure) -> dict:
    return {"max_severity": exposure.max_severity.name if exposure.max_severity else None, "at_max": exposure.at_max, "total": exposure.total}


def result_section(ws: Workspace, outcome: PlanOutcome | None, remaining: tuple[Violation, ...]) -> dict:
    section: dict = {"branch": ws.branch, "worktree": str(ws.worktree), "review": f"git diff {ws.repo.base_sha[:7]}..{ws.branch}",
                     "remaining_violations": [v.identity for v in remaining], "enforcer_clean": not remaining}  # fmt: skip
    if outcome is None:
        return {**section, "committed": [], "left": [], "builds": 0, "stop_reason": STOP_NO_TIER}
    return {**section, "stop_reason": outcome.stop_reason, "stop_detail": outcome.stop_detail, "builds": outcome.builds,
            "exposure_before": _exposure_dict(outcome.exposure_before), "exposure_after": _exposure_dict(outcome.exposure_after),
            "committed": [{"label": c.label, "kind": c.kind, "sha": c.sha, "clears": list(c.clears), "edits": list(c.edits)}
                          for c in outcome.committed],
            "left": [{"key": e.key, "coordinates": list(e.members), "reason": e.reason} for e in outcome.left]}  # fmt: skip


def _exposure_text(exposure: Mapping) -> str:
    if not exposure["max_severity"]:
        return "none"
    return f"worst {exposure['max_severity']}, {exposure['at_max']} at that severity, {exposure['total']} in all"


def render_plan_summary(report: dict) -> str:
    result = report["result"]
    lines = [f"plan: {report['project']} at {report['base_sha'][:7]}: {len(result['committed'])} step(s) committed, "
             f"{len(result['left'])} left, {result['builds']} build(s), stop reason {result['stop_reason']}"]  # fmt: skip
    if "exposure_before" in result:
        lines.append(f"exposure: {_exposure_text(result['exposure_before'])} -> {_exposure_text(result['exposure_after'])}")
    lines += [f"  + {c['label']} ({c['sha'][:7]})" for c in result["committed"]]
    lines += [f"  - {e['key']}: {e['reason']}" for e in result["left"]]
    if result["remaining_violations"]:
        lines.append(f"enforcer: {len(result['remaining_violations'])} violation(s) remain: {', '.join(result['remaining_violations'])}")
    else:
        lines.append("enforcer: clean")
    if report["tier"]["usable"] is False:
        lines.append(f"report only: {report['tier']['note']}")
    return "\n".join(lines) + "\n"


def render_plan_markdown(report: dict) -> str:
    result = report["result"]
    lines = [f"# giml plan: {report['project']}", "", f"Base commit `{report['base_sha']}`. Run `{report['run_id']}`. Branch `{result['branch']}`.",
             "", render_plan_summary(report).replace("\n", "  \n").rstrip(), ""]  # fmt: skip
    for entry in result["committed"]:
        lines += [f"## {entry['label']}", f"Commit `{entry['sha']}`. " + (f"Clears {', '.join(entry['clears'])}." if entry["clears"] else "")]
        lines += [f"- {edit}" for edit in entry["edits"]] + [""]
    lines += ["## Review", f"`{result['review']}`", "", "## Clean up", "`giml clean <project>`", ""]
    return "\n".join(lines)
