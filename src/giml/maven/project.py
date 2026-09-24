"""Reactor discovery: the project's pom.xml plus every module it declares (spec section 3)."""

from __future__ import annotations

from pathlib import Path

from giml.maven import pom_xml
from giml.maven.pom_xml import PomError


class MissingPomError(PomError):
    """A pom.xml the project needs does not exist (exit 5)."""


class UnsupportedProjectError(ValueError):
    """The project is a shape phase 1 does not support (exit 3)."""


def _module_pom(parent_pom: Path, module: str) -> Path:
    target = parent_pom.parent / module
    return target if module.endswith(".xml") else target / "pom.xml"


def discover_reactor(project_dir: Path) -> list[Path]:
    """Every pom.xml in the reactor, root first, then modules depth-first in declared order.

    Modules declared inside profiles are included, because any profile may be active in a build.
    """
    root = project_dir / "pom.xml"
    if not root.is_file():
        raise MissingPomError(f"{project_dir}: no pom.xml")
    top = project_dir.resolve()
    found: list[Path] = []

    def visit(pom: Path) -> None:
        resolved = pom.resolve()
        if resolved in found:
            return
        if not resolved.is_relative_to(top):
            raise UnsupportedProjectError(f"{pom}: module outside the project directory {top}")
        if not resolved.is_file():
            raise MissingPomError(f"{pom}: declared module has no pom.xml")
        found.append(resolved)
        project = pom_xml.parse_project(resolved)
        for scope in [project, *pom_xml.children(project, "profiles", "profile")]:
            for module in pom_xml.children(scope, "modules", "module"):
                if module.text and module.text.strip():
                    visit(_module_pom(resolved, module.text.strip()))

    visit(root)
    return found
