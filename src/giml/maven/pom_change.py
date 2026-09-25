"""Apply dependency changes to pom.xml files as lossless text edits (spec section 8.4).

Three kinds of change, and nothing else: a version edited where it is actually declared, a
``dependencyManagement`` pin, and (only when a project allows it) an exclusion. The documents are
never re-serialised: a version edit splices new text over the exact span the declaration reader
found, and a pin or exclusion is inserted with the document's own indentation and line endings, so
comments, whitespace and CRLF line endings everywhere else stay byte for byte identical.

A set of changes is applied all or nothing: every file is computed first and only written when all
of them succeeded.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from xml.sax.saxutils import escape

from giml.core.model import Coordinate
from giml.maven import pom_edit
from giml.maven.declarations import Site


class ChangeError(ValueError):
    """A change cannot be applied as asked; nothing has been written."""


@dataclass(frozen=True)
class SetVersion:
    site: Site  # where the version text lives: a literal, a property or a managing entry
    expected: str  # what the file must hold there now; guards against a stale site
    version: str


@dataclass(frozen=True)
class AddPin:
    """Manage ``coordinate`` at ``version`` in ``pom`` (the reactor's root, normally)."""

    pom: Path
    coordinate: Coordinate
    version: str
    note: str | None = None  # written as a comment above the entry, so a reader knows why it is there


@dataclass(frozen=True)
class AddExclusion:
    """Exclude ``excluded`` from the dependency ``holder`` declared in ``pom``."""

    pom: Path
    holder: Coordinate
    excluded: Coordinate


Change = SetVersion | AddPin | AddExclusion


def _read(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def _write(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _set_versions(text: str, edits: list[SetVersion]) -> str:
    """Replace each version's text, last position first so earlier spans stay valid."""
    ordered = sorted(edits, key=lambda e: e.site.span[0], reverse=True)
    for later, earlier in zip(ordered, ordered[1:], strict=False):
        if earlier.site.span[1] > later.site.span[0]:
            raise ChangeError(f"{later.site.pom}:{later.site.line}: more than one change to the same version")
    for edit in ordered:
        start, end = edit.site.span
        if text[start:end] != edit.expected:
            raise ChangeError(f"{edit.site.pom}:{edit.site.line}: expected {edit.expected} but the file has {text[start:end]}")
        text = text[:start] + edit.version + text[end:]
    return text


def _matches(text: str, dependency: pom_edit.Element, coordinate: Coordinate) -> bool:
    return (pom_edit.text_of(text, dependency.child("groupId")), pom_edit.text_of(text, dependency.child("artifactId"))) == (
        coordinate.group_id, coordinate.artifact_id)  # fmt: skip


def _dependency_block(coordinate: Coordinate, version: str, note: str | None) -> str:
    comment = f"<!-- giml: {' '.join(note.replace('--', '-').split())} -->\n" if note else ""
    return (f"{comment}<dependency>\n\t<groupId>{escape(coordinate.group_id)}</groupId>\n"
            f"\t<artifactId>{escape(coordinate.artifact_id)}</artifactId>\n\t<version>{escape(version)}</version>\n</dependency>")  # fmt: skip


def _add_pin(text: str, change: AddPin) -> str:
    root = pom_edit.parse(text)
    management = root.child("dependencyManagement")
    existing = management.child("dependencies") if management is not None else None
    if existing is not None and any(_matches(text, d, change.coordinate) for d in existing.all("dependency")):
        raise ChangeError(f"{change.coordinate} is already managed in {change.pom}; change its version instead")
    text, _ = pom_edit.ensure_path(text, ["dependencyManagement", "dependencies"])
    root = pom_edit.parse(text)
    dependencies = pom_edit.find(root, ["dependencyManagement", "dependencies"])
    return pom_edit.append_child(text, root, dependencies, _dependency_block(change.coordinate, change.version, change.note))


def _add_exclusion(text: str, change: AddExclusion) -> str:
    root = pom_edit.parse(text)
    dependencies = root.child("dependencies")
    holder = next((d for d in dependencies.all("dependency") if _matches(text, d, change.holder)), None) if dependencies else None
    if holder is None:
        raise ChangeError(f"{change.holder} is not a dependency declared in {change.pom}")
    exclusion = ("<exclusion>\n\t<groupId>{}</groupId>\n\t<artifactId>{}</artifactId>\n</exclusion>"
                 .format(escape(change.excluded.group_id), escape(change.excluded.artifact_id)))  # fmt: skip
    exclusions = holder.child("exclusions")
    if exclusions is None:
        return pom_edit.append_child(text, root, holder, "<exclusions>\n" + "\n".join("\t" + line for line in exclusion.split("\n")) + "\n</exclusions>")
    if any(_matches(text, e, change.excluded) for e in exclusions.all("exclusion")):
        return text
    return pom_edit.append_child(text, root, exclusions, exclusion)


def apply_changes(changes: Sequence[Change]) -> None:
    """Apply the changes to the files they name, or none of them if any cannot be applied."""
    texts: dict[Path, str] = {}

    def current(path: Path) -> str:
        if path not in texts:
            texts[path] = _read(path)
        return texts[path]

    edits: dict[Path, list[SetVersion]] = {}
    for change in changes:
        if isinstance(change, SetVersion):
            edits.setdefault(change.site.pom, []).append(change)
    for pom, versions in edits.items():
        texts[pom] = _set_versions(current(pom), versions)
    for change in changes:
        if isinstance(change, AddPin):
            texts[change.pom] = _add_pin(current(change.pom), change)
        elif isinstance(change, AddExclusion):
            texts[change.pom] = _add_exclusion(current(change.pom), change)
    for path, text in texts.items():
        if text != _read(path):
            _write(path, text)


def rebase(change: Change, old_root: Path, new_root: Path) -> Change:
    """The same change for another worktree of the same commit: paths move from ``old_root`` to ``new_root``."""

    def moved(path: Path) -> Path:
        if not path.is_relative_to(old_root):
            raise ChangeError(f"{path} is not inside {old_root}")
        return new_root / path.relative_to(old_root)

    if isinstance(change, SetVersion):
        return replace(change, site=replace(change.site, pom=moved(change.site.pom)))
    return replace(change, pom=moved(change.pom))


def describe(change: Change, root: Path) -> str:
    """One line saying what a change does, with paths relative to ``root`` (for commit messages and reports)."""

    def relative(path: Path) -> str:
        return str(path.relative_to(root)) if path.is_relative_to(root) else str(path)

    if isinstance(change, SetVersion):
        what = f"property {change.site.name}" if change.site.kind == "property" else "version"
        return f"set {what} {change.expected} → {change.version} ({relative(change.site.pom)}:{change.site.line})"
    if isinstance(change, AddPin):
        return f"pin {change.coordinate} at {change.version} in {relative(change.pom)}"
    return f"exclude {change.excluded} from {change.holder} in {relative(change.pom)}"
