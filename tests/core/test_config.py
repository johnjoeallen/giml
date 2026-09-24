import datetime
from pathlib import Path

import pytest
import yaml

from giml.core.config import ConfigError, load_gate_config, parse_gate_config

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "gate-config.yaml"


def default_data() -> dict:
    return yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))


def parse(data: dict):
    return parse_gate_config(yaml.safe_dump(data))


def test_default_config_loads_as_specified():
    config = load_gate_config(DEFAULT_CONFIG)
    assert config.version == 2
    assert list(config.tiers) == ["A", "B"]
    b = config.tiers["B"]
    assert (b.unit_line_coverage, b.unit_branch_coverage) == (80, 70)
    assert (b.pit_test_strength, b.pit_mutation_coverage) == (85, 80)
    assert b.ml_action == "suggest_only" and b.expires is None
    assert config.tiers["A"].require_startup_check is True
    assert config.shared.flake_check_runs == 5
    assert config.planning.objective_profile == "cve_first"
    assert config.planning.release_cooldown_days == 7
    assert config.warnings == ()


def test_unknown_key_is_rejected_with_its_path():
    data = default_data()
    data["tiers"]["B"]["unit_line_coverag"] = 80
    with pytest.raises(ConfigError, match=r"tiers\.B: unknown key\(s\): unit_line_coverag"):
        parse(data)


def test_unknown_top_level_key_is_rejected():
    data = default_data()
    data["extra"] = 1
    with pytest.raises(ConfigError, match=r"<root>: unknown key\(s\): extra"):
        parse(data)


def test_duplicate_key_is_rejected():
    text = DEFAULT_CONFIG.read_text(encoding="utf-8").replace("version: 2", "version: 2\nversion: 3")
    with pytest.raises(ConfigError, match="duplicate key 'version' at line 2"):
        parse_gate_config(text)


def test_missing_key_is_rejected():
    data = default_data()
    del data["planning"]["max_builds"]
    with pytest.raises(ConfigError, match=r"planning\.max_builds: required key missing"):
        parse(data)


@pytest.mark.parametrize(
    ("section", "key", "value", "message"),
    [
        (("tiers", "B"), "unit_line_coverage", 101, "percentage 0-100"),
        (("tiers", "B"), "unit_line_coverage", True, "percentage 0-100"),
        (("tiers", "B"), "unit_line_coverage", "80", "percentage 0-100"),
        (("tiers", "B"), "require_startup_check", "no", "true or false"),
        (("tiers", "B"), "ml_action", "maybe", "one of auto_apply, suggest_only, none"),
        (("shared",), "flake_check_runs", 0, "integer >= 1"),
        (("shared",), "touchpoint_thresholds", "custom", "one of same_as_tier"),
        (("planning",), "release_cooldown_days", -1, "integer >= 0"),
        (("planning",), "max_wall_minutes", 1.5, "integer >= 1"),
        (("planning",), "objective_profile", " ", "non-empty string"),
    ],
)
def test_invalid_values_are_rejected(section, key, value, message):
    data = default_data()
    target = data
    for part in section:
        target = target[part]
    target[key] = value
    with pytest.raises(ConfigError, match=message):
        parse(data)


def test_version_must_be_positive_integer():
    data = default_data()
    data["version"] = 0
    with pytest.raises(ConfigError, match=r"<root>\.version: expected an integer >= 1"):
        parse(data)


@pytest.mark.parametrize("tiers", [{}, [], None])
def test_tiers_must_be_non_empty_mapping(tiers):
    data = default_data()
    data["tiers"] = tiers
    with pytest.raises(ConfigError, match="tiers: expected a non-empty mapping"):
        parse(data)


def test_section_must_be_mapping():
    data = default_data()
    data["shared"] = [1]
    with pytest.raises(ConfigError, match="shared: expected a mapping"):
        parse(data)


def test_root_must_be_mapping():
    with pytest.raises(ConfigError, match="<root>: expected a mapping"):
        parse_gate_config("- 1\n")


def test_invalid_yaml_is_a_config_error():
    with pytest.raises(ConfigError, match="invalid YAML"):
        parse_gate_config("version: [1\n")


def test_named_tier_with_expiry_is_allowed():
    data = default_data()
    data["tiers"]["B-legacy"] = dict(data["tiers"]["B"], expires=datetime.date(2027, 1, 31))
    config = parse(data)
    assert config.tiers["B-legacy"].expires == datetime.date(2027, 1, 31)


@pytest.mark.parametrize("value", ["2027-01-31x", datetime.datetime(2027, 1, 31, 12, 0)])
def test_expiry_must_be_a_date(value):
    data = default_data()
    data["tiers"]["B"]["expires"] = value
    with pytest.raises(ConfigError, match=r"tiers\.B\.expires: expected a date"):
        parse(data)


@pytest.mark.parametrize("strength", [80, 79.5])
def test_strength_not_above_coverage_warns(strength):
    data = default_data()
    data["tiers"]["B"]["pit_test_strength"] = strength
    config = parse(data)
    assert len(config.warnings) == 1
    assert config.warnings[0].startswith("tiers.B: pit_test_strength")


def test_load_names_the_file_on_error(tmp_path):
    path = tmp_path / "gate.yaml"
    path.write_text("version: 2\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=rf"^{path}: <root>\.tiers: required key missing"):
        load_gate_config(path)


def test_load_reports_unreadable_file(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_gate_config(tmp_path / "missing.yaml")


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        (("planning",), "release_cooldown_days", 0),
        (("planning",), "max_builds", 1),
        (("planning",), "max_wall_minutes", 1),
        (("shared",), "flake_check_runs", 1),
        (("shared",), "max_result_age_days", 1),
        (("tiers", "B"), "unit_line_coverage", 0),
        (("tiers", "B"), "unit_line_coverage", 100),
        (("tiers", "B"), "max_excluded_share", 99.5),
    ],
)
def test_boundary_values_are_accepted(section, key, value):
    data = default_data()
    target = data
    for part in section:
        target = target[part]
    target[key] = value
    config = parse(data)
    holder = config.tiers["B"] if section[0] == "tiers" else getattr(config, section[0])
    assert getattr(holder, key) == value


def test_all_unknown_keys_are_listed_sorted():
    data = default_data()
    data["planning"]["zeta"] = 1
    data["planning"]["alpha"] = 2
    with pytest.raises(ConfigError) as exc:
        parse(data)
    assert str(exc.value) == "planning: unknown key(s): alpha, zeta"


def test_empty_tiers_message_is_exact():
    data = default_data()
    data["tiers"] = {}
    with pytest.raises(ConfigError) as exc:
        parse(data)
    assert str(exc.value) == "tiers: expected a non-empty mapping of tier name to thresholds"
