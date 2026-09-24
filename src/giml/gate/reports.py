"""Parsers for the reports assessment reads: JaCoCo XML, PIT mutations.xml, Surefire TEST-*.xml.

Everything here is pure parsing of files the tools wrote; nothing is executed.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# PIT counts these statuses as detected (a test noticed the mutant); NON_VIABLE mutants never ran.
PIT_DETECTED = frozenset({"KILLED", "TIMED_OUT", "MEMORY_ERROR", "RUN_ERROR"})
PIT_NOT_RUN = frozenset({"NON_VIABLE"})


class ReportError(ValueError):
    """A report file is missing or malformed."""


def _parse(path: Path) -> ET.Element:
    try:
        return ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ReportError(f"{path}: cannot read report: {exc}") from exc


def _percent(part: int, whole: int) -> float | None:
    return 100.0 * part / whole if whole else None


# ---------------------------------------------------------------------------------------------
# JaCoCo


@dataclass(frozen=True)
class Coverage:
    line_covered: int
    line_missed: int
    branch_covered: int
    branch_missed: int
    classes: frozenset[str]  # JVM names, e.g. org/example/Foo

    @property
    def line_percent(self) -> float | None:
        return _percent(self.line_covered, self.line_covered + self.line_missed)

    @property
    def branch_percent(self) -> float | None:
        return _percent(self.branch_covered, self.branch_covered + self.branch_missed)


def read_jacoco(path: Path) -> Coverage:
    """Report-level LINE and BRANCH counters and the classes a JaCoCo XML report covers."""
    root = _parse(path)
    if root.tag != "report":
        raise ReportError(f"{path}: not a JaCoCo report")
    counters = {c.get("type"): c for c in root.findall("counter")}

    def count(kind: str, attribute: str) -> int:
        counter = counters.get(kind)
        return int(counter.get(attribute)) if counter is not None else 0

    classes = frozenset(c.get("name") for c in root.iter("class"))
    return Coverage(count("LINE", "covered"), count("LINE", "missed"),
                    count("BRANCH", "covered"), count("BRANCH", "missed"), classes)  # fmt: skip


# ---------------------------------------------------------------------------------------------
# PIT


@dataclass(frozen=True)
class Mutations:
    statuses: Counter = field(default_factory=Counter)

    def __add__(self, other: Mutations) -> Mutations:
        return Mutations(self.statuses + other.statuses)

    @property
    def total(self) -> int:
        return sum(n for status, n in self.statuses.items() if status not in PIT_NOT_RUN)

    @property
    def detected(self) -> int:
        return sum(self.statuses[s] for s in PIT_DETECTED)

    @property
    def no_coverage(self) -> int:
        return self.statuses["NO_COVERAGE"]

    @property
    def test_strength(self) -> float | None:
        """Detected mutants among those some test covered (PIT's definition)."""
        return _percent(self.detected, self.total - self.no_coverage)

    @property
    def mutation_coverage(self) -> float | None:
        """Detected mutants among all mutants."""
        return _percent(self.detected, self.total)


def read_pit(path: Path) -> Mutations:
    root = _parse(path)
    if root.tag != "mutations":
        raise ReportError(f"{path}: not a PIT mutations report")
    return Mutations(Counter(m.get("status", "UNKNOWN") for m in root.findall("mutation")))


# ---------------------------------------------------------------------------------------------
# Surefire


def read_surefire(report_dir: Path) -> dict[str, str]:
    """Outcome per test ("passed", "failed", "error" or "skipped") from one run's TEST-*.xml."""
    outcomes: dict[str, str] = {}
    for path in sorted(report_dir.glob("TEST-*.xml")):
        for case in _parse(path).iter("testcase"):
            test_id = f"{case.get('classname')}#{case.get('name')}"
            if case.find("failure") is not None:
                outcomes[test_id] = "failed"
            elif case.find("error") is not None:
                outcomes[test_id] = "error"
            elif case.find("skipped") is not None:
                outcomes[test_id] = "skipped"
            else:
                outcomes[test_id] = "passed"
    return outcomes


def flaky_tests(runs: list[dict[str, str]]) -> list[str]:
    """Tests whose outcome differs between runs (including tests missing from some runs)."""
    all_tests = set().union(*runs) if runs else set()
    return sorted(t for t in all_tests if len({run.get(t, "missing") for run in runs}) > 1)
