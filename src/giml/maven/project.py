"""Project shape checks: phase 1 supports exactly one pom.xml with no modules (spec section 3)."""

from __future__ import annotations

from pathlib import Path

from giml.maven import pom_xml
from giml.maven.pom_xml import PomError


class MissingPomError(PomError):
    """The project directory has no pom.xml (exit 5)."""


class UnsupportedProjectError(ValueError):
    """The project is a shape phase 1 does not support (exit 3)."""


def check_single_module(project_dir: Path) -> Path:
    """The project's pom.xml, after checking it declares no <modules>. Raises on any problem."""
    pom = project_dir / "pom.xml"
    if not pom.is_file():
        raise MissingPomError(f"{project_dir}: no pom.xml")
    root = pom_xml.parse_project(pom)
    scopes = [root, *pom_xml.children(root, "profiles", "profile")]
    if any(pom_xml.children(scope, "modules", "module") for scope in scopes):
        raise UnsupportedProjectError(f"{pom}: multi-module projects unsupported in phase 1")
    return pom
