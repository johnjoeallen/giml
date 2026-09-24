"""Survey run (spec M3): assess several projects and tabulate the four tier metrics side by side,
to calibrate the tier numbers before the gate config version is locked.

Usage: python scripts/survey.py [--state-dir DIR] PROJECT [PROJECT ...]
Run it on scratch clones, not your working checkouts. Writes <state>/reports/survey-<UTC>.md.
"""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

from giml.core.config import default_gate_config_path, load_gate_config
from giml.gate.assess import assess
from giml.maven.jdk import catalog
from giml.store.sqlite_store import SqliteStateStore

COLUMNS = ("project", "jdk", "line %", "branch %", "strength %", "mutation %", "excluded %", "flaky",
           "untested modules", "enforcer", "tooling added", "earned tier")  # fmt: skip


def _cell(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def _jdk(jdk: dict) -> str:
    return f"{jdk['version'] or 'unknown'} ({jdk['source']})"


def row(result: dict) -> list[str]:
    measured = result["measured"]
    enforcer = result["enforcer"]["status"]
    if result["enforcer"].get("failed_rules"):
        enforcer += " (" + ", ".join(result["enforcer"]["failed_rules"]) + ")"
    return [_cell(v) for v in (
        result["project"], _jdk(result["tools"]["jdk"]), measured["unit_line_coverage"], measured["unit_branch_coverage"],
        measured["pit_test_strength"], measured["pit_mutation_coverage"], result["excluded_share"],
        len(result["flaky_tests"]), ", ".join(result["untested_modules"]) or "none", enforcer,
        len(result["tooling_added"]), result["passed_tier"] or "none",
    )]  # fmt: skip


def table(results: list[dict]) -> str:
    lines = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    lines += ["| " + " | ".join(row(r)) + " |" for r in results]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".giml")
    parser.add_argument("--config", type=Path, help="global config (default: ~/.giml/config.yml)")
    parser.add_argument("projects", nargs="+", type=Path)
    args = parser.parse_args(argv)
    config = load_gate_config(default_gate_config_path())
    jdks = catalog(args.config)
    now = datetime.datetime.now(datetime.UTC)
    results, failures = [], []
    with SqliteStateStore(args.state_dir / "state.db") as store:
        for project in args.projects:
            try:
                results.append(assess(project, args.state_dir, store, lambda: datetime.datetime.now(datetime.UTC),
                                      config, jdks=jdks).result)  # fmt: skip
            except Exception as exc:  # report every project, even if one fails
                failures.append(f"- {project}: {type(exc).__name__}: {exc}")
    report = [f"# giml survey {now:%Y-%m-%d %H:%M} UTC (gate config version {config.version})", "", table(results)]
    if failures:
        report += ["", "Not assessed:", *failures]
    out = args.state_dir / "reports" / f"survey-{now:%Y%m%dT%H%M%SZ}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report))
    print(f"\nwritten to {out}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
