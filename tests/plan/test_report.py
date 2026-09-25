import datetime
import json
from pathlib import Path

from giml.core.config import PlanningSettings
from giml.core.model import Coordinate, Finding, Severity, SeverityRating, SeveritySource
from giml.maven.declarations import ExternalParent, Site
from giml.plan.candidates import Candidate, DependencyPlan
from giml.plan.exposure import ResolvedDependency, TreeExposure, exposure_of
from giml.plan.report import ReportInputs, TierStatus, build_report, render_markdown, render_summary

NOW = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.UTC)
ROOT = Path("/state/worktrees/proj/run")
MODULE = Coordinate.parse("g:core")


def c(text):
    return Coordinate.parse(text)


def finding(advisory, coordinate, version, rating=SeverityRating.HIGH, fixed="9.0", cves=None, score=7.5):
    return Finding(advisory, tuple(cves if cves is not None else [f"CVE-{advisory}"]),
                   Severity(rating, SeveritySource.CVSS_V3, score), c(coordinate), version, "0", fixed)  # fmt: skip


def resolved(coordinate, version, findings=(), direct=True):
    return ResolvedDependency(c(coordinate), version, (MODULE,), ("compile",), direct, tuple(findings))


def candidate(kinds, version, clears=True, blocked=None, new=(), remaining=(), unknown=False):
    return Candidate(tuple(kinds), version, NOW, unknown, clears, tuple(remaining), tuple(new), blocked)


def plan(dep, status, candidates=(), change="edit", sites=(), held=()):
    return DependencyPlan(dep.coordinate, dep.version, bool(dep.findings), tuple(sorted({f.advisory_id for f in dep.findings})),
                          status, change, tuple(sites), tuple(candidates), tuple(held))  # fmt: skip


def tier(usable=True, earned="A"):
    return TierStatus(earned, None, "2026-09-20T00:00:00+00:00", "2026-10-20T00:00:00+00:00", usable,
                      "" if usable else "no assessment; run giml assess")  # fmt: skip


def inputs(deps, plans, options=None, tier_status=None, latest=None, **extra) -> ReportInputs:
    exposure = TreeExposure(tuple(deps), exposure_of(f for d in deps for f in d.findings), "osv-1")
    return ReportInputs(
        project="proj", base_sha="abc1234def", run_id="run-1", worktree=ROOT, generated_at=NOW, config_version=3,
        options=options or PlanningSettings(7, 60, 120, "conservative", "cve", "disallowed", 7),
        tier=tier_status or tier(), snapshots={"osv": "osv-1", "central": "central-1"},
        jdk={"version": "21.0.9", "home": "/jdk", "source": "global config"}, exposure=exposure, plans=tuple(plans),
        latest_available=latest or {}, parents=extra.get("parents", ()), skipped=extra.get("skipped", ()),
        warnings=extra.get("warnings", ()),
    )  # fmt: skip


def reason_of(report, coordinate):
    return next(d["reason"] for d in report["dependencies"] if d["coordinate"] == coordinate)


def one_dep(dep, p, **kw):
    return build_report(inputs([dep], [p], **kw))


def test_patch_fix_reason():
    dep = resolved("o:l", "2.17.1", [finding("A", "o:l", "2.17.1")])
    p = plan(dep, "fix_available", [candidate(["cve_patch"], "2.17.3")])
    assert reason_of(one_dep(dep, p), "o:l") == "CVE-A fixed by a patch bump (2.17.1 → 2.17.3); a candidate, not built (dry run)"


def test_minor_fix_says_why_there_is_no_patch_fix_and_lists_every_cve():
    dep = resolved("o:l", "2.17.1", [finding("A", "o:l", "2.17.1"), finding("B", "o:l", "2.17.1", cves=[])])
    p = plan(dep, "fix_available", [candidate(["cve_minor"], "2.18.0")])
    assert reason_of(one_dep(dep, p), "o:l") == (
        "B, CVE-A fixed by a minor bump (2.17.1 → 2.18.0; no patch-level fix exists); a candidate, not built (dry run)")


def test_fix_after_a_blocked_step_names_the_first_usable_one():
    dep = resolved("o:l", "1.0.0", [finding("A", "o:l", "1.0.0")])
    p = plan(dep, "fix_available", [candidate(["cve_patch"], "1.0.2"), candidate(["cve_minor"], "1.1.0")])
    assert "patch bump (1.0.0 → 1.0.2)" in reason_of(one_dep(dep, p), "o:l")


