"""Loader and validator for ``gate-config.yaml`` (spec section 6.1).

Strict by design: unknown or duplicate keys are errors, so a typo cannot silently weaken a gate.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

AUTONOMY_ACTIONS = ("auto_apply", "suggest_only")
TOUCHPOINT_THRESHOLDS = ("same_as_tier",)
INTEGRATION_TEST_MODES = ("when_present", "off")
STARTUP_CHECK_MODES = ("when_configured", "off")


class ConfigError(ValueError):
    """Invalid configuration. The message names the file and the offending key path."""


@dataclass(frozen=True)
class Tier:
    """A test-quality level (spec 6): thresholds only, never upgrade criteria or stages."""

    name: str
    unit_line_coverage: float
    unit_branch_coverage: float
    pit_test_strength: float
    pit_mutation_coverage: float
    max_excluded_share: float
    expires: datetime.date | None = None


@dataclass(frozen=True)
class VerificationSettings:
    """Verification stages that run at every tier."""

    integration_tests: str
    startup_check: str


@dataclass(frozen=True)
class SharedSettings:
    touchpoint_thresholds: str
    flake_check_runs: int
    max_result_age_days: int


@dataclass(frozen=True)
class PlanningSettings:
    release_cooldown_days: int
    max_builds: int
    max_wall_minutes: int
    objective_profile: str


@dataclass(frozen=True)
class GateConfig:
    version: int
    tiers: dict[str, Tier]
    autonomy: dict[str, str]  # earned tier name -> action; no tier means propose nothing
    verification: VerificationSettings
    shared: SharedSettings
    planning: PlanningSettings
    warnings: tuple[str, ...] = ()


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of keeping the last one."""


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConfigError(f"duplicate key {key!r} at line {key_node.start_mark.line + 1}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


class _Section:
    """Reads typed values from one mapping, tracking which keys were consumed."""

    def __init__(self, data: Any, path: str) -> None:
        if not isinstance(data, dict):
            raise ConfigError(f"{path}: expected a mapping")
        self.data = data
        self.path = path
        self.used: set[str] = set()

    def _get(self, key: str, required: bool = True) -> Any:
        self.used.add(key)
        if key not in self.data:
            if required:
                raise ConfigError(f"{self.path}.{key}: required key missing")
            return None
        return self.data[key]

    def int(self, key: str, minimum: int) -> int:
        value = self._get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ConfigError(f"{self.path}.{key}: expected an integer >= {minimum}, got {value!r}")
        return value

    def percent(self, key: str) -> float:
        value = self._get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
            raise ConfigError(f"{self.path}.{key}: expected a percentage 0-100, got {value!r}")
        return float(value)

    def choice(self, key: str, allowed: tuple[str, ...]) -> str:
        value = self._get(key)
        if value not in allowed:
            raise ConfigError(f"{self.path}.{key}: expected one of {', '.join(allowed)}, got {value!r}")
        return value

    def text(self, key: str) -> str:
        value = self._get(key)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{self.path}.{key}: expected a non-empty string, got {value!r}")
        return value

    def optional_date(self, key: str) -> datetime.date | None:
        value = self._get(key, required=False)
        if value is None:
            return None
        if isinstance(value, datetime.datetime) or not isinstance(value, datetime.date):
            raise ConfigError(f"{self.path}.{key}: expected a date (YYYY-MM-DD), got {value!r}")
        return value

    def mapping(self, key: str) -> Any:
        return self._get(key)

    def finish(self) -> None:
        unknown = sorted(str(k) for k in self.data if k not in self.used)
        if unknown:
            raise ConfigError(f"{self.path}: unknown key(s): {', '.join(unknown)}")


def _parse_tier(name: str, data: Any) -> Tier:
    section = _Section(data, f"tiers.{name}")
    tier = Tier(
        name=name,
        unit_line_coverage=section.percent("unit_line_coverage"),
        unit_branch_coverage=section.percent("unit_branch_coverage"),
        pit_test_strength=section.percent("pit_test_strength"),
        pit_mutation_coverage=section.percent("pit_mutation_coverage"),
        max_excluded_share=section.percent("max_excluded_share"),
        expires=section.optional_date("expires"),
    )
    section.finish()
    return tier


def parse_gate_config(text: str) -> GateConfig:
    """Parse and validate gate-config YAML text. Raises ConfigError."""
    try:
        data = yaml.load(text, Loader=_StrictLoader)  # _StrictLoader extends SafeLoader
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML: {exc}") from exc
    root = _Section(data, "<root>")
    version = root.int("version", minimum=1)

    tiers_data = root.mapping("tiers")
    if not isinstance(tiers_data, dict) or not tiers_data:
        raise ConfigError("tiers: expected a non-empty mapping of tier name to thresholds")
    tiers = {str(name): _parse_tier(str(name), value) for name, value in tiers_data.items()}

    autonomy_section = _Section(root.mapping("autonomy"), "autonomy")
    missing = [name for name in tiers if name not in autonomy_section.data]
    if missing:
        raise ConfigError(f"autonomy: no action for tier(s): {', '.join(missing)}")
    autonomy = {name: autonomy_section.choice(name, AUTONOMY_ACTIONS) for name in tiers}
    autonomy_section.finish()

    verification_section = _Section(root.mapping("verification"), "verification")
    verification = VerificationSettings(
        integration_tests=verification_section.choice("integration_tests", INTEGRATION_TEST_MODES),
        startup_check=verification_section.choice("startup_check", STARTUP_CHECK_MODES),
    )
    verification_section.finish()

    shared_section = _Section(root.mapping("shared"), "shared")
    shared = SharedSettings(
        touchpoint_thresholds=shared_section.choice("touchpoint_thresholds", TOUCHPOINT_THRESHOLDS),
        flake_check_runs=shared_section.int("flake_check_runs", minimum=1),
        max_result_age_days=shared_section.int("max_result_age_days", minimum=1),
    )
    shared_section.finish()

    planning_section = _Section(root.mapping("planning"), "planning")
    planning = PlanningSettings(
        release_cooldown_days=planning_section.int("release_cooldown_days", minimum=0),
        max_builds=planning_section.int("max_builds", minimum=1),
        max_wall_minutes=planning_section.int("max_wall_minutes", minimum=1),
        objective_profile=planning_section.text("objective_profile"),
    )
    planning_section.finish()
    root.finish()

    warnings = tuple(
        f"tiers.{t.name}: pit_test_strength ({t.pit_test_strength:g}) should be above "
        f"pit_mutation_coverage ({t.pit_mutation_coverage:g}); strength is always >= coverage"
        for t in tiers.values()
        if t.pit_test_strength <= t.pit_mutation_coverage
    )
    return GateConfig(version, tiers, autonomy, verification, shared, planning, warnings)


def load_gate_config(path: Path) -> GateConfig:
    """Load and validate a gate-config file. Raises ConfigError naming the file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc.strerror}") from exc
    try:
        return parse_gate_config(text)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
