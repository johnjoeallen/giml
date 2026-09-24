"""The self-gate's mapping from mutmut counts to PIT-style metrics (scripts/selfgate.py)."""

import importlib.util
import sys
from pathlib import Path

import pytest

from giml.core.config import load_gate_config

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("selfgate", ROOT / "scripts" / "selfgate.py")
selfgate = importlib.util.module_from_spec(_spec)
sys.modules["selfgate"] = selfgate  # dataclasses resolves the defining module through sys.modules
_spec.loader.exec_module(selfgate)

TIER_B = load_gate_config(ROOT / "config" / "gate-config.yaml").tiers["B"]


def stats(**counts):
    base = dict(killed=0, survived=0, total=0, no_tests=0, skipped=0, suspicious=0, timeout=0, segfault=0)
    return base | counts


def test_timeouts_count_as_detected_and_no_tests_lower_only_mutation_coverage():
    strength, coverage = selfgate.mutation_metrics_from_stats(
        stats(killed=80, timeout=5, survived=10, no_tests=5, total=100), TIER_B
    )
    assert strength.value == pytest.approx(100 * 85 / 95)
    assert coverage.value == pytest.approx(85.0)


def test_skipped_mutants_are_left_out_and_suspicious_count_against():
    strength, coverage = selfgate.mutation_metrics_from_stats(
        stats(killed=90, suspicious=10, skipped=20, total=120), TIER_B
    )
    assert strength.value == pytest.approx(90.0)
    assert coverage.value == pytest.approx(90.0)


def test_no_mutants_fails_rather_than_dividing_by_zero():
    strength, coverage = selfgate.mutation_metrics_from_stats(stats(), TIER_B)
    assert (strength.value, coverage.value) == (0.0, 0.0)
    assert not strength.passed and not coverage.passed


def test_metric_verdict_and_line():
    metric = selfgate.Metric("excluded_share", 16.0, 15.0, higher_is_better=False)
    assert not metric.passed
    assert metric.line() == "FAIL  excluded_share            16.00  (required <= 15)"
