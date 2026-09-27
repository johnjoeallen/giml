"""Choose the JDK a project builds with.

A project's ``.giml/settings.yml`` names either a ``java_home`` (any directory with ``bin/javac``)
or a ``jdk`` version. A version is looked up in the developer's global config (``jdks``) and then
in Maven's ``~/.m2/toolchains.xml``; the highest matching version wins. Without either setting the
inherited JAVA_HOME/PATH is used, as the developer's own build would.
"""

from __future__ import annotations

import os
import re
import shutil
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from giml.core.config import ConfigError, ProjectSettings, default_global_config_path, load_global_config
from giml.maven.pom_xml import child, children, text

SOURCE_JAVA_HOME = "settings java_home"
SOURCE_CONFIG = "global config"
SOURCE_TOOLCHAINS = "maven toolchains"
SOURCE_INHERITED = "inherited"


@dataclass(frozen=True)
class Jdk:
    home: Path | None  # None only when inherited and no java is found
    version: str | None  # from the JDK's `release` file, when it has one
    source: str

    def build_env(self, environ: Mapping[str, str]) -> dict[str, str] | None:
        """The environment for Maven and java; None keeps the inherited one."""
        if self.source == SOURCE_INHERITED or self.home is None:
            return None
        env = dict(environ)
        env["JAVA_HOME"] = str(self.home)
        env["PATH"] = os.pathsep.join(p for p in (str(self.home / "bin"), environ.get("PATH")) if p)
        return env

    def record(self) -> dict:
        return {"version": self.version, "home": str(self.home) if self.home else None, "source": self.source}


def looks_like_jdk(home: Path) -> bool:
    javac = home / "bin" / "javac"
    return javac.is_file() or javac.with_suffix(".exe").is_file()


def release_version(home: Path) -> str | None:
    try:
        text = (home / "release").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r'^JAVA_VERSION="?([^"\n]+)"?\s*$', text, re.M)
    return match.group(1).strip() if match else None


def version_key(version: str) -> tuple[int, ...]:
    """Leading numeric components, with Java 8's `1.8.0_392` style read as `8.0.392`."""
    parts: list[int] = []
    for part in re.split(r"[._+-]", version):
        if not part.isdigit():
            break
        parts.append(int(part))
    if len(parts) > 1 and parts[0] == 1:
        parts = parts[1:]
    return tuple(parts)


def matches(wanted: str, version: str) -> bool:
    """`17` matches every 17.x; a full version matches only itself (component by component)."""
    want = version_key(wanted)
    return bool(want) and version_key(version)[: len(want)] == want


@dataclass(frozen=True)
class JdkCatalog:
    """Where installed JDKs are listed: the global config, then Maven's toolchains file."""

    config_path: Path
    toolchains_path: Path

    def configured(self) -> list[tuple[Path, str]]:
        found = []
        for home in load_global_config(self.config_path).jdks:
            if not looks_like_jdk(home):
                raise ConfigError(f"{self.config_path}: jdks: {home} is not a JDK (no bin/javac)")
            version = release_version(home)
            if version is None:
                raise ConfigError(f"{self.config_path}: jdks: {home} has no JAVA_VERSION in its release file")
            found.append((home, version))
        return found

    def toolchains(self) -> list[tuple[Path, str]]:
        """JDK toolchains whose jdkHome is a JDK; others are skipped (this is only a fallback)."""
        try:
            root = ET.parse(self.toolchains_path).getroot()
        except FileNotFoundError:
            return []
        except (OSError, ET.ParseError) as exc:
            raise ConfigError(f"{self.toolchains_path}: cannot read: {exc}") from exc
        found = []
        for toolchain in children(root, "toolchain"):
            jdk_home = text(child(toolchain, "configuration"), "jdkHome")
            if text(toolchain, "type") != "jdk" or not jdk_home:
                continue
            home = Path(jdk_home).expanduser()
            if not home.is_absolute() or not looks_like_jdk(home):
                continue
            version = release_version(home) or text(child(toolchain, "provides"), "version")
            if version:
                found.append((home, version))
        return found

    def find(self, wanted: str) -> Jdk:
        for source, listed in ((SOURCE_CONFIG, self.configured), (SOURCE_TOOLCHAINS, self.toolchains)):
            hits = [(home, version) for home, version in listed() if matches(wanted, version)]
            if hits:
                home, version = max(hits, key=lambda hit: version_key(hit[1]))
                return Jdk(home, version, source)
        raise ConfigError(f"no JDK {wanted} in {self.config_path} (jdks) or {self.toolchains_path}")


def catalog(config_path: Path | None = None) -> JdkCatalog:
    """The developer's catalog: the given (or default) global config and ~/.m2/toolchains.xml."""
    return JdkCatalog(config_path or default_global_config_path(), Path.home() / ".m2" / "toolchains.xml")


def inherited_jdk(environ: Mapping[str, str]) -> Jdk:
    """The JDK a plain `mvn` would use: JAVA_HOME, else the `java` on PATH."""
    if environ.get("JAVA_HOME"):
        home: Path | None = Path(environ["JAVA_HOME"])
    else:
        java = shutil.which("java", path=environ.get("PATH", ""))
        home = Path(java).resolve().parent.parent if java else None
    return Jdk(home, release_version(home) if home else None, SOURCE_INHERITED)


def resolve_jdk(settings: ProjectSettings, catalog: JdkCatalog, environ: Mapping[str, str]) -> Jdk:
    if settings.java_home is not None:
        if not looks_like_jdk(settings.java_home):
            raise ConfigError(f"{settings.path}: java_home {settings.java_home} is not a JDK (no bin/javac)")
        return Jdk(settings.java_home, release_version(settings.java_home), SOURCE_JAVA_HOME)
    if settings.jdk is not None:
        try:
            return catalog.find(settings.jdk)
        except ConfigError as exc:
            raise ConfigError(f"{settings.path}: jdk {settings.jdk}: {exc}") from exc
    return inherited_jdk(environ)