def test_major_only_fix_is_left_open_with_the_mode():
    dep = resolved("o:l", "2.17.1", [finding("A", "o:l", "2.17.1")])
    blocked = candidate(["cve_major"], "3.0.1", blocked="major update: major_updates is disallowed")
    report = one_dep(dep, plan(dep, "fix_blocked_major", [blocked]))
    assert reason_of(report, "o:l") == "CVE-A left open: the only fix is a major update (2.x to 3.0.1) and `major_updates` is `disallowed`"
    ml = PlanningSettings(7, 60, 120, "conservative", "cve", "ml", 7)
    blocked_ml = candidate(["cve_major"], "3.0.1", blocked="major update: major_updates is ml and there is no ML evidence")
    assert "`major_updates` is `ml` and there is no ML evidence" in reason_of(
        one_dep(dep, plan(dep, "fix_blocked_major", [blocked_ml]), options=ml), "o:l")  # fmt: skip


def test_no_fix_reason_mentions_cooldown_and_metadata():
    dep = resolved("o:l", "1.0.0", [finding("A", "o:l", "1.0.0")])
    assert reason_of(one_dep(dep, plan(dep, "no_fix")), "o:l") == "CVE-A left open: no version that clears it is available"
    held = plan(dep, "no_fix", held=["1.0.1", "1.0.2"])
    assert reason_of(one_dep(dep, held), "o:l") == (
        "CVE-A left open: no usable version clears it; 1.0.1, 1.0.2 are inside the 7-day release cooldown")
    assert reason_of(one_dep(dep, plan(dep, "no_metadata")), "o:l") == (
        "CVE-A left open: no Central metadata for o:l; run `giml sync --central --coordinate o:l`")


def test_latest_strategy_reason_names_the_aspirational_candidate():
    dep = resolved("o:l", "2.17.1", [finding("A", "o:l", "2.17.1")])
    p = plan(dep, "fix_available", [candidate(["latest_in_major"], "2.19.4"), candidate(["cve_patch"], "2.17.3")])
    options = PlanningSettings(7, 60, 120, "latest", "cve", "disallowed", 7)
    assert reason_of(one_dep(dep, p, options=options), "o:l") == (
        "CVE-A: aim for 2.19.4 (latest_in_major), demoting to 2.17.3 (cve_patch) if it fails; a candidate, not built (dry run)")


def test_reasons_for_dependencies_without_a_cve():
    dep = resolved("o:m", "1.0.0")
    assert reason_of(one_dep(dep, plan(dep, "unchanged_scope")), "o:m") == "no CVE, scope is `cve`, left unchanged"
    assert reason_of(one_dep(dep, plan(dep, "up_to_date")), "o:m") == "no CVE, already at its newest patch and minor, left unchanged"
    up = plan(dep, "update_available", [candidate(["next_patch"], "1.0.3")])
    assert reason_of(one_dep(dep, up), "o:m") == "no CVE, patch bump (1.0.0 → 1.0.3) is the next step; a candidate, not built (dry run)"
    minor = plan(dep, "update_available", [candidate(["next_minor"], "1.1.0")])
    assert "minor bump (1.0.0 → 1.1.0)" in reason_of(one_dep(dep, minor), "o:m")
    assert reason_of(one_dep(dep, plan(dep, "no_metadata")), "o:m") == "no CVE; no Central metadata for o:m, so no update could be judged"


def test_without_a_usable_tier_nothing_is_proposed_but_the_findings_stay():
    dep = resolved("o:l", "2.17.1", [finding("A", "o:l", "2.17.1", fixed="2.17.3")], direct=True)
    p = plan(dep, "fix_available", [candidate(["cve_patch"], "2.17.3")])
    report = one_dep(dep, p, tier_status=tier(usable=False, earned=None), latest={c("o:l"): "2.19.4"})
    entry = report["dependencies"][0]
    assert report["proposals_allowed"] is False and entry["candidates"] == []
    assert entry["reason"] == "CVE-A open, fixed in 2.17.3 (report only: no usable tier, so nothing is proposed)"
    assert entry["latest_available"] == "2.19.4" and entry["advisories"][0]["fixed"] == "2.17.3"
    assert report["tier"]["usable"] is False and "giml assess" in report["tier"]["note"]


def test_report_only_listing_marks_outdated_dependencies():
    dep = resolved("o:m", "1.0.0")
    report = one_dep(dep, plan(dep, "unchanged_scope"), tier_status=tier(usable=False, earned=None), latest={c("o:m"): "1.4.0"})
    assert reason_of(report, "o:m") == "no CVE; 1.4.0 is the newest release (report only: no usable tier, so nothing is proposed)"
    up_to_date = one_dep(dep, plan(dep, "unchanged_scope"), tier_status=tier(usable=False, earned=None), latest={c("o:m"): "1.0.0"})
    assert reason_of(up_to_date, "o:m") == "no CVE; already at the newest release"


