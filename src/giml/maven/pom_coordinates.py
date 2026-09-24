"""Read the ``groupId:artifactId`` coordinates a single ``pom.xml`` declares (read-only).

Used by ``giml sync --central`` to know which artifacts to fetch metadata for. This is not
dependency resolution (that is milestone 5): only the POM's own declarations are read, with
simple property interpolation. Anything that cannot be resolved is reported, not guessed.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from giml.core.model import Coordinate
from giml.maven import pom_xml
from giml.maven.pom_xml import PomError

__all__ = ["PomCoordinates", "PomError", "read_pom_coordinates"]

_PROPERTY = re.compile(r"\$\{([^}]+)\}")
_DEFAULT_PLUGIN_GROUP = "org.apache.maven.plugins"


@dataclass
class PomCoordinates:
    coordinates: set[Coordinate] = field(default_factory=set)
    unresolved: list[str] = field(default_factory=list)  # human-readable descriptions


class _Unresolved(Exception):
    """A ``${...}`` reference with no value, or a reference cycle."""


class _Interpolator:
    def __init__(self, project: ET.Element) -> None:
        parent = pom_xml.child(project, "parent")
        group = pom_xml.text(project, "groupId") or pom_xml.text(parent, "groupId")
        self.values: dict[str, str | None] = {
            "project.groupId": group,
            "pom.groupId": group,
            "groupId": group,
            "project.artifactId": pom_xml.text(project, "artifactId"),
            "project.parent.groupId": pom_xml.text(parent, "groupId"),
            "project.parent.artifactId": pom_xml.text(parent, "artifactId"),
        }
        for prop in pom_xml.children(project, "properties"):
            for entry in prop:
                self.values.setdefault(pom_xml.local(entry.tag), (entry.text or "").strip())

    def resolve(self, text: str | None, seen: frozenset[str] = frozenset()) -> str | None:
        if text is None:
            return None

        def substitute(match: re.Match) -> str:
            name = match.group(1)
            value = self.values.get(name)
            resolved = None if value is None or name in seen else self.resolve(value, seen | {name})
            if resolved is None:
                raise _Unresolved
            return resolved

        try:
            return _PROPERTY.sub(substitute, text)
        except _Unresolved:
            return None


def read_pom_coordinates(pom: Path) -> PomCoordinates:
    root = pom_xml.parse_project(pom)

    interpolate = _Interpolator(root)
    result = PomCoordinates()

    def add(kind: str, element: ET.Element, default_group: str | None = None) -> None:
        raw_group = pom_xml.text(element, "groupId") or default_group
        raw_artifact = pom_xml.text(element, "artifactId")
        group, artifact = interpolate.resolve(raw_group), interpolate.resolve(raw_artifact)
        try:
            coordinate = Coordinate(group or "", artifact or "")
        except ValueError:
            result.unresolved.append(f"{kind} {raw_group}:{raw_artifact}")
            return
        result.coordinates.add(coordinate)

    parent = pom_xml.child(root, "parent")
    if parent is not None:
        add("parent", parent)
    scopes = [root, *pom_xml.children(root, "profiles", "profile")]
    for scope in scopes:
        for dep in pom_xml.children(scope, "dependencies", "dependency"):
            add("dependency", dep)
        for dep in pom_xml.children(scope, "dependencyManagement", "dependencies", "dependency"):
            add("managed dependency", dep)
        for plugin in pom_xml.children(scope, "build", "plugins", "plugin"):
            add("plugin", plugin, _DEFAULT_PLUGIN_GROUP)
        for plugin in pom_xml.children(scope, "build", "pluginManagement", "plugins", "plugin"):
            add("managed plugin", plugin, _DEFAULT_PLUGIN_GROUP)
    return result
