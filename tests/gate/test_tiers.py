import pytest

from giml.core.config import default_gate_config_path, load_gate_config
from giml.gate.tiers import Metrics, earned_tier, misses

CONFIG = load_gate_config(default_gate_config_path())


def metrics(line=95.0, branch=85.0, strength=95.0, coverage=92.0, excluded=5.0, **extra):
    return Metrics(line, branch, strength, coverage, excluded, **extra)


def test_strong_project_earns_the_first_tier():
    assert earned_tier(metrics(), CONFIG) == "A"
    assert misses(metrics(), CONFIG.tiers["A"]) == []


def test_tiers_are_tried_strongest_first():
    assert earned_tier(metrics(line=85.0), CONFIG) == "B"
    assert earned_tier(metrics(line=10.0), CONFIG) is None


def test_boundaries_are_inclusive():
    b = CONFIG.tiers["B"]
    at_minimum = metrics(b.unit_line_coverage, b.unit_branch_coverage, b.pit_test_strength, b.pit_mutation_coverage,
                         b.max_excluded_share)  # fmt: skip
    assert misses(at_minimum, b) == []


def test_misses_report_how_far_off_each_metric_is():
    found = misses(metrics(line=75.5, excluded=16.25), CONFIG.tiers["B"])
    assert found == [
        {"metric": "unit_line_coverage", "required": 80.0, "measured": 75.5, "short_by": 4.5},
        {"metric": "excluded_share", "maximum": 15.0, "measured": 16.25, "over_by": 1.25},
    ]


def test_unmeasured_metric_is_a_miss_with_its_reason():
    found = misses(metrics(strength=None, unavailable={"pit_test_strength": "PIT failed"}), CONFIG.tiers["B"])
    assert found == [{"metric": "pit_test_strength", "required": 85.0, "measured": None, "reason": "PIT failed"}]
    assert misses(metrics(coverage=None), CONFIG.tiers["B"])[0]["reason"] == "not measured"


@pytest.mark.parametrize(
    ("extra", "entry"),
    [({"flaky_tests": ("T#a",)}, {"metric": "flaky_tests", "tests": ["T#a"]}),
     ({"untested_modules": ("app",)}, {"metric": "untested_modules", "modules": ["app"]})],
)  # fmt: skip
def test_flaky_tests_and_untested_modules_block_every_tier(extra, entry):
    assert misses(metrics(**extra), CONFIG.tiers["B"]) == [entry]
    assert earned_tier(metrics(**extra), CONFIG) is None
