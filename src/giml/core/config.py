"""Loaders and validators for ``gate-config.yaml`` (spec section 6.1), the developer's global
``~/.giml/config.yml`` and a project's ``.giml/settings.yml``.

Strict by design: unknown or duplicate keys are errors, so a typo cannot silently weaken a gate.
"""

from __future__ import annotations

import dataclasses
import datetime
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

AUTONOMY_ACTIONS = ("auto_apply", "suggest_only")
TOUCHPOINT_THRESHOLDS = ("same_as_tier",)
INTEGRATION_TEST_MODES = ("when_present", "off")
STARTUP_CHECK_MODES = ("when_configured", "off")
PIT_MODES = ("always", "off")
PLANNING_STRATEGIES = ("conservative", "latest")  # how far and how fast versions move (spec 8.2, 8.3)
PLANNING_SCOPES = ("cve", "general")  # which dependencies may move: CVE-affected only, or all (CVE first)
MAJOR_UPDATE_MODES = ("disallowed", "allowed", "ml")  # spec 8.7; "ml" needs ML evidence, so it is "disallowed" until M8
TEST_SCOPE_MODES = ("disallowed", "allowed")  # whether a major change in a test-only dependency counts (spec 8.7)


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
    pit: str  # always: a candidate's mutation scores must hold up; off: PIT is only measured by `assess`


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
    strategy: str
    scope: str
    major_updates: str
    max_snapshot_age_days: int  # planning warns when a data snapshot is older than this (spec 7.3)
    major_updates_test_scope: str  # "allowed": major changes in dependencies that only have test scope pass the gate


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
        if value is False and "off" in allowed:  # YAML reads an unquoted `off` as a boolean
            value = "off"
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

    def optional(self, key: str) -> Any:
        return self._get(key, required=False)

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
        pit=verification_section.choice("pit", PIT_MODES),
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
        strategy=planning_section.choice("strategy", PLANNING_STRATEGIES),
        scope=planning_section.choice("scope", PLANNING_SCOPES),
        major_updates=planning_section.choice("major_updates", MAJOR_UPDATE_MODES),
        max_snapshot_age_days=planning_section.int("max_snapshot_age_days", minimum=1),
        major_updates_test_scope=planning_section.choice("major_updates_test_scope", TEST_SCOPE_MODES),
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


def default_gate_config_path() -> Path:
    """The gate config shipped with giml (spec 6.1)."""
    return Path(str(resources.files("giml.gate") / "gate-config.yaml"))


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


def _load_yaml(path: Path) -> Any:
    """The parsed YAML of an optional file, or None when the file does not exist."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc.strerror}") from exc
    try:
        return yaml.load(text, Loader=_StrictLoader)  # _StrictLoader extends SafeLoader
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def _absolute_path(value: Any, where: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: expected a path, got {value!r}")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ConfigError(f"{where}: expected an absolute path (or one starting with ~), got {value!r}")
    return path


JDK_VERSION = re.compile(r"\d+(\.\d+)*")


@dataclass(frozen=True)
class GlobalConfig:
    """The developer's own giml settings (``~/.giml/config.yml``); knows nothing about projects."""

    jdks: tuple[Path, ...] = ()


def default_global_config_path() -> Path:
    return Path.home() / ".giml" / "config.yml"


def load_global_config(path: Path) -> GlobalConfig:
    """Load the global config; a missing file means no settings. Raises ConfigError naming the file."""
    data = _load_yaml(path)
    if data is None:
        return GlobalConfig()
    root = _Section(data, f"{path}")
    jdks = root.mapping("jdks")
    if not isinstance(jdks, list):
        raise ConfigError(f"{path}.jdks: expected a list of JDK home directories, got {jdks!r}")
    root.finish()
    return GlobalConfig(tuple(_absolute_path(entry, f"{path}.jdks[{i}]") for i, entry in enumerate(jdks)))


ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
SCHEMA_PREFIX = re.compile(r"[a-z][a-z0-9_]{0,20}")
PLACEHOLDER = re.compile(r"\$\{([A-Z_]+)\}")
RUNTIME_PLACEHOLDERS = frozenset({"PORT", "TRIAL_SCHEMA"})
DATABASE_PLACEHOLDERS = frozenset({"DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"})
SECRET_NAME = re.compile(r"PASSWORD|SECRET|TOKEN|KEY|CREDENTIAL|PASSPHRASE", re.IGNORECASE)
COMMAND_KEYS = frozenset({"command", "cmd", "run", "exec", "script", "args", "arguments", "java_opts", "jvm_args", "shell", "entrypoint"})


