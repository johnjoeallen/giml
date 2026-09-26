"""Skip candidate versions whose API break the project would hit, before spending a build (spec section 8.2).

japicmp compares the jar of the current version with the jar of a candidate and lists the changes that break
callers: removed classes, methods, fields and constructors, and signature changes. A break only matters if the
project uses it, and giml has no call graph, so "uses" is a deliberately loose textual match on the project's
own sources (the class's simple name and the member's name as whole words). A wrong guess costs a skipped
version, never a wrong result: a skipped step is kept as a fallback and built anyway when nothing else in its
ladder passes (hard rule 8), and japicmp's verdict is never a reason to accept anything.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

_MEMBERS = ("methods", "fields", "constructors")


@dataclass(frozen=True)
class Break:
    class_name: str  # fully qualified
    member: str | None  # None for a class-level break
    change: str  # japicmp's compatibility change, for example METHOD_REMOVED, or the class's change status

    def __str__(self) -> str:
        return f"{self.class_name}{'.' + self.member if self.member else ''} ({self.change})"


def _incompatible(element: ET.Element) -> bool:
    return element.get("binaryCompatible") == "false" or element.get("sourceCompatible") == "false"


def _change_types(element: ET.Element) -> list[str]:
    return [c.get("type", "?") for c in element.findall("compatibilityChanges/compatibilityChange")]


def parse_japicmp(xml_text: str) -> tuple[Break, ...]:
    """The incompatible changes in a japicmp XML report, in report order."""
    root = ET.fromstring(xml_text)
    found: list[Break] = []
    for cls in root.findall("classes/class"):
        name = cls.get("fullyQualifiedName", "?")
        if cls.get("changeStatus") == "REMOVED":
            found.append(Break(name, None, "CLASS_REMOVED"))
            continue
        found += [Break(name, None, change) for change in _change_types(cls)]
        for section in _MEMBERS:
            for member in cls.findall(f"{section}/*"):
                if _incompatible(member):
                    label = member.get("name") or name.rsplit(".", 1)[-1]  # a constructor carries its class's name
                    found += [Break(name, label, change) for change in _change_types(member)] or [Break(name, label, member.get("changeStatus", "?"))]
    return tuple(found)


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"(?<![\w$]){re.escape(word)}(?![\w$])", text) is not None


def _simple_name(class_name: str) -> str:
    """The name sources use for a class: a nested class is reached through its outer one, but a name that starts with
    ``$`` (Gson's generated-style internal classes) is the whole segment, never an empty string that matches everything."""
    segment = class_name.rsplit(".", 1)[-1]
    return segment if segment.startswith("$") else segment.split("$")[0]


def used_breaks(breaks: Iterable[Break], sources: Iterable[str]) -> tuple[Break, ...]:
    """The breaks the sources appear to hit: a class-level one when the class is named, a member one when the class and member are."""
    texts = list(sources)
    hits = []
    for found in breaks:
        simple = _simple_name(found.class_name)
        if any(_has_word(text, simple) and (found.member is None or _has_word(text, found.member)) for text in texts):
            hits.append(found)
    return tuple(hits)


def project_sources(project_dir: Path) -> list[str]:
    """The text of every main and test Java source under the project (unreadable files are skipped, they cannot be used)."""
    texts = []
    for path in sorted(project_dir.rglob("*.java")):
        if "target" in path.relative_to(project_dir).parts:
            continue
        try:
            texts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return texts


class ApiChecker:
    """Answers "does moving from one version to another break something this project uses?" from two jars.

    ``jar`` fetches a dependency version's jar and ``compare`` runs japicmp on two jars and returns its XML;
    both are injected, so the logic is testable without a JDK or a network. Answers are memoised.
    """

    def __init__(self, sources: list[str], jar: Callable[[str, str], Path | None], compare: Callable[[Path, Path], str | None]) -> None:
        self.sources, self.jar, self.compare = sources, jar, compare
        self._memo: dict[tuple[str, str, str], tuple[str, ...]] = {}

    def check(self, coordinate: str, old: str, new: str) -> tuple[str, ...]:
        """The used breaks as text, or () when there are none or the comparison could not be made (then nothing is skipped)."""
        key = (coordinate, old, new)
        if key not in self._memo:
            old_jar, new_jar = self.jar(coordinate, old), self.jar(coordinate, new)
            report = self.compare(old_jar, new_jar) if old_jar and new_jar else None
            self._memo[key] = tuple(str(b) for b in used_breaks(parse_japicmp(report), self.sources)) if report else ()
        return self._memo[key]


class JapicmpTools:
    """Fetches dependency jars and giml's pinned japicmp with Maven, runs japicmp, and caches jars in ``<state>/tools``.

    Any failure (no network, no such jar, japicmp crashing) answers None: no evidence, so nothing is skipped.
    """

    def __init__(self, tools_dir: Path, project_dir: Path, maven, java, env, logs: Path, timeout: float) -> None:
        self.tools_dir, self.project_dir, self.maven, self.java = tools_dir, project_dir, maven, java
        self.env, self.logs, self.timeout = env, logs, timeout
        self._runs = 0

    def _copy(self, artifact: str, directory: Path, name: str) -> bool:
        from giml.gate.setup import tooling

        self._runs += 1
        result = self.maven(self.project_dir, [f"{tooling()['dependency_plugin'].gav}:copy", f"-Dartifact={artifact}",
                                               f"-DoutputDirectory={directory}"],
                            self.logs / f"api-{self._runs:03d}-{name}.log", self.timeout, self.env)  # fmt: skip
        return result.succeeded

    def _japicmp(self) -> Path | None:
        from giml.gate.setup import tooling

        tool = tooling()["japicmp"]
        jar = self.tools_dir / f"{tool.artifact_id}-{tool.version}-{tool.classifier}.jar"
        if not jar.is_file():
            self.tools_dir.mkdir(parents=True, exist_ok=True)
            self._copy(tool.gav, self.tools_dir, "japicmp")
        return jar if jar.is_file() else None

    def jar(self, coordinate: str, version: str) -> Path | None:
        group, artifact = coordinate.split(":")
        directory = self.tools_dir / "jars" / group / artifact / version
        found = directory / f"{artifact}-{version}.jar"
        if not found.is_file():
            directory.mkdir(parents=True, exist_ok=True)
            self._copy(f"{coordinate}:{version}:jar", directory, f"{artifact}-{version}")
        return found if found.is_file() else None

    def compare(self, old: Path, new: Path) -> str | None:
        tool = self._japicmp()
        if tool is None:
            return None
        out = self.tools_dir / "reports" / f"{old.stem}-to-{new.stem}.xml"
        out.parent.mkdir(parents=True, exist_ok=True)
        result = self.java(["-jar", str(tool), "--old", str(old), "--new", str(new), "--only-incompatible", "--xml-file", str(out)],
                           self.timeout, self.env)  # fmt: skip
        return out.read_text(encoding="utf-8") if result.returncode == 0 and out.is_file() else None
