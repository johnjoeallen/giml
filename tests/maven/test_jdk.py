import os
from pathlib import Path

import pytest

from giml.core.config import ConfigError, ProjectSettings
from giml.maven.jdk import (
    SOURCE_CONFIG, SOURCE_INHERITED, SOURCE_JAVA_HOME, SOURCE_TOOLCHAINS, Jdk, JdkCatalog, catalog, inherited_jdk,
    matches, release_version, resolve_jdk, version_key,
)  # fmt: skip

TOOLCHAINS_NS = "http://maven.apache.org/TOOLCHAINS/1.1.0"


def make_jdk(home: Path, version: str | None = None, javac: bool = True) -> Path:
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "java").write_text("")
    if javac:
        (home / "bin" / "javac").write_text("")
    if version is not None:
        (home / "release").write_text(f'IMPLEMENTOR="Eclipse Adoptium"\nJAVA_VERSION="{version}"\nOS_NAME="Linux"\n')
    return home


def toolchain(home, version="17", kind="jdk") -> str:
    return (f"<toolchain><type>{kind}</type><provides><version>{version}</version></provides>"
            f"<configuration><jdkHome>{home}</jdkHome></configuration></toolchain>")  # fmt: skip


def write_toolchains(path: Path, *entries: str) -> Path:
    path.write_text(f'<?xml version="1.0"?>\n<toolchains xmlns="{TOOLCHAINS_NS}">{"".join(entries)}</toolchains>\n')
    return path


def make_catalog(tmp_path, jdks=(), toolchains=None) -> JdkCatalog:
    config = tmp_path / "config.yml"
    config.write_text("jdks:\n" + "".join(f"  - {home}\n" for home in jdks) if jdks else "")
    return JdkCatalog(config, toolchains or tmp_path / "no-toolchains.xml")


@pytest.mark.parametrize(("version", "key"), [
    ("17", (17,)), ("17.0.16", (17, 0, 16)), ("21.0.9+10", (21, 0, 9, 10)), ("1.8.0_392", (8, 0, 392)),
    ("25-ea", (25,)), ("1", (1,)), ("abc", ()), ("", ()),
])  # fmt: skip
def test_version_key(version, key):
    assert version_key(version) == key


@pytest.mark.parametrize(("wanted", "version", "expected"), [
    ("17", "17.0.16", True), ("17", "17", True), ("17.0.16", "17.0.16", True), ("17.0", "17.0.16", True),
    ("7", "1.7.0_80", True), ("17", "1.7.0_80", False), ("8", "1.8.0_392", True), ("1.8", "1.8.0_392", True),
    ("17", "21.0.8", False), ("17.0.16", "17.0.15", False), ("17.0.16", "17", False), ("1", "17.0.1", False),
    ("", "17", False), ("17", "abc", False),
])  # fmt: skip
def test_matches(wanted, version, expected):
    assert matches(wanted, version) is expected


def test_release_version(tmp_path):
    assert release_version(make_jdk(tmp_path / "a", "21.0.9")) == "21.0.9"
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "release").write_text("JAVA_VERSION=17.0.2\n")
    assert release_version(tmp_path / "b") == "17.0.2"
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "release").write_text('IMPLEMENTOR="x"\n')
    assert release_version(tmp_path / "c") is None
    assert release_version(tmp_path / "missing") is None
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "release").write_bytes(b'IMPLEMENTOR="\xff"\nJAVA_VERSION="11.0.2"\n')
    assert release_version(tmp_path / "d") == "11.0.2"


def test_catalog_picks_the_highest_matching_configured_jdk(tmp_path):
    homes = [make_jdk(tmp_path / n, v) for n, v in (("a", "21.0.8"), ("b", "21.0.9"), ("c", "17.0.16"))]
    jdks = make_catalog(tmp_path, homes)
    assert jdks.find("21") == Jdk(homes[1], "21.0.9", SOURCE_CONFIG)
    assert jdks.find("21.0.8") == Jdk(homes[0], "21.0.8", SOURCE_CONFIG)
    assert jdks.find("17") == Jdk(homes[2], "17.0.16", SOURCE_CONFIG)


def test_equal_versions_keep_the_first_listed(tmp_path):
    homes = [make_jdk(tmp_path / n, "25.0.4") for n in ("tem", "zulu")]
    assert make_catalog(tmp_path, homes).find("25").home == homes[0]


def test_configured_entries_must_be_jdks_with_a_version(tmp_path):
    with pytest.raises(ConfigError, match=r"config\.yml: jdks: .*jre is not a JDK \(no bin/javac\)"):
        make_catalog(tmp_path, [make_jdk(tmp_path / "jre", "17", javac=False)]).find("17")
    with pytest.raises(ConfigError, match=r"config\.yml: jdks: .*bare has no JAVA_VERSION"):
        make_catalog(tmp_path, [make_jdk(tmp_path / "bare")]).find("17")


def test_toolchains_are_the_fallback(tmp_path):
    configured = make_jdk(tmp_path / "21", "21.0.9")
    chained = make_jdk(tmp_path / "17", "17.0.16")
    toolchains = write_toolchains(tmp_path / "toolchains.xml", toolchain(chained, "17"))
    jdks = make_catalog(tmp_path, [configured], toolchains)
    assert jdks.find("17") == Jdk(chained, "17.0.16", SOURCE_TOOLCHAINS)
    assert jdks.find("21").source == SOURCE_CONFIG


def test_config_match_does_not_read_toolchains(tmp_path):
    configured = make_jdk(tmp_path / "21", "21.0.9")
    broken = tmp_path / "toolchains.xml"
    broken.write_text("<toolchains>")
    assert make_catalog(tmp_path, [configured], broken).find("21").home == configured
    with pytest.raises(ConfigError, match=r"toolchains\.xml: cannot read"):
        make_catalog(tmp_path, [configured], broken).find("17")