def test_advisory_details_candidates_and_sites_are_recorded():
    dep = resolved("o:l", "2.17.1", [finding("A", "o:l", "2.17.1", fixed="2.17.3", cves=["CVE-1", "CVE-2"], score=8.1)])
    site = Site(ROOT / "core" / "pom.xml", 12, (100, 106), "property", "l.version")
    p = plan(dep, "fix_available", [candidate(["cve_patch"], "2.17.3", new=["N"], remaining=[])], sites=[site], held=["2.17.4"])
    entry = one_dep(dep, p)["dependencies"][0]
    assert entry["advisories"] == [{"id": "A", "cves": ["CVE-1", "CVE-2"], "severity": "HIGH", "score": 8.1,
                                    "source": "cvss_v3", "fixed": "2.17.3"}]  # fmt: skip
    assert entry["sites"] == [{"file": "core/pom.xml", "line": 12, "kind": "property", "name": "l.version"}]
    assert entry["candidates"] == [{"kinds": ["cve_patch"], "version": "2.17.3", "released_at": NOW.isoformat(),
                                    "date_unknown": False, "clears": True, "remaining": [], "new_advisories": ["N"],
                                    "blocked": None}]  # fmt: skip
    assert (entry["change"], entry["held_by_cooldown"], entry["modules"], entry["scopes"], entry["direct"]) == (
        "edit", ["2.17.4"], ["g:core"], ["compile"], True)  # fmt: skip


def test_dependencies_are_ordered_worst_first_then_by_coordinate():
    low = resolved("o:low", "1", [finding("L", "o:low", "1", SeverityRating.LOW)])
    crit = resolved("o:crit", "1", [finding("C", "o:crit", "1", SeverityRating.CRITICAL)])
    z, a = resolved("z:clean", "1"), resolved("a:clean", "1")
    plans = [plan(d, "unchanged_scope" if not d.findings else "no_fix") for d in (z, low, a, crit)]
    report = build_report(inputs([z, low, a, crit], plans))
    assert [d["coordinate"] for d in report["dependencies"]] == ["o:crit", "o:low", "a:clean", "z:clean"]


def test_summary_counts_and_exposure():
    fix = resolved("o:fix", "1", [finding("A", "o:fix", "1")])
    stuck = resolved("o:stuck", "1", [finding("B", "o:stuck", "1", SeverityRating.MEDIUM)])
    blocked = resolved("o:blocked", "1", [finding("C", "o:blocked", "1", SeverityRating.LOW)], direct=False)
    ok = resolved("o:ok", "1")
    plans = [plan(fix, "fix_available", [candidate(["cve_patch"], "2")]), plan(stuck, "no_fix"),
             plan(blocked, "fix_blocked_major", [candidate(["cve_major"], "9", blocked="x")]), plan(ok, "up_to_date")]  # fmt: skip
    report = build_report(inputs([fix, stuck, blocked, ok], plans))
    assert report["summary"] == {"dependencies": 4, "direct": 3, "cve_affected": 3, "fix_available": 1,
                                 "fix_blocked_major": 1, "no_fix": 1, "update_available": 0}  # fmt: skip
    assert report["exposure"] == {"max_severity": "HIGH", "at_max": 1, "total": 3}
    assert report["missing_metadata"] == []


def test_missing_metadata_is_listed_with_the_command_that_fixes_it():
    dep = resolved("o:l", "1", [finding("A", "o:l", "1")])
    other = resolved("o:ok", "1")
    report = build_report(inputs([dep, other], [plan(dep, "no_metadata"), plan(other, "no_metadata")]))
    assert report["missing_metadata"] == ["o:l", "o:ok"]
    assert report["sync_command"] == "giml sync --central --coordinate o:l --coordinate o:ok"


