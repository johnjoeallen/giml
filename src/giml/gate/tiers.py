"""Tier evaluation (spec section 6): which test-quality tier the measurements earn."""

from __future__ import annotations

from dataclasses import dataclass, field

from giml.core.config import GateConfig, Tier

THRESHOLD_METRICS = ("unit_line_coverage", "unit_branch_coverage", "pit_test_strength", "pit_mutation_coverage")


@dataclass(frozen=True)
class Metrics:
    unit_line_coverage: float | None
    unit_branch_coverage: float | None
    pit_test_strength: float | None
    pit_mutation_coverage: float | None
    excluded_share: float
    flaky_tests: tuple[str, ...] = ()
    untested_modules: tuple[str, ...] = ()
    unavailable: dict[str, str] = field(default_factory=dict)  # metric -> reason it could not be measured


def misses(metrics: Metrics, tier: Tier) -> list[dict]:
    """Every requirement of ``tier`` the metrics do not meet, with how far off each one is."""
    found: list[dict] = []
    for name in THRESHOLD_METRICS:
        measured, required = getattr(metrics, name), getattr(tier, name)
        if measured is None:
            found.append({"metric": name, "required": required, "measured": None,
                          "reason": metrics.unavailable.get(name, "not measured")})  # fmt: skip
        elif measured < required:
            found.append({"metric": name, "required": required, "measured": round(measured, 2),
                          "short_by": round(required - measured, 2)})  # fmt: skip
    if metrics.excluded_share > tier.max_excluded_share:
        found.append({"metric": "excluded_share", "maximum": tier.max_excluded_share,
                      "measured": round(metrics.excluded_share, 2),
                      "over_by": round(metrics.excluded_share - tier.max_excluded_share, 2)})  # fmt: skip
    if metrics.flaky_tests:
        found.append({"metric": "flaky_tests", "tests": list(metrics.flaky_tests)})
    if metrics.untested_modules:
        # PIT skips modules without tests, so their mutants would silently vanish (user decision).
        found.append({"metric": "untested_modules", "modules": list(metrics.untested_modules)})
    return found


def earned_tier(metrics: Metrics, config: GateConfig) -> str | None:
    """The first tier, in config order (strongest first), whose every requirement is met."""
    return next((name for name, tier in config.tiers.items() if not misses(metrics, tier)), None)
