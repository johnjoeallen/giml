"""Where each dependency's version is declared in a reactor's POMs (spec sections 8.1 and 8.4).

Read-only. For every dependency (and dependencyManagement entry) in the reactor's POMs it records
the version giml would have to edit and where that text lives: a literal ``<version>``, a property
(followed through the reactor's parent chain and through properties that point at properties), or
the dependencyManagement entry that supplies it. A version that comes from outside the reactor (an
external parent's management, an imported BOM) is reported as such and left unchanged, except that
an external parent's own version is a declaration site (spec 8.4).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from giml.core.model import Coordinate
from giml.maven import pom_edit
from giml.maven.pom_edit import Element
from giml.maven.pom_xml import PomError

LITERAL = "literal"  # a <version> holding the version itself
PROPERTY = "property"  # ${name}, edited where the property holds the literal
MANAGED = "managed"  # no <version>: supplied by a dependencyManagement entry in the reactor
EXTERNAL = "external"  # supplied from outside the reactor; unsupported, left unchanged
BUILTIN = "builtin"  # ${project.version} and friends: the POM's own data, not a dependency's
COMPLEX = "complex"  # text mixing literals and properties; not lossless-editable
UNRESOLVED = "unresolved"  # no version anywhere

_PROPERTY = re.compile(r"\$\{([^}]+)\}")
_BUILTIN_PREFIXES = ("project.", "pom.", "parent.")
_EXTERNAL_PROPERTY = object()  # a property that may be defined in an external parent


@dataclass(frozen=True)
class Site:
    """Text a version edit would replace."""

    pom: Path
    line: int  # 1-based, of the start of the text
    span: tuple[int, int]  # offsets in the file's text
    kind: str  # "version" (a <version> element) or "property"
    name: str | None = None  # the property name when kind is "property"


@dataclass(frozen=True)
class Declaration:
    coordinate: Coordinate
    pom: Path  # the POM containing the declaration
    section: str  # "dependencies" or "dependencyManagement"
    profile: str | None  # the profile id when declared inside a profile
    scope: str | None
    is_bom: bool  # an imported BOM (scope import): a change unit of its own (spec 8.2)
    raw: str | None  # the <version> text as written
    version: str | None  # the effective version when it is known inside the reactor
    origin: str  # LITERAL, PROPERTY, MANAGED, EXTERNAL, BUILTIN, COMPLEX or UNRESOLVED
    site: Site | None  # the text to edit; None when giml cannot edit the version
    managed_by: Declaration | None = None


@dataclass(frozen=True)
class ExternalParent:
    """A parent POM outside the reactor: its version is the declaration site (spec 8.4)."""

    pom: Path
    coordinate: Coordinate
    version: str
    site: Site


@dataclass(frozen=True)
class Declarations:
    declared: tuple[Declaration, ...]
    parents: tuple[ExternalParent, ...]
    skipped: tuple[str, ...]  # declarations whose coordinates could not be read, one line each

    def for_coordinate(self, coordinate: Coordinate) -> list[Declaration]:
        return [d for d in self.declared if d.coordinate == coordinate]


class _Pom:
    def __init__(self, path: Path) -> None:
        try:
            with path.open(encoding="utf-8", newline="") as handle:  # raw: a site's span must index what is written back
                self.text = handle.read()
        except OSError as exc:
            raise PomError(f"{path}: cannot read: {exc.strerror}") from exc
        try:
            self.root = pom_edit.parse(self.text)
        except pom_edit.PomEditError as exc:
            raise PomError(f"{path}: {exc}") from exc
        self.path = path
        self.parent_element = self.root.child("parent")
        self.group = self.value(self.root, "groupId") or self.value(self.parent_element, "groupId")
        self.artifact = self.value(self.root, "artifactId")
        self.parent_pom: _Pom | None = None
        self.external_parent = False

    def value(self, element: Element | None, name: str) -> str | None:
        return pom_edit.text_of(self.text, element.child(name)) if element is not None else None

    def text_of(self, element: Element) -> str | None:
        return pom_edit.text_of(self.text, element)

    def site(self, element: Element, kind: str, name: str | None = None) -> Site:
        span = (element.start_end, element.end_start)
        return Site(self.path, self.text.count("\n", 0, span[0]) + 1, span, kind, name)


class _Reader:
    def __init__(self, poms: Sequence[Path]) -> None:
        self.poms = [_Pom(path) for path in poms]
        by_coordinate = {(p.group, p.artifact): p for p in self.poms if p.group and p.artifact and "${" not in p.group}
        self.skipped: list[str] = []
        self.parents: list[ExternalParent] = []
        for pom in self.poms:
            if pom.parent_element is None:
                continue
            key = (pom.value(pom.parent_element, "groupId"), pom.value(pom.parent_element, "artifactId"))
            pom.parent_pom = by_coordinate.get(key)
            if pom.parent_pom is None:
                pom.external_parent = True
                self._external_parent(pom, key)
        self._made: dict[tuple[int, int], Declaration | None] = {}

    def _external_parent(self, pom: _Pom, key: tuple[str | None, str | None]) -> None:
        version = pom.parent_element.child("version")
        try:
            coordinate = Coordinate(key[0] or "", key[1] or "")
        except ValueError:
            self.skipped.append(f"{pom.path}: parent {key[0]}:{key[1]}")
            return
        if version is not None and pom.text_of(version):
            self.parents.append(ExternalParent(pom.path, coordinate, pom.text_of(version), pom.site(version, "version")))

    def _property(self, pom: _Pom, name: str) -> tuple[_Pom, Element] | object | None:
        current: _Pom | None = pom
        while current is not None:
            properties = current.root.child("properties")
            found = properties.child(name) if properties is not None else None
            if found is not None:
                return current, found
            if current.external_parent:
                return _EXTERNAL_PROPERTY
            current = current.parent_pom
        return None

    def _resolve(self, pom: _Pom, element: Element) -> tuple[str, str | None, Site | None]:
        raw = pom.text_of(element)
        if not raw:
            return UNRESOLVED, None, None
        match = _PROPERTY.fullmatch(raw)
        if match is None:
            return (COMPLEX, None, None) if "${" in raw else (LITERAL, raw, pom.site(element, "version"))
        name, seen = match.group(1), set()
        while True:
            if name.startswith(_BUILTIN_PREFIXES):
                return BUILTIN, None, None
            if name in seen:
                return UNRESOLVED, None, None
            seen.add(name)
            found = self._property(pom, name)
            if found is None:
                return UNRESOLVED, None, None
            if found is _EXTERNAL_PROPERTY:
                return EXTERNAL, None, None
            holder, holder_element = found
            value = holder.text_of(holder_element)
            if not value:
                return UNRESOLVED, None, None
            nested = _PROPERTY.fullmatch(value)
            if nested:
                name = nested.group(1)
                continue
            if "${" in value:
                return COMPLEX, None, None
            return PROPERTY, value, holder.site(holder_element, "property", name)

    def _management(self, pom: _Pom) -> list[Declaration]:
        """The POM's own dependencyManagement entries (imported BOMs included)."""
        entries = self._elements(pom.root, "dependencyManagement")
        return [d for e in entries if (d := self._declare(pom, "dependencyManagement", None, e)) is not None]

    @staticmethod
    def _elements(scope: Element, section: str) -> list[Element]:
        container = scope.child(section)
        if container is not None and section == "dependencyManagement":
            container = container.child("dependencies")
        return container.all("dependency") if container is not None else []

    def _inherited(self, pom: _Pom, coordinate: Coordinate) -> Declaration | None:
        current: _Pom | None = pom
        while current is not None:
            found = next((d for d in self._management(current) if d.coordinate == coordinate and not d.is_bom), None)
            if found is not None:
                return found
            current = current.parent_pom
        return None

    def _has_external_management(self, pom: _Pom) -> bool:
        current: _Pom | None = pom
        while current is not None:
            if current.external_parent or any(d.is_bom for d in self._management(current)):
                return True
            current = current.parent_pom
        return False

    def _declare(self, pom: _Pom, section: str, profile: str | None, element: Element) -> Declaration | None:
        key = (id(pom), element.start)
        if key in self._made:
            return self._made[key]
        group, artifact = pom.value(element, "groupId"), pom.value(element, "artifactId")
        try:
            if not group or not artifact or "${" in group or "${" in artifact:
                raise ValueError
            coordinate = Coordinate(group, artifact)
        except ValueError:
            self.skipped.append(f"{pom.path}: {section} {group}:{artifact}")
            self._made[key] = None
            return None
        scope = pom.value(element, "scope")
        version_element = element.child("version")
        managed_by = None
        if version_element is not None:
            origin, version, site = self._resolve(pom, version_element)
            raw = pom.text_of(version_element)
        else:
            raw = None
            managed_by = self._inherited(pom, coordinate) if section == "dependencies" else None
            if managed_by is not None:
                origin, version, site = MANAGED, managed_by.version, managed_by.site
            else:
                origin = EXTERNAL if section == "dependencies" and self._has_external_management(pom) else UNRESOLVED
                version, site = None, None
        made = Declaration(coordinate, pom.path, section, profile, scope, scope == "import", raw, version, origin,
                           site, managed_by)  # fmt: skip
        self._made[key] = made
        return made

    def read(self) -> Declarations:
        declared: list[Declaration] = []
        for pom in self.poms:
            scopes = [(None, pom.root)]
            scopes += [(pom.value(p, "id"), p) for p in pom.root.child("profiles").all("profile")] \
                if pom.root.child("profiles") is not None else []  # fmt: skip
            for profile, scope in scopes:
                for section in ("dependencies", "dependencyManagement"):
                    for element in self._elements(scope, section):
                        declaration = self._declare(pom, section, profile, element)
                        if declaration is not None:
                            declared.append(declaration)
        return Declarations(tuple(declared), tuple(self.parents), tuple(self.skipped))


def read_declarations(poms: Sequence[Path]) -> Declarations:
    """Every dependency declaration in the reactor's POMs, in reactor order. Raises PomError."""
    return _Reader(poms).read()