def test_header_fields_and_declaration_notes():
    parent = ExternalParent(ROOT / "pom.xml", c("org.springframework.boot:spring-boot-starter-parent"), "3.3.5",
                            Site(ROOT / "pom.xml", 10, (1, 2), "version"))  # fmt: skip
    dep = resolved("o:m", "1")
    report = build_report(inputs([dep], [plan(dep, "up_to_date")], parents=(parent,), skipped=("pom.xml: dependencies ${g}:a",)))
    assert (report["kind"], report["project"], report["run_id"], report["base_sha"]) == ("dry_run", "proj", "run-1", "abc1234def")
    assert report["planning"] == {"strategy": "conservative", "scope": "cve", "major_updates": "disallowed",
                                  "release_cooldown_days": 7}  # fmt: skip
    assert report["snapshots"] == {"osv": "osv-1", "central": "central-1"} and report["config_version"] == 3
    assert report["external_parents"] == [{"coordinate": "org.springframework.boot:spring-boot-starter-parent",
                                           "version": "3.3.5", "file": "pom.xml", "line": 10}]  # fmt: skip
    assert report["skipped_declarations"] == ["pom.xml: dependencies ${g}:a"]
    assert report["generated_at"] == NOW.isoformat() and report["proposals_allowed"] is True
    json.dumps(report)  # plain JSON types only


def test_warnings_are_carried_into_both_forms():
    dep = resolved("o:m", "1")
    report = build_report(inputs([dep], [plan(dep, "up_to_date")], warnings=("osv snapshot osv-1 is 9 days old",)))
    assert report["warnings"] == ["osv snapshot osv-1 is 9 days old"]
    assert "## Warnings\n\n- osv snapshot osv-1 is 9 days old" in render_markdown(report)
    assert "## Warnings" not in render_markdown(build_report(inputs([dep], [plan(dep, "up_to_date")])))


def test_markdown_rendering():
    dep = resolved("o:l", "2.17.1", [finding("A", "o:l", "2.17.1", fixed="2.17.3")])
    fixed = plan(dep, "fix_available", [candidate(["cve_patch"], "2.17.3")])
    ok = resolved("o:ok", "1")
    report = build_report(inputs([dep, ok], [fixed, plan(ok, "up_to_date")]))
    text = render_markdown(report)
    assert text.startswith("# giml dry run: proj\n")
    for line in ("Base commit: `abc1234def`", "Tier: A", "Strategy `conservative`, scope `cve`, major updates `disallowed`",
                 "## CVE-affected dependencies", "### o:l 2.17.1", "CVE-A fixed by a patch bump (2.17.1 → 2.17.3)",
                 "Advisories: A (CVE-A, HIGH 7.5), fixed in 2.17.3", "cve_patch 2.17.3", "## Other dependencies",
                 "no CVE, already at its newest patch and minor, left unchanged", "dry run: nothing was built or edited"):
        assert line in text, line
    assert text.endswith("\n")


def test_markdown_for_a_report_without_a_usable_tier_and_with_missing_metadata():
    dep = resolved("o:l", "1", [finding("A", "o:l", "1")])
    report = build_report(inputs([dep], [plan(dep, "no_metadata")], tier_status=tier(usable=False, earned=None)))
    text = render_markdown(report)
    assert "Tier: none. no assessment; run giml assess" in text and "report only" in text.lower()
    assert "Missing Central metadata" in text and "`giml sync --central --coordinate o:l`" in text



def test_summary_text_for_the_terminal():
    dep = resolved("o:l", "2.17.1", [finding("A", "o:l", "2.17.1", fixed="2.17.3")])
    fixed = plan(dep, "fix_available", [candidate(["cve_patch"], "2.17.3")])
    ok, missing = resolved("o:ok", "1"), resolved("o:gap", "1", [finding("G", "o:gap", "1", SeverityRating.LOW)])
    report = build_report(inputs([dep, ok, missing], [fixed, plan(ok, "up_to_date"), plan(missing, "no_metadata")]))
    text = render_summary(report)
    assert text.splitlines() == [
        "dry run: proj at abc1234 (tier A; strategy conservative, scope cve, major updates disallowed)",
        "exposure: worst HIGH, 1 at that severity, 2 in all; 3 dependencies, 2 CVE-affected",
        "  o:l 2.17.1: CVE-A fixed by a patch bump (2.17.1 → 2.17.3); a candidate, not built (dry run)",
        "  o:gap 1: CVE-G left open: no Central metadata for o:gap; run `giml sync --central --coordinate o:gap`",
        "missing Central metadata for 1 coordinate(s); run: giml sync --central --coordinate o:gap",
    ]


def test_summary_text_without_findings_or_a_tier():
    dep = resolved("o:ok", "1")
    text = render_summary(build_report(inputs([dep], [plan(dep, "up_to_date")], tier_status=tier(usable=False, earned=None))))
    assert text.splitlines() == [
        "dry run: proj at abc1234 (tier none, report only; strategy conservative, scope cve, major updates disallowed)",
        "exposure: none; 1 dependencies, 0 CVE-affected",
    ]