@dataclass(frozen=True)
class ReadySettings:
    """When the started application counts as ready (spec 12.2)."""

    http: str | None = None  # a path on the loopback port; without one, a successful connect plus the "Started" log line
    contains: str | None = None  # text the response body must contain
    timeout_seconds: int = 60


@dataclass(frozen=True)
class DatabaseSettings:
    """A PostgreSQL server the startup check gets a fresh schema on, per trial (spec 12.3).

    Only the *names* of the runner's own environment variables are recorded here; the values (including the
    password) are read from the runner's environment and never appear in the repository, a report or a log.
    """

    host_env: str
    port_env: str
    name_env: str  # the database inside which giml_* schemas are created and dropped
    user_env: str
    password_env: str
    schema_prefix: str = "giml"


@dataclass(frozen=True)
class SmokeSettings:
    """The ``smoke`` section: how to boot the packaged application (spec 12.1). Data only, never a command."""

    profile: str  # the application's own smoke profile (it decides what a smoke run configures, Flyway included)
    artifact: str | None = None  # a path relative to the project, for example target/app.jar; default: the one executable jar
    env: dict[str, str] | None = None  # extra environment for the application; values may use ${PORT}, ${TRIAL_SCHEMA}, ${DB_*}
    ready: ReadySettings = ReadySettings()
    database: DatabaseSettings | None = None


@dataclass(frozen=True)
class ProjectSettings:
    """A project's ``.giml/settings.yml``, read from giml's worktree (the base commit)."""

    path: Path
    jdk: str | None = None  # a major ("17") or full ("17.0.16") version
    java_home: Path | None = None
    allow_exclusions: bool = False  # may the planner add <exclusion>s to fix enforcer violations (spec 8.4)
    smoke: SmokeSettings | None = None  # the startup check, when configured (spec 12)


def project_settings_path(project_dir: Path) -> Path:
    return project_dir / ".giml" / "settings.yml"


def load_project_settings(project_dir: Path) -> ProjectSettings:
    """Load a project's settings; a missing or empty file means none. Raises ConfigError."""
    path = project_settings_path(project_dir)
    data = _load_yaml(path)
    if data is None:
        return ProjectSettings(path)
    root = _Section(data, f"{path}")
    jdk = root.optional("jdk")
    java_home = root.optional("java_home")
    allow_exclusions = root.optional("allow_exclusions")
    smoke_data = root.optional("smoke")
    root.finish()
    if allow_exclusions is None:
        allow_exclusions = False
    elif not isinstance(allow_exclusions, bool):
        raise ConfigError(f"{path}.allow_exclusions: expected true or false, got {allow_exclusions!r}")
    if jdk is not None and java_home is not None:
        raise ConfigError(f"{path}: set jdk or java_home, not both")
    if jdk is not None:
        if isinstance(jdk, bool) or not isinstance(jdk, (int, str)) or not JDK_VERSION.fullmatch(str(jdk)):
            raise ConfigError(f"{path}.jdk: expected a version such as 17 or \"17.0.16\" (quote dotted "
                              f"versions), got {jdk!r}")  # fmt: skip
        jdk = str(jdk)
    if java_home is not None:
        java_home = _absolute_path(java_home, f"{path}.java_home")
    return ProjectSettings(path, jdk, java_home, allow_exclusions, _parse_smoke(smoke_data, f"{path}.smoke") if smoke_data is not None else None)


def _env_name(value: Any, where: str) -> str:
    if not isinstance(value, str) or not ENV_NAME.fullmatch(value):
        raise ConfigError(f"{where}: expected the NAME of an environment variable (upper case letters, digits, _), got {value!r}")
    return value


def _reject_commands(data: Any, where: str) -> None:
    if isinstance(data, dict):
        commands = sorted(str(k) for k in data if str(k).lower() in COMMAND_KEYS)
        if commands:
            raise ConfigError(f"{where}: {', '.join(commands)}: settings are data only and never carry a command (hard rule 5)")


