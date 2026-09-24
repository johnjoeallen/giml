"""Collectors against real reports produced by running the mini-reactor fixture (tests/fixtures/maven)."""

from collections import Counter
from pathlib import Path

import pytest

from giml.gate.reports import (
    Mutations,
    ReportError,
    flaky_tests,
    read_jacoco,
    read_pit,
    read_surefire,
)

REPORTS = Path(__file__).resolve().parents[1] / "fixtures" / "reports"


def test_module_jacoco_report():
    coverage = read_jacoco(REPORTS / "jacoco-core.xml")
    assert (coverage.line_covered, coverage.line_missed) == (4, 2)
    assert coverage.classes == {"org/giml/fixture/core/Calculator"}


def test_aggregate_report_includes_the_untested_module():
    coverage = read_jacoco(REPORTS / "jacoco-aggregate.xml")
    assert (coverage.line_covered, coverage.line_missed, coverage.branch_covered, coverage.branch_missed) == (4, 4, 1, 5)
    assert coverage.line_percent == 50.0
    assert coverage.branch_percent == pytest.approx(100 / 6)
    assert coverage.classes == {"org/giml/fixture/core/Calculator", "org/giml/fixture/app/Greeter"}


def test_empty_counters_give_no_percentage(tmp_path):
    report = tmp_path / "r.xml"
    report.write_text('<report name="x"/>')
    coverage = read_jacoco(report)
    assert coverage.line_percent is None and coverage.branch_percent is None and coverage.classes == frozenset()


def test_pit_report():
    mutations = read_pit(REPORTS / "mutations-core.xml")
    assert mutations.statuses == Counter({"KILLED": 5, "NO_COVERAGE": 3})
    assert (mutations.total, mutations.detected, mutations.no_coverage) == (8, 5, 3)
    assert mutations.test_strength == 100.0
    assert mutations.mutation_coverage == 62.5


def test_pit_status_rules_and_aggregation():
    a = Mutations(Counter({"KILLED": 2, "TIMED_OUT": 1, "MEMORY_ERROR": 1, "RUN_ERROR": 1, "SURVIVED": 3}))
    b = Mutations(Counter({"NO_COVERAGE": 2, "NON_VIABLE": 4}))
    combined = a + b
    assert (combined.total, combined.detected, combined.no_coverage) == (10, 5, 2)
    assert combined.test_strength == 62.5 and combined.mutation_coverage == 50.0
    assert Mutations().test_strength is None and Mutations().mutation_coverage is None


def test_surefire_outcomes(tmp_path):
    assert read_surefire(REPORTS) == {
        "org.giml.fixture.core.CalculatorTest#adds": "passed",
        "org.giml.fixture.core.CalculatorTest#divides": "passed",
    }
    (tmp_path / "TEST-x.xml").write_text(
        '<testsuite><testcase classname="C" name="f"><failure/></testcase><testcase classname="C" name="e"><error/>'
        '</testcase><testcase classname="C" name="s"><skipped/></testcase></testsuite>'
    )
    assert read_surefire(tmp_path) == {"C#f": "failed", "C#e": "error", "C#s": "skipped"}


def test_flaky_tests_are_those_with_differing_outcomes():
    runs = [{"a": "passed", "b": "passed", "c": "failed"}, {"a": "passed", "b": "failed", "c": "failed"}, {"a": "passed"}]
    assert flaky_tests(runs) == ["b", "c"]
    assert flaky_tests([{"a": "passed"}] * 3) == []
    assert flaky_tests([]) == []


@pytest.mark.parametrize(("reader", "content", "message"), [
    (read_jacoco, "<mutations/>", "not a JaCoCo report"),
    (read_pit, "<report/>", "not a PIT mutations report"),
    (read_jacoco, "<report", "cannot read report"),
])  # fmt: skip
def test_wrong_or_broken_reports(tmp_path, reader, content, message):
    path = tmp_path / "r.xml"
    path.write_text(content)
    with pytest.raises(ReportError, match=message):
        reader(path)


def test_missing_report(tmp_path):
    with pytest.raises(ReportError, match="cannot read report"):
        read_pit(tmp_path / "absent.xml")