def test_toolchain_entries_that_are_not_usable_jdks_are_skipped(tmp_path):
    declared_only = make_jdk(tmp_path / "declared")  # no release file: the declared version is used
    toolchains = write_toolchains(
        tmp_path / "toolchains.xml",
        toolchain(make_jdk(tmp_path / "nb", "17.0.1"), kind="netbeans"),
        toolchain(tmp_path / "gone", "17"),
        toolchain(make_jdk(tmp_path / "jre", "17.0.2", javac=False), "17"),
        toolchain("relative/jdk", "17"),
        "<toolchain><type>jdk</type><configuration/></toolchain>",
        toolchain(make_jdk(tmp_path / "noversion"), ""),
        toolchain(declared_only, "17"),
    )  # fmt: skip
    assert make_catalog(tmp_path, toolchains=toolchains).toolchains() == [(declared_only, "17")]


def test_toolchain_release_file_beats_the_declared_version(tmp_path):
    home = make_jdk(tmp_path / "jdk", "17.0.16")
    toolchains = write_toolchains(tmp_path / "toolchains.xml", toolchain(home, "17"))
    assert make_catalog(tmp_path, toolchains=toolchains).toolchains() == [(home, "17.0.16")]


def test_no_match_names_both_sources(tmp_path):
    jdks = make_catalog(tmp_path, [make_jdk(tmp_path / "21", "21.0.9")])
    with pytest.raises(ConfigError, match=r"no JDK 17 in .*config\.yml \(jdks\) or .*no-toolchains\.xml"):
        jdks.find("17")


def test_default_catalog_paths(tmp_path):
    assert catalog() == JdkCatalog(Path.home() / ".giml" / "config.yml", Path.home() / ".m2" / "toolchains.xml")
    assert catalog(tmp_path / "c.yml").config_path == tmp_path / "c.yml"


def test_settings_java_home_is_used_when_it_has_javac(tmp_path):
    home = make_jdk(tmp_path / "jdk", "17.0.16")
    settings = ProjectSettings(tmp_path / "settings.yml", java_home=home)
    assert resolve_jdk(settings, make_catalog(tmp_path), {}) == Jdk(home, "17.0.16", SOURCE_JAVA_HOME)
    versionless = make_jdk(tmp_path / "custom")
    assert resolve_jdk(ProjectSettings(tmp_path / "s", java_home=versionless), make_catalog(tmp_path), {}).version is None


def test_settings_java_home_without_javac_is_refused(tmp_path):
    settings = ProjectSettings(tmp_path / "settings.yml", java_home=make_jdk(tmp_path / "jre", "17", javac=False))
    with pytest.raises(ConfigError, match=r"settings\.yml: java_home .*jre is not a JDK \(no bin/javac\)"):
        resolve_jdk(settings, make_catalog(tmp_path), {})


def test_settings_jdk_is_looked_up_and_errors_name_the_settings_file(tmp_path):
    home = make_jdk(tmp_path / "17", "17.0.16")
    jdks = make_catalog(tmp_path, [home])
    assert resolve_jdk(ProjectSettings(tmp_path / "settings.yml", jdk="17"), jdks, {}).home == home
    with pytest.raises(ConfigError, match=r"settings\.yml: jdk 11: no JDK 11 in "):
        resolve_jdk(ProjectSettings(tmp_path / "settings.yml", jdk="11"), jdks, {})


def test_without_settings_the_inherited_java_home_is_used(tmp_path):
    home = make_jdk(tmp_path / "jdk", "25.0.4")
    jdk = resolve_jdk(ProjectSettings(tmp_path / "s"), make_catalog(tmp_path), {"JAVA_HOME": str(home)})
    assert jdk == Jdk(home, "25.0.4", SOURCE_INHERITED)


def test_inherited_falls_back_to_java_on_path(tmp_path):
    home = make_jdk(tmp_path / "jdk", "21.0.9")
    os.chmod(home / "bin" / "java", 0o755)
    link = tmp_path / "links"
    link.mkdir()
    (link / "java").symlink_to(home / "bin" / "java")
    assert inherited_jdk({"PATH": str(link)}) == Jdk(home, "21.0.9", SOURCE_INHERITED)
    assert inherited_jdk({"PATH": str(tmp_path / "empty")}) == Jdk(None, None, SOURCE_INHERITED)
    assert inherited_jdk({}) == Jdk(None, None, SOURCE_INHERITED)


def test_build_env_puts_the_jdk_first(tmp_path):
    jdk = Jdk(tmp_path / "jdk", "17", SOURCE_CONFIG)
    env = jdk.build_env({"PATH": "/usr/bin", "HOME": "/h", "JAVA_HOME": "/old"})
    assert env == {"PATH": f"{tmp_path / 'jdk' / 'bin'}{os.pathsep}/usr/bin", "HOME": "/h",
                   "JAVA_HOME": str(tmp_path / "jdk")}  # fmt: skip
    assert jdk.build_env({})["PATH"] == str(tmp_path / "jdk" / "bin")


def test_inherited_jdk_keeps_the_environment():
    assert Jdk(Path("/jdk"), "17", SOURCE_INHERITED).build_env({"PATH": "/usr/bin"}) is None
    assert Jdk(None, None, SOURCE_INHERITED).build_env({}) is None


def test_record():
    assert Jdk(Path("/jdk"), "17.0.16", SOURCE_CONFIG).record() == {
        "version": "17.0.16", "home": "/jdk", "source": "global config"}  # fmt: skip
    assert Jdk(None, None, SOURCE_INHERITED).record() == {"version": None, "home": None, "source": "inherited"}
