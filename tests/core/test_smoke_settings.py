import pytest

from giml.core.config import ConfigError, DatabaseSettings, ReadySettings, load_project_settings

FULL = """
smoke:
  profile: smoke
  artifact: target/app.jar
  env:
    SPRING_DATASOURCE_URL: jdbc:postgresql://${DB_HOST}:${DB_PORT}/${DB_NAME}?currentSchema=${TRIAL_SCHEMA}
    SPRING_DATASOURCE_USERNAME: ${DB_USER}
    SPRING_DATASOURCE_PASSWORD: ${DB_PASSWORD}
    SPRING_FLYWAY_SCHEMAS: ${TRIAL_SCHEMA}
    SERVER_PORT: ${PORT}
  ready:
    http: /actuator/health
    contains: UP
    timeout_seconds: 90
  database:
    kind: postgresql
    host_env: GIML_PG_HOST
    port_env: GIML_PG_PORT
    name_env: GIML_PG_DATABASE
    user_env: GIML_PG_USER
    password_env: GIML_PG_PASSWORD
    schema_prefix: giml
"""


def load(tmp_path, text):
    (tmp_path / ".giml").mkdir(exist_ok=True)
    (tmp_path / ".giml" / "settings.yml").write_text(text)
    return load_project_settings(tmp_path)


def test_a_project_without_a_smoke_section_has_no_startup_check(tmp_path):
    assert load(tmp_path, "jdk: 17\n").smoke is None


def test_a_full_smoke_section_is_read_as_data(tmp_path):
    smoke = load(tmp_path, FULL).smoke
    assert (smoke.profile, smoke.artifact) == ("smoke", "target/app.jar")
    assert smoke.ready == ReadySettings("/actuator/health", "UP", 90)
    assert smoke.database == DatabaseSettings("GIML_PG_HOST", "GIML_PG_PORT", "GIML_PG_DATABASE", "GIML_PG_USER", "GIML_PG_PASSWORD", "giml")
    assert smoke.env["SPRING_FLYWAY_SCHEMAS"] == "${TRIAL_SCHEMA}" and smoke.env["SPRING_DATASOURCE_PASSWORD"] == "${DB_PASSWORD}"


def test_the_smallest_smoke_section_needs_only_the_profile(tmp_path):
    smoke = load(tmp_path, "smoke:\n  profile: ci-smoke\n").smoke
    assert (smoke.profile, smoke.env, smoke.ready, smoke.database, smoke.artifact) == ("ci-smoke", {}, ReadySettings(), None, None)
    assert smoke.ready.timeout_seconds == 60


@pytest.mark.parametrize("text, message", [
    ("smoke:\n  env: {A: b}\n", "profile: required key missing"),
    ("smoke:\n  profile: 'bad profile'\n", "expected a profile name"),
    ("smoke:\n  profile: s\n  command: java -jar x\n", "settings are data only and never carry a command"),
    ("smoke:\n  profile: s\n  ready:\n    exec: curl x\n", "never carry a command"),
    ("smoke:\n  profile: s\n  surprise: 1\n", "unknown key\\(s\\): surprise"),
    ("smoke:\n  profile: s\n  artifact: /etc/app.jar\n", "expected a path inside the project"),
    ("smoke:\n  profile: s\n  artifact: ../x.jar\n", "expected a path inside the project"),
    ("smoke:\n  profile: s\n  env:\n    lower: x\n", "expected the NAME of an environment variable"),
    ("smoke:\n  profile: s\n  env:\n    A: ${NOPE}\n", "unknown placeholder"),
    ("smoke:\n  profile: s\n  env:\n    A: ${DB_HOST}\n", "unknown placeholder"),  # no database section
    ("smoke:\n  profile: s\n  env:\n    DB_PASSWORD: hunter2\n", "looks like a secret with a literal value"),
    ("smoke:\n  profile: s\n  env:\n    API_TOKEN: abc\n", "looks like a secret"),
    ("smoke:\n  profile: s\n  ready:\n    http: health\n", "starting with /"),
    ("smoke:\n  profile: s\n  ready:\n    contains: UP\n", "needs http"),
    ("smoke:\n  profile: s\n  ready:\n    http: /h\n    timeout_seconds: 0\n", "expected 1-600"),
    ("smoke:\n  profile: s\n  database:\n    kind: mysql\n    host_env: A\n    port_env: B\n    name_env: C\n    user_env: D\n    password_env: E\n", "only postgresql"),
    ("smoke:\n  profile: s\n  database:\n    kind: postgresql\n    host_env: localhost\n    port_env: B\n    name_env: C\n    user_env: D\n    password_env: E\n", "NAME of an environment variable"),
    ("smoke:\n  profile: s\n  database:\n    kind: postgresql\n    host_env: A\n    port_env: B\n    name_env: C\n    user_env: D\n", "password_env"),
    ("smoke:\n  profile: s\n  database:\n    kind: postgresql\n    host_env: A\n    port_env: B\n    name_env: C\n    user_env: D\n    password_env: E\n    schema_prefix: Bad-Prefix\n", "schema_prefix"),
])
def test_invalid_smoke_settings_are_refused_with_the_reason(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load(tmp_path, text)


def test_a_placeholder_password_is_allowed_because_it_is_not_a_value(tmp_path):
    text = FULL.replace("SPRING_FLYWAY_SCHEMAS: ${TRIAL_SCHEMA}", "SPRING_FLYWAY_SCHEMAS: ${TRIAL_SCHEMA}\n    MY_SECRET: ${DB_PASSWORD}")
    assert load(tmp_path, text).smoke.env["MY_SECRET"] == "${DB_PASSWORD}"
