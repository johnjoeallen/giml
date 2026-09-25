"""The dry-run report (spec section 14): a JSON document and its Markdown rendering.

Built from finished analysis results and nothing else, so it is pure and re-renderable from the
stored JSON. A dry run builds and edits nothing, so its reason lines speak of candidates, never of
verified steps.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from giml.core.config import PlanningSettings
from giml.core.model import Coordinate, Finding
from giml.maven.declarations import ExternalParent, Site
from giml.maven.version import ComparableVersion
from giml.plan.candidates import Candidate, DependencyPlan
from giml.plan.exposure import ResolvedDependency, TreeExposure

_DRY_RUN = "a candidate, not built (dry run)"
_REPORT_ONLY = "report only: no usable tier, so nothing is proposed"
_DEFAULT_STATUSES = ("fix_available", "fix_blocked_major", "no_fix", "update_available")


@dataclass(frozen=True)
class TierStatus:
    """The stored assessment as the planner sees it (spec 6.4): usable only if valid and at least Tier B."""

    earned: str | None
    declared: str | None
    measured_at: str | None
    expires: str | None
    usable: bool
    note: str  # why it is not usable, or a warning; empty when there is nothing to say


@dataclass(frozen=True)
class ReportInputs:
    project: str
    base_sha: str
    run_id: str
    worktree: Path
    generated_at: datetime.datetime
    config_version: int
    options: PlanningSettings
    tier: TierStatus
    snapshots: dict[str, str]
    jdk: dict
    exposure: TreeExposure
    plans: Sequence[DependencyPlan]
    latest_available: dict[Coordinate, str | None]  # the newest release of each artifact, when Central has it
    parents: Sequence[ExternalParent]
    skipped: Sequence[str]
    warnings: Sequence[str]  # for example a stale snapshot


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _site(site: Site, root: Path) -> dict:
    return {"file": _relative(site.pom, root), "line": site.line, "kind": site.kind, "name": site.name}


def _advisories(findings: Sequence[Finding]) -> list[dict]:
    unique = {}
    for finding in findings:
        unique.setdefault(finding.advisory_id, finding)
    return [{"id": f.advisory_id, "cves": list(f.cves), "severity": f.severity.rating.name, "score": f.severity.score,
             "source": f.severity.source.value, "fixed": f.fixed} for f in unique.values()]  # fmt: skip


def _labels(findings: Sequence[Finding]) -> str:
    return ", ".join(sorted({cve for f in findings for cve in (f.cves or (f.advisory_id,))}))


def _bump(kind: str, current: str, candidate: Candidate, candidates: Sequence[Candidate]) -> str:
    level = {"cve_patch": "patch", "cve_minor": "minor", "cve_major": "major"}[kind]
    present = {k for c in candidates for k in c.kinds}
    missing = [name for name, k in (("patch", "cve_patch"), ("minor", "cve_minor")) if k not in present and name != level]
    lower = "no patch-level fix exists" if level == "minor" else "no patch- or minor-level fix exists"
    note = f"; {lower}" if level != "patch" and missing else ""
    return f"a {level} bump ({current} → {candidate.version}{note})"


def _aim(prefix: str, candidates: Sequence[Candidate]) -> str:
    usable = [c for c in candidates if c.blocked is None and c.clears is not False]
    aim, floor = usable[0], usable[-1]
    text = f"{prefix}aim for {aim.version} ({aim.kinds[0]})"
    if floor is not aim:
        text += f", demoting to {floor.version} ({floor.kinds[-1]}) if it fails"
    return text


def _major_of(version: str) -> str:
    match = re.match(r"\d+", version)
    return match.group() if match else version


def _cve_reason(plan: DependencyPlan, findings: Sequence[Finding], options: PlanningSettings) -> str:
    labels = _labels(findings)
    if plan.status == "fix_available":
        usable = [c for c in plan.candidates if c.blocked is None and c.clears]
        if options.strategy == "latest":
            return f"{_aim(f'{labels}: ', usable)}; {_DRY_RUN}"
        first = usable[0]
        kind = next(k for k in first.kinds if k.startswith("cve_"))
        return f"{labels} fixed by {_bump(kind, plan.version, first, plan.candidates)}; {_DRY_RUN}"
    if plan.status == "fix_blocked_major":
        version = min((c.version for c in plan.candidates if c.clears), key=ComparableVersion)
        update = f"a major update ({_major_of(plan.version)}.x to {version})"
        if options.major_updates == "ml":
            return f"{labels} left open: the only fix is {update}; `major_updates` is `ml` and there is no ML evidence"
        return f"{labels} left open: the only fix is {update} and `major_updates` is `{options.major_updates}`"
    if plan.status == "no_metadata":
        return f"{labels} left open: no Central metadata for {plan.coordinate}; run `giml sync --central --coordinate {plan.coordinate}`"
    if plan.held_by_cooldown:
        return (f"{labels} left open: no usable version clears it; {', '.join(plan.held_by_cooldown)} are inside the "
                f"{options.release_cooldown_days}-day release cooldown")  # fmt: skip
    return f"{labels} left open: no version that clears it is available"


def _plain_reason(plan: DependencyPlan, options: PlanningSettings) -> str:
    if plan.status == "unchanged_scope":
        return "no CVE, scope is `cve`, left unchanged"
    if plan.status == "up_to_date":
        return "no CVE, already at its newest patch and minor, left unchanged"
    if plan.status == "no_metadata":
        return f"no CVE; no Central metadata for {plan.coordinate}, so no update could be judged"
    if options.strategy == "latest":
        return f"{_aim('no CVE, ', plan.candidates)}; {_DRY_RUN}"
    first = plan.candidates[0]
    return f"no CVE, {'patch' if 'next_patch' in first.kinds else 'minor'} bump ({plan.version} → {first.version}) is the next step; {_DRY_RUN}"


def _report_only_reason(plan: DependencyPlan, findings: Sequence[Finding], newest: str | None) -> str:
    if findings:
        fixed = sorted({f.fixed for f in findings if f.fixed}, key=ComparableVersion)
        where = f"fixed in {', '.join(fixed)}" if fixed else "no fixed version recorded"
        return f"{_labels(findings)} open, {where} ({_REPORT_ONLY})"
    if newest is None:
        return "no CVE"
    if ComparableVersion(newest) > ComparableVersion(plan.version):
        return f"no CVE; {newest} is the newest release ({_REPORT_ONLY})"
    return "no CVE; already at the newest release"


def _candidate(candidate: Candidate) -> dict:
    return {"kinds": list(candidate.kinds), "version": candidate.version,
            "released_at": candidate.released_at.isoformat() if candidate.released_at else None,
            "date_unknown": candidate.date_unknown, "clears": candidate.clears, "remaining": list(candidate.remaining),
            "new_advisories": list(candidate.new_advisories), "blocked": candidate.blocked}  # fmt: skip


def _entry(inputs: ReportInputs, dependency: ResolvedDependency, plan: DependencyPlan) -> dict:
    newest = inputs.latest_available.get(dependency.coordinate)
    allowed = inputs.tier.usable
    if not allowed:
        reason = _report_only_reason(plan, dependency.findings, newest)
    elif dependency.findings:
        reason = _cve_reason(plan, dependency.findings, inputs.options)
    else:
        reason = _plain_reason(plan, inputs.options)
    return {"coordinate": str(dependency.coordinate), "version": dependency.version,
            "modules": [str(m) for m in dependency.modules], "scopes": list(dependency.scopes),
            "direct": dependency.direct, "cve_affected": bool(dependency.findings),
            "advisories": _advisories(dependency.findings), "status": plan.status, "reason": reason,
            "change": plan.change, "sites": [_site(s, inputs.worktree) for s in plan.sites],
            "candidates": [_candidate(c) for c in plan.candidates] if allowed else [],
            "held_by_cooldown": list(plan.held_by_cooldown), "latest_available": newest}  # fmt: skip


def _worst_first(dependency: ResolvedDependency) -> tuple:
    rating = max((int(f.severity.rating) for f in dependency.findings), default=-1)
    return (-rating, str(dependency.coordinate), dependency.version)


def build_report(inputs: ReportInputs) -> dict:
    """The dry-run report as plain JSON types."""
    plans = {(p.coordinate, p.version): p for p in inputs.plans}
    dependencies = sorted(inputs.exposure.dependencies, key=_worst_first)
    entries = [_entry(inputs, d, plans[(d.coordinate, d.version)]) for d in dependencies]
    statuses = [e["status"] for e in entries]
    missing = sorted(e["coordinate"] for e in entries if e["status"] == "no_metadata")
    exposure = inputs.exposure.exposure
    return {
        "kind": "dry_run", "project": inputs.project, "base_sha": inputs.base_sha, "run_id": inputs.run_id,
        "generated_at": inputs.generated_at.isoformat(),
        "config_version": inputs.config_version,
        "planning": {"strategy": inputs.options.strategy, "scope": inputs.options.scope,
                     "major_updates": inputs.options.major_updates,
                     "release_cooldown_days": inputs.options.release_cooldown_days},  # fmt: skip
        "tier": {"earned": inputs.tier.earned, "declared": inputs.tier.declared, "measured_at": inputs.tier.measured_at,
                 "expires": inputs.tier.expires, "usable": inputs.tier.usable, "note": inputs.tier.note},  # fmt: skip
        "proposals_allowed": inputs.tier.usable, "snapshots": dict(inputs.snapshots), "jdk": dict(inputs.jdk),
        "exposure": {"max_severity": exposure.max_severity.name if exposure.max_severity else None,
                     "at_max": exposure.at_max, "total": exposure.total},  # fmt: skip
        "summary": {"dependencies": len(entries), "direct": sum(e["direct"] for e in entries),
                    "cve_affected": sum(e["cve_affected"] for e in entries),
                    **{status: statuses.count(status) for status in _DEFAULT_STATUSES}},  # fmt: skip
        "dependencies": entries, "missing_metadata": missing,
        "sync_command": "giml sync --central " + " ".join(f"--coordinate {c}" for c in missing) if missing else None,
        "external_parents": [{"coordinate": str(p.coordinate), "version": p.version,
                              "file": _relative(p.pom, inputs.worktree), "line": p.site.line} for p in inputs.parents],  # fmt: skip
        "skipped_declarations": list(inputs.skipped), "warnings": list(inputs.warnings),
    }


def _advisory_line(advisory: dict) -> str:
    rating = advisory["severity"] + (f" {advisory['score']}" if advisory["score"] is not None else "")
    ids = f" ({', '.join(advisory['cves'])}, {rating})" if advisory["cves"] else f" ({rating})"
    fixed = f", fixed in {advisory['fixed']}" if advisory["fixed"] else ""
    return f"{advisory['id']}{ids}{fixed}"


def _declared_in(entry: dict) -> str:
    if entry["sites"]:
        return "; ".join(f"{s['file']}:{s['line']} ({s['kind']}{' `' + s['name'] + '`' if s['name'] else ''})" for s in entry["sites"])
    if entry["change"] == "pin":
        return "not declared with a version the reactor can edit; a `dependencyManagement` pin would be added"
    return "unsupported: giml cannot edit where this version comes from"


def _candidate_text(candidate: dict) -> str:
    text = f"{'/'.join(candidate['kinds'])} {candidate['version']}"
    notes = []
    if candidate["clears"] is not None:
        notes.append("clears the CVEs" if candidate["clears"] else "still affected by " + ", ".join(candidate["remaining"]))
    if candidate["new_advisories"]:
        notes.append("new advisories: " + ", ".join(candidate["new_advisories"]))
    if candidate["blocked"]:
        notes.append("blocked, " + candidate["blocked"])
    if candidate["date_unknown"]:
        notes.append("release date unknown")
    return text + (f" ({'; '.join(notes)})" if notes else "")


def render_markdown(report: dict) -> str:
    """The Markdown form of a stored dry-run report."""
    tier, planning, exposure = report["tier"], report["planning"], report["exposure"]
    lines = [f"# giml dry run: {report['project']}", "",
             f"Base commit: `{report['base_sha']}`. Run `{report['run_id']}`, generated {report['generated_at']}.",
             f"Tier: {tier['earned'] or 'none'}" + (f" (measured {tier['measured_at']}, expires {tier['expires']})" if tier["usable"] else f". {tier['note']}"),
             f"Strategy `{planning['strategy']}`, scope `{planning['scope']}`, major updates `{planning['major_updates']}`, "
             f"cooldown {planning['release_cooldown_days']} days. Gate config version {report['config_version']}.",
             f"Snapshots: OSV `{report['snapshots'].get('osv')}`, Central `{report['snapshots'].get('central')}`. "
             f"JDK {report['jdk'].get('version')} ({report['jdk'].get('source')}).",  # fmt: skip
             f"Exposure: worst severity {exposure['max_severity'] or 'none'}, {exposure['at_max']} at that severity, "
             f"{exposure['total']} vulnerabilities in all."]  # fmt: skip
    if not report["proposals_allowed"]:
        lines += ["", "**Report only:** no usable tier, so no candidates are proposed."]
    summary = report["summary"]
    if report["warnings"]:
        lines += ["", "## Warnings", "", *[f"- {w}" for w in report["warnings"]]]
    lines += ["", "## Summary", "", "| " + " | ".join(summary) + " |", "|" + "---|" * len(summary),
              "| " + " | ".join(str(v) for v in summary.values()) + " |"]  # fmt: skip
    affected = [e for e in report["dependencies"] if e["cve_affected"]]
    others = [e for e in report["dependencies"] if not e["cve_affected"]]
    lines += ["", "## CVE-affected dependencies", ""]
    if not affected:
        lines += ["None."]
    for entry in affected:
        lines += [f"### {entry['coordinate']} {entry['version']}", "", entry["reason"], "",
                  "- Advisories: " + "; ".join(_advisory_line(a) for a in entry["advisories"]),
                  f"- Declared in: {_declared_in(entry)}",
                  f"- Direct in: {', '.join(entry['modules'])}" if entry["direct"] else f"- Transitive, in: {', '.join(entry['modules'])}"]  # fmt: skip
        if entry["candidates"]:
            lines += ["- Candidates: " + "; ".join(_candidate_text(c) for c in entry["candidates"])]
        lines += [""]
    lines += ["## Other dependencies", ""]
    lines += [f"- {e['coordinate']} {e['version']}: {e['reason']}" for e in others] or ["None."]
    if report["missing_metadata"]:
        lines += ["", "## Missing Central metadata", "", "Run `" + report["sync_command"] + "` and plan again."]
    notes = [f"- External parent {p['coordinate']} {p['version']} ({p['file']}:{p['line']}): its version is the declaration site for everything it manages"
             for p in report["external_parents"]] + [f"- Skipped: {s}" for s in report["skipped_declarations"]]  # fmt: skip
    if notes:
        lines += ["", "## Notes", "", *notes]
    lines += ["", "---", "", "dry run: nothing was built or edited; the candidates above are untested."]
    return "\n".join(lines) + "\n"
