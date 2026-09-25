"""Effective dependency trees from the pinned dependency plugin's JSON output (spec section 3).

Only the plugin's structured output is parsed, never Maven's log text. The plugin coordinate and
version are giml's own (tooling.yaml), never the project's.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from giml.core.model import Coordinate
from giml.gate.setup import tooling
from giml.maven.runner import Env, MavenRunner

TREE_FILE = "giml-tree.json"


class TreeError(ValueError):
    """The plugin's JSON is not a dependency tree giml understands."""


class ResolutionError(RuntimeError):
    """Maven could not resolve the project's dependencies; the message names the log (exit 5)."""


@dataclass(frozen=True)
class Dependency:
    """One node of a module's resolved tree."""

    coordinate: Coordinate
    version: str
    scope: str
    type: str
    classifier: str
    optional: bool
    via: tuple[Coordinate, ...]  # the dependencies between the module and this one; empty when direct

    @property
    def direct(self) -> bool:
        return not self.via


@dataclass(frozen=True)
class ModuleTree:
    coordinate: Coordinate
    version: str
    dependencies: tuple[Dependency, ...]  # Maven's order: a dependency before its own dependencies


def _text(node: dict, key: str, where: str) -> str:
    if key not in node:
        raise TreeError(f"{where}: missing {key}")
    value = node[key]
    if not isinstance(value, str):
        raise TreeError(f"{where}.{key}: expected a string, got {value!r}")
    return value


def _coordinate(node: dict, where: str) -> Coordinate:
    try:
        return Coordinate(_text(node, "groupId", where), _text(node, "artifactId", where))
    except ValueError as exc:
        if isinstance(exc, TreeError):
            raise
        raise TreeError(f"{where}: {exc}") from exc


def _object(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise TreeError(f"{where}: expected an object, got {type(value).__name__}")
    return value


def _flatten(node: dict, via: tuple[Coordinate, ...], where: str, out: list[Dependency]) -> None:
    coordinate = _coordinate(node, where)
    optional = _text(node, "optional", where)
    if optional not in ("true", "false"):
        raise TreeError(f"{where}.optional: expected true or false, got {optional!r}")
    out.append(Dependency(coordinate, _text(node, "version", where), _text(node, "scope", where),
                          _text(node, "type", where), _text(node, "classifier", where),
                          optional == "true", via))  # fmt: skip
    _children(node, via + (coordinate,), where, out)


def _children(node: dict, via: tuple[Coordinate, ...], where: str, out: list[Dependency]) -> None:
    children = node.get("children", [])
    if not isinstance(children, list):
        raise TreeError(f"{where}.children: expected a list, got {children!r}")
    for index, child in enumerate(children):
        _flatten(_object(child, f"{where}.children[{index}]"), via, f"{where}.children[{index}]", out)


def parse_tree(text: str) -> ModuleTree:
    """Parse one module's tree file. Raises TreeError."""
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TreeError(f"not valid JSON: {exc}") from exc
    root = _object(document, "<root>")
    dependencies: list[Dependency] = []
    _children(root, (), "<root>", dependencies)
    return ModuleTree(_coordinate(root, "<root>"), _text(root, "version", "<root>"), tuple(dependencies))


def resolve_reactor(
    project_dir: Path,
    poms: Sequence[Path],
    log_path: Path,
    timeout_seconds: float,
    maven: MavenRunner,
    env: Env,
) -> list[ModuleTree]:
    """Run the pinned plugin once over the reactor and read every module's tree, in ``poms`` order.

    A tree file left over from an earlier run is removed first, so it can never stand in for a
    plugin run that failed or skipped a module.
    """
    files = [pom.parent / "target" / TREE_FILE for pom in poms]
    for file in files:
        file.unlink(missing_ok=True)
    plugin = tooling()["dependency_plugin"]
    result = maven(project_dir, [f"{plugin.gav}:tree", "-DoutputType=json", f"-DoutputFile=target/{TREE_FILE}",
                                 "-Denforcer.skip=true"], log_path, timeout_seconds, env)  # fmt: skip
    if not result.succeeded:
        raise ResolutionError(f"dependency resolution failed; see {result.log_path}")
    trees = []
    for pom, file in zip(poms, files, strict=True):
        if not file.is_file():
            raise ResolutionError(f"no dependency tree written for {pom.parent}; see {result.log_path}")
        try:
            trees.append(parse_tree(file.read_text(encoding="utf-8")))
        except TreeError as exc:
            raise ResolutionError(f"{file}: {exc}") from exc
    return trees
