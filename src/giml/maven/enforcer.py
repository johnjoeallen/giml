"""Structured enforcer violations from Maven's log text (spec sections 3, 9.1 and 8.5).

The enforcer has no structured output, so its report is read from the log, and only from the
``Rule N: <class> failed with message:`` blocks it prints. The parser is tested against real logs
(tests/fixtures/logs) for the three rules giml adds (``DependencyConvergence``,
``BanDuplicateClasses``, ``BanDuplicatePomDependencyVersions``) and keeps any other rule's message.

A violation is identified by its rule and subject, never by the versions involved: a conflict on the
same artifact at other versions is the same violation, so the baseline's violations can be the
reference set that a candidate is compared with (a candidate fails only on a violation outside it).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from giml.maven.failures import normalise
from giml.maven.version import ComparableVersion

_RULE = re.compile(r"^Rule (\d+): (\S+) failed with message:\s*$")
_END = re.compile(r"^(?:-> \[Help \d+\]|To see the full stack trace)")
_CONVERGENCE = re.compile(r"^Dependency convergence error for (\S+)\. Paths to dependency are:")
_PATH_NODE = re.compile(r"^\s*\+-(\S+)")
_POM_DUPLICATE = re.compile(r"^\s*- (\S+)\[(\S+)\] \((\d+) times\)")
_SAMPLE_CLASSES = 5


@dataclass(frozen=True)
class Violation:
    rule: str  # the rule's class name without its package, for example "DependencyConvergence"
    subject: str  # what it objects to: an artifact, a set of artifacts, a declaration, or its message
    detail: dict[str, Any]  # rule-specific and JSON-able: versions and paths, artifacts and classes, times

    @property
    def identity(self) -> str:
        return f"{self.rule}:{self.subject}"

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "subject": self.subject, "detail": self.detail}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Violation:
        return cls(data["rule"], data["subject"], data["detail"])


def _node_version(node: str) -> str:
    """The version in a Maven tree node: group:artifact:type[:classifier]:version[:scope]."""
    parts = node.split(":")
    return parts[4] if len(parts) >= 6 else parts[3] if len(parts) >= 4 else ""


def _convergence(lines: list[str]) -> list[Violation]:
    found: list[Violation] = []
    subject = None
    paths: list[list[str]] = []
    path: list[str] = []

    def close() -> None:
        if subject is not None:
            all_paths = [*paths, path] if path else paths
            versions = sorted({_node_version(p[-1]) for p in all_paths if p}, key=ComparableVersion)
            found.append(Violation("DependencyConvergence", subject, {"versions": versions, "paths": all_paths}))

    for line in lines:
        header = _CONVERGENCE.match(line)
        if header:
            close()
            subject = ":".join(header.group(1).split(":")[:2])
            paths, path = [], []
        elif line.strip() == "and":
            paths.append(path)
            path = []
        elif (node := _PATH_NODE.match(line)) and subject is not None:
            path.append(node.group(1))
    close()
    return found


def _duplicate_classes(lines: list[str]) -> list[Violation]:
    groups: list[tuple[list[str], list[str]]] = []
    mode = None
    for line in lines:
        text = line.strip()
        if text == "Duplicate classes found:":
            groups.append(([], []))
            mode = None
        elif text == "Found in:":
            mode = "artifacts"
        elif text == "Duplicate classes:":
            mode = "classes"
        elif text and groups and mode:
            groups[-1][0 if mode == "artifacts" else 1].append(text)
    found = []
    for artifacts, classes in groups:
        coordinates = sorted({":".join(a.split(":")[:2]) for a in artifacts})
        detail = {"artifacts": sorted(f"{':'.join(a.split(':')[:2])}:{_node_version(a)}" for a in artifacts),
                  "classes": len(classes), "sample": sorted(classes)[:_SAMPLE_CLASSES]}  # fmt: skip
        found.append(Violation("BanDuplicateClasses", " + ".join(coordinates), detail))
    return found


def _duplicate_pom_versions(lines: list[str]) -> list[Violation]:
    found = []
    for line in lines:
        match = _POM_DUPLICATE.match(line)
        if match:
            found.append(Violation("BanDuplicatePomDependencyVersions", match.group(2),
                                   {"declared_in": match.group(1), "times": int(match.group(3))}))  # fmt: skip
    return found


def _other(rule: str, lines: list[str]) -> list[Violation]:
    message = normalise(" ".join(line.strip() for line in lines if line.strip()))[:300]
    return [Violation(rule, message, {"message": message})]


_PARSERS = {"DependencyConvergence": _convergence, "BanDuplicateClasses": _duplicate_classes,
            "BanDuplicatePomDependencyVersions": _duplicate_pom_versions}  # fmt: skip


def _error_lines(log_text: str) -> list[str]:
    lines = []
    for raw in log_text.splitlines():
        if raw.startswith("[ERROR]"):
            line = raw[len("[ERROR]"):]
            lines.append(line[1:] if line.startswith(" ") else line)
    return lines


def parse_enforcer(log_text: str) -> tuple[Violation, ...]:
    """Every violation in a Maven log, deduplicated and in a fixed order (by rule, then subject)."""
    lines = _error_lines(log_text)
    blocks: list[tuple[str, list[str]]] = []
    for line in lines:
        rule = _RULE.match(line)
        if rule:
            blocks.append((rule.group(2).rsplit(".", 1)[-1], []))
        elif _END.match(line):
            blocks.append(("", []))  # closes the current block
        elif blocks:
            blocks[-1][1].append(line)
    found: dict[str, Violation] = {}
    for rule, body in blocks:
        if not rule:
            continue
        for violation in _PARSERS.get(rule, lambda body, rule=rule: _other(rule, body))(body):
            found.setdefault(violation.identity, violation)
    return tuple(sorted(found.values(), key=lambda v: (v.rule, v.subject)))


def new_violations(reference: Iterable[Violation], candidate: Iterable[Violation]) -> tuple[Violation, ...]:
    """The candidate's violations that the reference (the baseline's) does not have."""
    known = {v.identity for v in reference}
    return tuple(v for v in candidate if v.identity not in known)


def resolved_violations(reference: Iterable[Violation], candidate: Iterable[Violation]) -> tuple[Violation, ...]:
    """The reference's violations that the candidate no longer has: the enforcer goals it has met (spec 8.5)."""
    return new_violations(candidate, reference)
