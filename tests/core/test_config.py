import datetime
from pathlib import Path

import pytest
import yaml

from giml.core.config import (
    ConfigError, GlobalConfig, ProjectSettings, default_gate_config_path, load_gate_config, load_global_config,
    load_project_settings, parse_gate_config,
)  # fmt: skip

DEFAULT_CONFIG = default_gate_config_path()


def default_data() -> dict:
    return yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))


def parse(data: dict):
    return parse_gate_config(yaml.safe_dump(data))


def test_default_config_loads_as_specified():
    config = load_gate_config(DEFAULT_CONFIG)
    assert config.version == 3
    assert list(config.tiers) == ["A", "B"]
    b = config.tiers["B"]
    assert (b.unit_line_coverage, b.unit_branch_coverage) == (80, 70)
    assert (b.pit_test_strength, b.pit_mutation_coverage) == (85, 80)
    assert b.max_excluded_share == 15 and b.expires is None
    assert config.autonomy == {"A": "auto_apply", "B": "suggest_only"}
    assert (config.verification.integration_tests, config.verification.startup_check) == (
        "when_present", "when_configured",
    )  # fmt: skip
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
    text = DEFAULT_CONFIG.read_text(encoding="utf-8").replace("version: 3", "version: 3\nversion: 4")
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
        (("autonomy",), "B", "maybe", "one of auto_apply, suggest_only"),
        (("verification",), "integration_tests", "always", "one of when_present, off"),
        (("verification",), "startup_check", True, "one of when_configured, off"),
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
    data["autonomy"]["B-legacy"] = "suggest_only"
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


@pytest.mark.parametrize("key", ["require_integration_tests", "require_startup_check", "ml_action"])
def test_non_test_quality_settings_are_not_tier_keys(key):
    data = default_data()
    data["tiers"]["B"][key] = True
    with pytest.raises(ConfigError, match=rf"tiers\.B: unknown key\(s\): {key}"):
        parse(data)


def test_every_tier_needs_an_autonomy_action():
    data = default_data()
    data["tiers"]["C"] = dict(data["tiers"]["B"])
    with pytest.raises(ConfigError, match="autonomy: no action for tier\\(s\\): C"):
        parse(data)


def test_autonomy_for_an_undefined_tier_is_rejected():
    data = default_data()
    data["autonomy"]["Z"] = "suggest_only"
    with pytest.raises(ConfigError, match=r"autonomy: unknown key\(s\): Z"):
        parse(data)


def test_verification_stages_can_be_switched_off():
    data = default_data()
    data["verification"] = {"integration_tests": "off", "startup_check": "off"}
    config = parse(data)
    assert (config.verification.integration_tests, config.verification.startup_check) == ("off", "off")


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_global_config_missing_or_empty_means_no_jdks(tmp_path):
    assert load_global_config(tmp_path / "absent.yml") == GlobalConfig()
    assert load_global_config(write(tmp_path / "empty.yml", "")) == GlobalConfig()


def test_global_config_lists_jdk_homes_with_home_expansion(tmp_path):
    config = load_global_config(write(tmp_path / "c.yml", "jdks:\n  - /opt/jdk-17\n  - ~/jdks/21\n"))
    assert config.jdks == (Path("/opt/jdk-17"), Path.home() / "jdks" / "21")


@pytest.mark.parametrize(("text", "error"), [
    ("jdks: /opt/jdk\n", r"c\.yml\.jdks: expected a list of JDK home directories"),
    ("jdks:\n  - relative/jdk\n", r"c\.yml\.jdks\[0\]: expected an absolute path"),
    ("jdks:\n  - ''\n", r"c\.yml\.jdks\[0\]: expected a path, got ''"),
    ("jdks:\n  - 17\n", r"c\.yml\.jdks\[0\]: expected a path, got 17"),
    ("jdks: []\nprojects: {}\n", r"c\.yml: unknown key\(s\): projects"),
    ("{}\n", r"c\.yml\.jdks: required key missing"),
    ("jdks: []\njdks: []\n", r"c\.yml: duplicate key 'jdks'"),
    ("jdks: [\n", r"c\.yml: invalid YAML"),
])  # fmt: skip
def test_global_config_errors_name_the_file_and_key(tmp_path, text, error):
    with pytest.raises(ConfigError, match=error):
        load_global_config(write(tmp_path / "c.yml", text))


def test_unreadable_global_config_is_a_config_error(tmp_path):
    (tmp_path / "dir.yml").mkdir()
    with pytest.raises(ConfigError, match=r"dir\.yml: cannot read"):
        load_global_config(tmp_path / "dir.yml")


def test_project_settings_default_to_nothing(tmp_path):
    assert load_project_settings(tmp_path) == ProjectSettings(tmp_path / ".giml" / "settings.yml")
    write(tmp_path / ".giml" / "settings.yml", "")
    assert load_project_settings(tmp_path).jdk is None


@pytest.mark.parametrize(("text", "jdk"), [("jdk: 17\n", "17"), ("jdk: '17.0.16'\n", "17.0.16"), ("jdk: '21'\n", "21")])
def test_project_settings_jdk_version(tmp_path, text, jdk):
    write(tmp_path / ".giml" / "settings.yml", text)
    settings = load_project_settings(tmp_path)
    assert settings == ProjectSettings(tmp_path / ".giml" / "settings.yml", jdk, None)


def test_project_settings_java_home(tmp_path):
    write(tmp_path / ".giml" / "settings.yml", "java_home: ~/jdk\n")
    settings = load_project_settings(tmp_path)
    assert (settings.jdk, settings.java_home) == (None, Path.home() / "jdk")


@pytest.mark.parametrize(("text", "error"), [
    ("jdk: 17.0\n", r"settings\.yml\.jdk: expected a version such as 17 .* got 17\.0"),
    ("jdk: temurin-17\n", r"settings\.yml\.jdk: expected a version"),
    ("jdk: true\n", r"settings\.yml\.jdk: expected a version"),
    ("jdk: [17]\n", r"settings\.yml\.jdk: expected a version"),
    ("java_home: jdk\n", r"settings\.yml\.java_home: expected an absolute path"),
    ("jdk: 17\njava_home: /opt/jdk\n", r"settings\.yml: set jdk or java_home, not both"),
    ("jdks: 17\n", r"settings\.yml: unknown key\(s\): jdks"),
    ("- 17\n", r"settings\.yml: expected a mapping"),
])  # fmt: skip
def test_project_settings_errors(tmp_path, text, error):
    write(tmp_path / ".giml" / "settings.yml", text)
    with pytest.raises(ConfigError, match=error):
        load_project_settings(tmp_path)


def test_project_settings_allow_exclusions(tmp_path):
    assert load_project_settings(tmp_path).allow_exclusions is False
    write(tmp_path / ".giml" / "settings.yml", "jdk: 21\nallow_exclusions: true\n")
    settings = load_project_settings(tmp_path)
    assert (settings.jdk, settings.allow_exclusions) == ("21", True)
    write(tmp_path / ".giml" / "settings.yml", "allow_exclusions: false\n")
    assert load_project_settings(tmp_path).allow_exclusions is False


@pytest.mark.parametrize("value", ["yes please", "1", "[]"])
def test_project_settings_allow_exclusions_must_be_a_boolean(tmp_path, value):
    write(tmp_path / ".giml" / "settings.yml", f"allow_exclusions: {value}\n")
    with pytest.raises(ConfigError, match=r"settings\.yml\.allow_exclusions: expected true or false"):
        load_project_settings(tmp_path)
