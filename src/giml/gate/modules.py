"""Facts about each reactor module that assessment needs (sources, tests, build outputs)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from giml.maven import pom_xml

_IT_NAME = re.compile(r"^(IT.*|.*IT|.*ITCase)\.java$")


@dataclass(frozen=True)
class ModuleFacts:
    pom: Path
    packaging: str
    main_sources: int  # .java files under src/main/java
    test_sources: int  # .java files under src/test/java (unit and integration)
    integration_tests: int  # Failsafe-style names: IT*, *IT, *ITCase

    @property
    def directory(self) -> Path:
        return self.pom.parent

    @property
    def untested(self) -> bool:
        """Production code with no tests at all: PIT cannot analyse it (spec 6.3)."""
        return self.main_sources > 0 and self.test_sources == 0

    @property
    def classes_dir(self) -> Path:
        return self.directory / "target" / "classes"

    @property
    def jacoco_exec(self) -> Path:
        return self.directory / "target" / "jacoco.exec"

    @property
    def jacoco_report(self) -> Path:
        return self.directory / "target" / "site" / "jacoco" / "jacoco.xml"

    @property
    def pit_report(self) -> Path:
        return self.directory / "target" / "pit-reports" / "mutations.xml"

    @property
    def surefire_reports(self) -> Path:
        return self.directory / "target" / "surefire-reports"


def _java(directory: Path) -> list[Path]:
    return sorted(directory.rglob("*.java")) if directory.is_dir() else []


def module_facts(pom: Path) -> ModuleFacts:
    project = pom_xml.parse_project(pom)
    tests = _java(pom.parent / "src" / "test" / "java")
    return ModuleFacts(
        pom=pom,
        packaging=pom_xml.text(project, "packaging") or "jar",
        main_sources=len(_java(pom.parent / "src" / "main" / "java")),
        test_sources=len(tests),
        integration_tests=sum(1 for t in tests if _IT_NAME.match(t.name)),
    )


def declares_failsafe(reactor: list[Path]) -> bool:
    return any("maven-failsafe-plugin" in pom.read_text(encoding="utf-8") for pom in reactor)
