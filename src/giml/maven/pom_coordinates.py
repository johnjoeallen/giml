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

_PROPERTY = re.compile(r"\$\{([^}]+)\}")
_DEFAULT_PLUGIN_GROUP = "org.apache.maven.plugins"


class PomError(ValueError):
    """The POM cannot be read or parsed."""


@dataclass
class PomCoordinates:
    coordinates: set[Coordinate] = field(default_factory=set)
    unresolved: list[str] = field(default_factory=list)  # human-readable descriptions


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element: ET.Element | None, name: str) -> ET.Element | None:
    if element is None:
        return None
    return next((c for c in element if _local(c.tag) == name), None)


def _children(element: ET.Element, *path: str) -> list[ET.Element]:
    nodes = [element]
    for name in path:
        nodes = [c for n in nodes for c in n if _local(c.tag) == name]
    return nodes


def _text(element: ET.Element | None, name: str) -> str | None:
    child = _child(element, name)
    return child.text.strip() if child is not None and child.text else None


class _Unresolved(Exception):
    """A ``${...}`` reference with no value, or a reference cycle."""


class _Interpolator:
    def __init__(self, project: ET.Element) -> None:
        parent = _child(project, "parent")
        group = _text(project, "groupId") or _text(parent, "groupId")
        self.values: dict[str, str | None] = {
            "project.groupId": group,
            "pom.groupId": group,
            "groupId": group,
            "project.artifactId": _text(project, "artifactId"),
            "project.parent.groupId": _text(parent, "groupId"),
            "project.parent.artifactId": _text(parent, "artifactId"),
        }
        for prop in _children(project, "properties"):
            for entry in prop:
                self.values.setdefault(_local(entry.tag), (entry.text or "").strip())

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
    try:
        root = ET.parse(pom).getroot()
    except (OSError, ET.ParseError) as exc:
        raise PomError(f"{pom}: cannot parse: {exc}") from exc
    if _local(root.tag) != "project":
        raise PomError(f"{pom}: root element is <{_local(root.tag)}>, expected <project>")

    interpolate = _Interpolator(root)
    result = PomCoordinates()

    def add(kind: str, element: ET.Element, default_group: str | None = None) -> None:
        raw_group = _text(element, "groupId") or default_group
        raw_artifact = _text(element, "artifactId")
        group, artifact = interpolate.resolve(raw_group), interpolate.resolve(raw_artifact)
        try:
            coordinate = Coordinate(group or "", artifact or "")
        except ValueError:
            result.unresolved.append(f"{kind} {raw_group}:{raw_artifact}")
            return
        result.coordinates.add(coordinate)

    parent = _child(root, "parent")
    if parent is not None:
        add("parent", parent)
    scopes = [root, *_children(root, "profiles", "profile")]
    for scope in scopes:
        for dep in _children(scope, "dependencies", "dependency"):
            add("dependency", dep)
        for dep in _children(scope, "dependencyManagement", "dependencies", "dependency"):
            add("managed dependency", dep)
        for plugin in _children(scope, "build", "plugins", "plugin"):
            add("plugin", plugin, _DEFAULT_PLUGIN_GROUP)
        for plugin in _children(scope, "build", "pluginManagement", "plugins", "plugin"):
            add("managed plugin", plugin, _DEFAULT_PLUGIN_GROUP)
    return result
