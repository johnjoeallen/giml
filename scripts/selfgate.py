"""giml's own quality gate: checks giml itself against a tier from config/gate-config.yaml.

Dogfooding requirement (spec section 0): giml meets Tier B from M1 and Tier A by the end of M6.
coverage.py and mutmut stand in for JaCoCo and PIT. The mutmut counts map to PIT's measures as:

    detected           = killed + timeout            (PIT counts TIMED_OUT as detected)
    test strength      = detected / (total - no_tests - skipped)
    mutation coverage  = detected / (total - skipped)

``suspicious`` and ``segfault`` mutants are treated as not detected (conservative).
Excluded share is the percentage of statements excluded from coverage (``pragma: no cover``) or
from mutation (``pragma: no mutate`` lines and files matched by ``[tool.mutmut] do_not_mutate``),
mirroring spec 6.3 step 5.

Usage: python scripts/selfgate.py [--tier B] [--flake-runs N] [--skip-mutation]
Needs the dev extra installed; run from the repository root. Exit 0 only if every metric passes.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from giml.core.config import Tier, load_gate_config  # noqa: E402 - needs the sys.path entry above


@dataclass(frozen=True)
class Metric:
    name: str
    value: float
    threshold: float
    higher_is_better: bool = True

    @property
    def passed(self) -> bool:
        return self.value >= self.threshold if self.higher_is_better else self.value <= self.threshold

    def line(self) -> str:
        relation = ">=" if self.higher_is_better else "<="
        verdict = "PASS" if self.passed else "FAIL"
        return f"{verdict}  {self.name:<24} {self.value:6.2f}  (required {relation} {self.threshold:g})"


def _run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("check", False)
    return subprocess.run(command, cwd=ROOT, **kwargs)


def _bin(name: str) -> str:
    candidate = Path(sys.executable).parent / name
    return str(candidate) if candidate.exists() else name


def coverage_metrics(tier: Tier) -> list[Metric]:
    for stale in ROOT.glob(".coverage*"):
        stale.unlink()
    result = _run([_bin("coverage"), "run", "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)  # fmt: skip
    if result.returncode != 0:
        raise SystemExit(f"selfgate: test suite failed under coverage; fix tests first\n{result.stdout}")
    report = ROOT / "coverage.json"
    _run([_bin("coverage"), "json", "-q", "-o", str(report)], check=True)
    data = json.loads(report.read_text(encoding="utf-8"))
    totals = data["totals"]
    report.unlink()
    patterns = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["mutmut"].get(
        "do_not_mutate", []
    )
    unmutated = sum(
        info["summary"]["num_statements"] for name, info in data["files"].items()
        if any(fnmatch.fnmatch(name, pattern) for pattern in patterns)
    )
    line = 100.0 * totals["covered_lines"] / totals["num_statements"]
    branch = 100.0 * totals["covered_branches"] / totals["num_branches"] if totals["num_branches"] else 100.0
    no_mutate = sum(
        line.count("pragma: no mutate") for path in (ROOT / "src").rglob("*.py")
        for line in path.read_text(encoding="utf-8").splitlines()
    )
    excluded = totals["excluded_lines"] + no_mutate + unmutated
    share = 100.0 * excluded / (totals["num_statements"] + totals["excluded_lines"])
    return [
        Metric("unit_line_coverage", line, tier.unit_line_coverage),
        Metric("unit_branch_coverage", branch, tier.unit_branch_coverage),
        Metric("excluded_share", share, tier.max_excluded_share, higher_is_better=False),
    ]


def mutation_metrics(tier: Tier, max_children: int) -> list[Metric]:
    shutil.rmtree(ROOT / "mutants", ignore_errors=True)
    result = _run([_bin("mutmut"), "run", "--max-children", str(max_children)],
                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)  # fmt: skip
    if result.returncode != 0:
        raise SystemExit(f"selfgate: mutmut run failed:\n{result.stderr}")
    _run([_bin("mutmut"), "export-cicd-stats"], check=True, stdout=subprocess.DEVNULL)
    stats = json.loads((ROOT / "mutants" / "mutmut-cicd-stats.json").read_text(encoding="utf-8"))
    return mutation_metrics_from_stats(stats, tier)


def mutation_metrics_from_stats(stats: dict, tier: Tier) -> list[Metric]:
    detected = stats["killed"] + stats["timeout"]
    considered = stats["total"] - stats["skipped"]
    covered = considered - stats["no_tests"]
    strength = 100.0 * detected / covered if covered else 0.0
    coverage = 100.0 * detected / considered if considered else 0.0
    return [
        Metric("pit_test_strength", strength, tier.pit_test_strength),
        Metric("pit_mutation_coverage", coverage, tier.pit_mutation_coverage),
    ]


def flake_check(runs: int) -> Metric:
    outcomes = set()
    for _ in range(runs):
        result = _run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)  # fmt: skip
        outcomes.add((result.returncode, result.stdout.strip().splitlines()[-1].split(" in ")[0]))
    return Metric("flaky_runs", float(len(outcomes) - 1), 0.0, higher_is_better=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tier", default="B")
    parser.add_argument("--flake-runs", type=int, default=0, help="rerun the suite N times; 0 skips")
    parser.add_argument("--skip-mutation", action="store_true", help="coverage only (fast feedback)")
    parser.add_argument("--max-children", type=int, default=os.cpu_count() or 4)
    args = parser.parse_args(argv)

    config = load_gate_config(ROOT / "config" / "gate-config.yaml")
    if args.tier not in config.tiers:
        parser.error(f"unknown tier {args.tier!r}; config has {', '.join(config.tiers)}")
    tier = config.tiers[args.tier]

    metrics = coverage_metrics(tier)
    if not args.skip_mutation:
        metrics += mutation_metrics(tier, args.max_children)
    if args.flake_runs:
        metrics.append(flake_check(args.flake_runs))

    print(f"giml self-gate: tier {tier.name} (gate-config version {config.version})")
    for metric in metrics:
        print(metric.line())
    if args.skip_mutation:
        print("NOTE  mutation metrics skipped; the gate is not fully evaluated")
    failed = [m.name for m in metrics if not m.passed]
    print(f"result: {'FAIL: ' + ', '.join(failed) if failed else 'PASS'}")
    return 1 if failed or args.skip_mutation else 0


if __name__ == "__main__":
    sys.exit(main())
