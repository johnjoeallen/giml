from giml.plan.planner import render_plan_markdown
from giml.plan.report import build_report  # noqa: F401


def test_the_markdown_ends_with_the_per_dependency_analysis():
    report = {"kind": "plan", "project": "p", "base_sha": "a" * 40, "run_id": "r", "generated_at": "t", "config_version": 3,
              "planning": {"strategy": "conservative", "scope": "cve", "major_updates": "disallowed", "major_updates_test_scope": "disallowed",
                           "release_cooldown_days": 7},
              "tier": {"earned": "A", "usable": True, "measured_at": "m", "expires": "e", "note": ""},
              "snapshots": {}, "jdk": {}, "exposure": {"max_severity": None, "at_max": 0, "total": 0}, "proposals_allowed": True,
              "summary": {"dependencies": 0, "direct": 0, "cve_affected": 0}, "dependencies": [], "change_units": [], "warnings": [],
              "missing_metadata": [], "external_parents": [], "skipped_declarations": [],
              "result": {"branch": "b", "review": "git diff x..b", "committed": [], "left": [], "builds": 0, "stop_reason": "complete",
                         "remaining_violations": [], "enforcer_clean": True}}
    text = render_plan_markdown(report)
    assert text.startswith("# giml plan: p") and "## Analysis at the base commit" in text and "git diff x..b" in text
