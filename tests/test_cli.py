import datetime
import shutil
from pathlib import Path

import pytest

from giml import __version__
from giml.cli import Environment, ExitCode, main
from giml.data import central
from giml.data.http import UrlLibFetcher
from giml.data.snapshots import read_manifest
from giml.store.sqlite_store import SqliteStateStore
from tests.conftest import Route
from tests.data.osv_helpers import logback_advisories, write_osv_zip

UTC = datetime.UTC
FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def test_version_flag_prints_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"giml {__version__}"


def test_no_command_is_a_configuration_error(capsys):
    assert main([]) == ExitCode.CONFIGURATION
    assert "usage: giml" in capsys.readouterr().err


class Clock:
    def __init__(self, start: datetime.datetime) -> None:
        self.now = start

    def __call__(self) -> datetime.datetime:
        return self.now


@pytest.fixture
def served(http_server, tmp_path):
    """A loopback 'internet': OSV bulk zip plus Central metadata for two artifacts."""
    zip_bytes = write_osv_zip(tmp_path / "all.zip", logback_advisories()).read_bytes()
    http_server.routes["/osv/Maven/all.zip"] = Route(zip_bytes)
    base = f"{http_server.base_url}/maven2"
    logback = "/maven2/ch/qos/logback/logback-core"
    http_server.routes[f"{logback}/maven-metadata.xml"] = Route(
        b"<metadata><versioning><versions><version>1.5.13</version><version>1.5.9</version>"
        b"</versions></versioning></metadata>"
    )
    for version, date in (("1.5.9", "Thu, 03 Oct 2024 10:00:00 GMT"), ("1.5.13", "Wed, 18 Dec 2024 09:00:00 GMT")):
        http_server.routes[f"{logback}/{version}/logback-core-{version}.pom"] = Route(headers={"Last-Modified": date})
    clock = Clock(NOW)
    env = Environment(
        fetcher=UrlLibFetcher(timeout_seconds=5),
        clock=clock,
        osv_url=f"{http_server.base_url}/osv/Maven/all.zip",
        central_url=base,
        environ={},
    )
    return env, clock, http_server


def test_sync_then_status_end_to_end(served, tmp_path, capsys):
    env, clock, server = served
    state = tmp_path / "state"
    code = main(["--state-dir", str(state), "sync", "--coordinate", "ch.qos.logback:logback-core",
                 "--coordinate", "com.example:absent"], env)  # fmt: skip
    out = capsys.readouterr().out
    assert code == ExitCode.SUCCESS, out
    assert "osv: osv-20260924T120000Z-" in out and "(10 advisories, 0 withdrawn, 0 malformed)" in out
    assert "central: central-20260924T120000Z-" in out and "2 coordinates, 2 versions, 1 not found" in out

    with SqliteStateStore(state / "state.db") as store:
        osv_info, central_info = store.latest_snapshot("osv"), store.latest_snapshot("central")
    assert read_manifest(osv_info.path)["content_hash"] == osv_info.content_hash
    releases = central.LocalCentralMetadataSource(central_info).versions(
        central.Coordinate("ch.qos.logback", "logback-core")
    )
    assert [(r.version, r.released_at) for r in releases] == [
        ("1.5.9", datetime.datetime(2024, 10, 3, 10, 0, tzinfo=UTC)),
        ("1.5.13", datetime.datetime(2024, 12, 18, 9, 0, tzinfo=UTC)),
    ]

    clock.now = NOW + datetime.timedelta(hours=3, minutes=5)
    assert main(["--state-dir", str(state), "status"], env) == ExitCode.SUCCESS
    status = capsys.readouterr().out.splitlines()
    assert status[0] == f"state dir: {state}"
    assert status[1].startswith(f"osv: {osv_info.id}  fetched 2026-09-24T12:00:00Z (3h ago)  hash ")
    assert "advisories 10" in status[1]
    assert status[2].startswith(f"central: {central_info.id}") and "coordinates 2" in status[2]