def _parse_ready(data: Any, where: str) -> ReadySettings:
    _reject_commands(data, where)
    section = _Section(data, where)
    http, contains, timeout = section.optional("http"), section.optional("contains"), section.optional("timeout_seconds")
    section.finish()
    if http is not None and (not isinstance(http, str) or not http.startswith("/") or re.search(r"\s", http)):
        raise ConfigError(f"{where}.http: expected a path starting with / and no spaces, got {http!r}")
    if contains is not None and (not isinstance(contains, str) or not contains):
        raise ConfigError(f"{where}.contains: expected a non-empty string, got {contains!r}")
    if contains is not None and http is None:
        raise ConfigError(f"{where}.contains: needs http, the response it is looked for in")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 600):
        raise ConfigError(f"{where}.timeout_seconds: expected 1-600, got {timeout!r}")
    return ReadySettings(http, contains, 60 if timeout is None else timeout)


def _parse_database(data: Any, where: str) -> DatabaseSettings:
    _reject_commands(data, where)
    section = _Section(data, where)
    kind = section.text("kind")
    settings = DatabaseSettings(*(_env_name(section.optional(f"{name}_env"), f"{where}.{name}_env")
                                  for name in ("host", "port", "name", "user", "password")))  # fmt: skip
    prefix = section.optional("schema_prefix")
    section.finish()
    if kind != "postgresql":
        raise ConfigError(f"{where}.kind: only postgresql is supported, got {kind!r}")
    if prefix is None:
        return settings
    if not isinstance(prefix, str) or not SCHEMA_PREFIX.fullmatch(prefix):
        raise ConfigError(f"{where}.schema_prefix: expected lower case letters, digits and _ starting with a letter (at most 21), got {prefix!r}")
    return dataclasses.replace(settings, schema_prefix=prefix)


def _parse_smoke_env(data: Any, where: str, database: bool) -> dict[str, str]:
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: expected a mapping of environment variable names to values")
    allowed = RUNTIME_PLACEHOLDERS | (DATABASE_PLACEHOLDERS if database else frozenset())
    env: dict[str, str] = {}
    for name, value in data.items():
        _env_name(name, f"{where} key {name!r}")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ConfigError(f"{where}.{name}: expected a string, got {value!r}")
        text = str(value)
        unknown = sorted(set(PLACEHOLDER.findall(text)) - allowed)
        if unknown:
            raise ConfigError(f"{where}.{name}: unknown placeholder(s) ${{{'}, ${'.join(unknown)}}} "
                              f"(known: {', '.join(sorted(allowed))}; the DB_* ones need a database section)")  # fmt: skip
        if SECRET_NAME.search(name) and PLACEHOLDER.sub("", text).strip():
            raise ConfigError(f"{where}.{name}: looks like a secret with a literal value; secrets come from the runner's own "
                              "environment (use a ${DB_PASSWORD} placeholder or leave it out), never from the repository")  # fmt: skip
        env[name] = text
    return env


def _parse_smoke(data: Any, where: str) -> SmokeSettings:
    _reject_commands(data, where)
    section = _Section(data, where)
    profile, artifact = section.text("profile"), section.optional("artifact")
    env_data, ready_data, database_data = section.optional("env"), section.optional("ready"), section.optional("database")
    section.finish()
    if not PROFILE_NAME.fullmatch(profile):
        raise ConfigError(f"{where}.profile: expected a profile name (letters, digits, - _ .), got {profile!r}")
    if artifact is not None and (not isinstance(artifact, str) or not artifact or artifact.startswith("/") or ".." in Path(artifact).parts):
        raise ConfigError(f"{where}.artifact: expected a path inside the project such as target/app.jar, got {artifact!r}")
    database = _parse_database(database_data, f"{where}.database") if database_data is not None else None
    env = _parse_smoke_env(env_data, f"{where}.env", database is not None) if env_data is not None else {}
    ready = _parse_ready(ready_data, f"{where}.ready") if ready_data is not None else ReadySettings()
    return SmokeSettings(profile, artifact, env, ready, database)
