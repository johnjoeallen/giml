"""The survey script's table formatting (scripts/survey.py)."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("survey", ROOT / "scripts" / "survey.py")
survey = importlib.util.module_from_spec(_spec)
sys.modules["survey"] = survey
_spec.loader.exec_module(survey)

RESULT = {
    "project": "demo",
    "tools": {"jdk": {"version": "17.0.16", "home": "/jdk", "source": "global config"}},
    "measured": {"unit_line_coverage": 81.234, "unit_branch_coverage": None,
                 "pit_test_strength": 90.0, "pit_mutation_coverage": 72.5},  # fmt: skip
    "excluded_share": 0.0,
    "flaky_tests": ["T#a"],
    "untested_modules": [],
    "enforcer": {"status": "baseline_failed", "failed_rules": ["DependencyConvergence"]},
    "tooling_added": ["a", "b", "c"],
    "passed_tier": None,
}


def test_row_formats_numbers_missing_values_and_enforcer_rules():
    assert survey.row(RESULT) == ["demo", "17.0.16 (global config)", "81.2", "n/a", "90.0", "72.5", "0.0", "1", "none",
                                  "baseline_failed (DependencyConvergence)", "3", "none"]  # fmt: skip


def test_table_has_header_separator_and_one_row_per_result():
    lines = survey.table([RESULT, dict(RESULT, project="other", passed_tier="B")]).splitlines()
    assert lines[0].startswith("| project | jdk | line % |") and lines[1] == "|" + "---|" * len(survey.COLUMNS)
    assert len(lines) == 4 and lines[3].endswith("| B |")
