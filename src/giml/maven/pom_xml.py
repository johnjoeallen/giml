"""Namespace-agnostic, read-only helpers for navigating a pom.xml."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path


class PomError(ValueError):
    """The POM cannot be read or parsed."""


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child(element: ET.Element | None, name: str) -> ET.Element | None:
    if element is None:
        return None
    return next((c for c in element if local(c.tag) == name), None)


def children(element: ET.Element, *path: str) -> list[ET.Element]:
    nodes = [element]
    for name in path:
        nodes = [c for n in nodes for c in n if local(c.tag) == name]
    return nodes


def text(element: ET.Element | None, name: str) -> str | None:
    found = child(element, name)
    return found.text.strip() if found is not None and found.text else None


def parse_project(pom: Path) -> ET.Element:
    """The <project> root element of a POM. Raises PomError."""
    try:
        root = ET.parse(pom).getroot()
    except (OSError, ET.ParseError) as exc:
        raise PomError(f"{pom}: cannot parse: {exc}") from exc
    if local(root.tag) != "project":
        raise PomError(f"{pom}: root element is <{local(root.tag)}>, expected <project>")
    return root