def test_central_sync_reads_project_pom_and_warns_on_unresolved(served, tmp_path, capsys):
    env, _, server = served
    project = tmp_path / "project"
    project.mkdir()
    shutil.copy(FIXTURES / "poms" / "sample-pom.xml", project / "pom.xml")
    code = main(["--state-dir", str(tmp_path / "state"), "sync", "--central", str(project)], env)
    captured = capsys.readouterr()
    assert code == ExitCode.SUCCESS
    assert "skipped unresolvable dependency ${undefined.group}:mystery" in captured.err
    assert "central: " in captured.out and "osv:" not in captured.out
    requested = {path for method, path, _ in server.requests if method == "GET"}
    assert "/maven2/org/jacoco/jacoco-maven-plugin/maven-metadata.xml" in requested


def test_second_central_sync_only_heads_new_versions(served, tmp_path, capsys):
    env, clock, server = served
    args = ["--state-dir", str(tmp_path / "state"), "sync", "--central", "--coordinate", "ch.qos.logback:logback-core"]
    assert main(args, env) == ExitCode.SUCCESS
    heads_before = sum(1 for method, _, _ in server.requests if method == "HEAD")
    clock.now = NOW + datetime.timedelta(days=1)
    assert main(["--state-dir", str(tmp_path / "state"), "sync", "--central"], env) == ExitCode.SUCCESS
    assert sum(1 for method, _, _ in server.requests if method == "HEAD") == heads_before == 2
    assert "1 coordinates" in capsys.readouterr().out.splitlines()[-1]


def test_status_without_snapshots(tmp_path, capsys):
    env = Environment(clock=lambda: NOW, environ={"GIML_STATE_DIR": str(tmp_path / "s")})
    assert main(["status"], env) == ExitCode.SUCCESS
    out = capsys.readouterr().out
    assert f"state dir: {tmp_path / 's'}" in out
    assert "osv: no snapshot (run `giml sync --osv`)" in out
    assert "central: no snapshot (run `giml sync --central`)" in out


def test_state_dir_defaults_to_home(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert main(["status"], Environment(clock=lambda: NOW, environ={})) == ExitCode.SUCCESS
    assert f"state dir: {tmp_path / '.giml'}" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["sync", "--central"], "nothing to sync from Central"),
        (["sync", "--osv", "--coordinate", "a:b"], "applies only to --central"),
        (["sync", "--central", "--coordinate", "not-a-coordinate"], "--coordinate: expected groupId:artifactId"),
    ],
)
def test_usage_errors_exit_5(tmp_path, capsys, args, message):
    code = main(["--state-dir", str(tmp_path), *args], Environment(clock=lambda: NOW, environ={}))
    assert code == ExitCode.CONFIGURATION
    assert message in capsys.readouterr().err


def test_unparseable_pom_exits_5(tmp_path, capsys):
    (tmp_path / "pom.xml").write_text("<project>")
    code = main(["--state-dir", str(tmp_path / "s"), "sync", "--central", str(tmp_path)], Environment(environ={}))
    assert code == ExitCode.CONFIGURATION
    assert "cannot parse" in capsys.readouterr().err


def test_network_failure_exits_4_and_names_url(served, tmp_path, capsys):
    env, _, server = served
    server.routes["/osv/Maven/all.zip"] = Route(status=503)
    assert main(["--state-dir", str(tmp_path / "s"), "sync", "--osv"], env) == ExitCode.INFRASTRUCTURE
    assert "/osv/Maven/all.zip: HTTP 503" in capsys.readouterr().err
    assert list((tmp_path / "s" / "snapshots" / "osv").iterdir()) == []


def test_corrupt_download_exits_4(served, tmp_path, capsys):
    env, _, server = served
    server.routes["/osv/Maven/all.zip"] = Route(b"not a zip")
    assert main(["--state-dir", str(tmp_path / "s"), "sync", "--osv"], env) == ExitCode.INFRASTRUCTURE
    assert "giml: error:" in capsys.readouterr().err
