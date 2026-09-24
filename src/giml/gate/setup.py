"""Add missing quality tooling in giml's worktree (spec section 3).

Developers do not have to configure JaCoCo, PIT or the enforcer bans before using giml. Where a
reactor lacks them, giml adds them to the reactor's inheritance roots with its own pinned
versions, as a separate "[giml-setup]" commit on the result branch that the developer can take.
Tooling the project already declares is kept and used as-is.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from pathlib import Path

import yaml

from giml.git.worktrees import commit_all
from giml.maven import pom_edit, pom_xml

ENFORCER_EXECUTION_ID = "giml-bans"
BANS = ("banDuplicateClasses", "banDuplicatePomDependencyVersions", "dependencyConvergence")


@dataclass(frozen=True)
class Plugin:
    group_id: str
    artifact_id: str
    version: str

    @property
    def key(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"


@cache
def tooling() -> dict[str, Plugin]:
    data = yaml.safe_load((resources.files("giml.gate") / "tooling.yaml").read_text("utf-8"))
    return {name: Plugin(v["groupId"], v["artifactId"], str(v["version"])) for name, v in data.items()}


@dataclass
class SetupResult:
    changed_files: list[str] = field(default_factory=list)  # relative to the worktree
    added: list[str] = field(default_factory=list)  # human-readable, one per addition
    kept: list[str] = field(default_factory=list)  # tooling the project already declares
    commit: str | None = None


# ---------------------------------------------------------------------------------------------
# Reactor facts


def _coordinates(pom: Path) -> tuple[str | None, str | None, tuple[str | None, str | None]]:
    root = pom_xml.parse_project(pom)
    parent = pom_xml.child(root, "parent")
    parent_ga = (pom_xml.text(parent, "groupId"), pom_xml.text(parent, "artifactId"))
    return pom_xml.text(root, "groupId") or parent_ga[0], pom_xml.text(root, "artifactId"), parent_ga


def inheritance_roots(reactor: list[Path]) -> list[Path]:
    """Reactor POMs whose parent is not itself in the reactor: tooling added there reaches every
    module through inheritance."""
    facts = {pom: _coordinates(pom) for pom in reactor}
    in_reactor = {(group, artifact) for group, artifact, _ in facts.values()}
    return [pom for pom, (_, _, parent) in facts.items() if parent not in in_reactor]


def _java_files(module_dir: Path, source_root: str) -> list[Path]:
    base = module_dir / source_root
    return sorted(base.rglob("*.java")) if base.is_dir() else []


def uses_junit5(reactor: list[Path]) -> bool:
    for pom in reactor:
        for source in _java_files(pom.parent, "src/test/java"):
            if "org.junit.jupiter" in source.read_text(encoding="utf-8", errors="replace"):
                return True
    return False


def target_classes(reactor: list[Path]) -> list[str]:
    """PIT targetClasses: the longest common package of each module's main sources."""
    patterns: set[str] = set()
    for pom in reactor:
        base = pom.parent / "src/main/java"
        packages = [list(f.parent.relative_to(base).parts) for f in _java_files(pom.parent, "src/main/java")]
        if not packages:
            continue
        common = os.path.commonprefix(packages)  # element-wise on lists of package parts
        patterns.add(".".join(common) + ".*" if common else "*")
    if "*" in patterns:
        return ["*"]
    # Drop patterns already covered by a broader one (com.a.* covers com.a.b.*).
    return sorted(p for p in patterns if not any(q != p and p.startswith(q[:-1]) for q in patterns))


# ---------------------------------------------------------------------------------------------
# Blocks (one tab per nesting level; converted to the POM's own indentation)


def _jacoco_block(p: Plugin) -> str:
    return f"""<plugin>
\t<groupId>{p.group_id}</groupId>
\t<artifactId>{p.artifact_id}</artifactId>
\t<version>{p.version}</version>
\t<executions>
\t\t<execution>
\t\t\t<id>giml-prepare-agent</id>
\t\t\t<goals>
\t\t\t\t<goal>prepare-agent</goal>
\t\t\t</goals>
\t\t</execution>
\t\t<execution>
\t\t\t<id>giml-report</id>
\t\t\t<phase>test</phase>
\t\t\t<goals>
\t\t\t\t<goal>report</goal>
\t\t\t</goals>
\t\t</execution>
\t</executions>
</plugin>"""


def _pitest_block(p: Plugin, targets: list[str], junit5: Plugin | None) -> str:
    params = "\n".join(f"\t\t\t<param>{t}</param>" for t in targets)
    dependencies = ""
    if junit5 is not None:
        dependencies = f"""
\t<dependencies>
\t\t<dependency>
\t\t\t<groupId>{junit5.group_id}</groupId>
\t\t\t<artifactId>{junit5.artifact_id}</artifactId>
\t\t\t<version>{junit5.version}</version>
\t\t</dependency>
\t</dependencies>"""
    return f"""<plugin>
\t<groupId>{p.group_id}</groupId>
\t<artifactId>{p.artifact_id}</artifactId>
\t<version>{p.version}</version>
\t<configuration>
\t\t<targetClasses>
{params}
\t\t</targetClasses>
\t\t<outputFormats>
\t\t\t<outputFormat>XML</outputFormat>
\t\t\t<outputFormat>HTML</outputFormat>
\t\t</outputFormats>
\t\t<timestampedReports>false</timestampedReports>
\t</configuration>{dependencies}
</plugin>"""


def _enforcer_execution() -> str:
    return f"""<execution>
\t<id>{ENFORCER_EXECUTION_ID}</id>
\t<goals>
\t\t<goal>enforce</goal>
\t</goals>
\t<configuration>
\t\t<rules>
\t\t\t<banDuplicateClasses>
\t\t\t\t<findAllDuplicates>true</findAllDuplicates>
\t\t\t</banDuplicateClasses>
\t\t\t<banDuplicatePomDependencyVersions/>
\t\t\t<dependencyConvergence/>
\t\t</rules>
\t</configuration>
</execution>"""


def _dependency_block(p: Plugin) -> str:
    return f"""<dependency>
\t<groupId>{p.group_id}</groupId>
\t<artifactId>{p.artifact_id}</artifactId>
\t<version>{p.version}</version>
</dependency>"""


# ---------------------------------------------------------------------------------------------
# Editing one inheritance root


def _declared_in_plugins(text: str, root: pom_edit.Element, plugin: Plugin) -> pom_edit.Element | None:
    build = root.child("build")
    plugins = build.child("plugins") if build is not None else None
    if plugins is None:
        return None
    for element in plugins.all("plugin"):
        group = pom_edit.text_of(text, element.child("groupId")) or "org.apache.maven.plugins"
        if (group, pom_edit.text_of(text, element.child("artifactId"))) == (plugin.group_id, plugin.artifact_id):
            return element
    return None


def _append_plugin(text: str, block: str) -> str:
    text, _ = pom_edit.ensure_path(text, ["build", "plugins"])
    root = pom_edit.parse(text)
    return pom_edit.append_child(text, root, pom_edit.find(root, ["build", "plugins"]), block)


def _append_in_plugin(text: str, plugin: Plugin, container: str, block: str) -> str:
    """Append ``block`` inside <container> of the plugin's build/plugins declaration."""
    root = pom_edit.parse(text)
    element = _declared_in_plugins(text, root, plugin)
    if element.child(container) is None:
        text = pom_edit.append_child(text, root, element, f"<{container}>\n</{container}>")
        root = pom_edit.parse(text)
        element = _declared_in_plugins(text, root, plugin)
    return pom_edit.append_child(text, root, element.child(container), block)


def _has_enforcer_bans(text: str, element: pom_edit.Element) -> bool:
    body = text[element.start : element.end]
    return all(f"<{ban}" in body for ban in BANS)


def setup_root(text: str, reactor: list[Path], label: str, result: SetupResult) -> str:
    tools = tooling()
    root = pom_edit.parse(text)

    jacoco = tools["jacoco"]
    if _declared_in_plugins(text, root, jacoco) is not None:
        result.kept.append(f"{label}: {jacoco.key} (project's own configuration)")
    else:
        text = _append_plugin(text, _jacoco_block(jacoco))
        result.added.append(f"{label}: {jacoco.key}:{jacoco.version} (prepare-agent, report)")

    pitest = tools["pitest"]
    if pom_edit.find_plugin(text, pom_edit.parse(text), pitest.group_id, pitest.artifact_id) is not None:
        result.kept.append(f"{label}: {pitest.key} (project's own configuration)")
    else:
        junit5 = tools["pitest_junit5"] if uses_junit5(reactor) else None
        text = _append_plugin(text, _pitest_block(pitest, target_classes(reactor), junit5))
        extra = f" with {junit5.artifact_id}:{junit5.version}" if junit5 else ""
        result.added.append(f"{label}: {pitest.key}:{pitest.version}{extra} (not bound; invoked by giml)")

    enforcer, rules = tools["enforcer"], tools["extra_enforcer_rules"]
    root = pom_edit.parse(text)
    declared = _declared_in_plugins(text, root, enforcer)
    if declared is not None and _has_enforcer_bans(text, declared):
        result.kept.append(f"{label}: {enforcer.key} (project already bans duplicates and checks convergence)")
        return text
    if declared is None:
        managed = pom_edit.find_plugin(text, root, enforcer.group_id, enforcer.artifact_id) is not None
        version = "" if managed else f"\n\t<version>{enforcer.version}</version>"
        text = _append_plugin(text, f"<plugin>\n\t<artifactId>{enforcer.artifact_id}</artifactId>{version}\n</plugin>")
    text = _append_in_plugin(text, enforcer, "executions", _enforcer_execution())
    if f"<artifactId>{rules.artifact_id}</artifactId>" not in text:
        text = _append_in_plugin(text, enforcer, "dependencies", _dependency_block(rules))
    result.added.append(f"{label}: {enforcer.key} execution '{ENFORCER_EXECUTION_ID}' ({', '.join(BANS)})")
    return text


def apply_setup(worktree: Path, reactor: list[Path]) -> SetupResult:
    """Add missing tooling to the worktree's reactor and commit it; ``reactor`` lists the
    worktree's own pom.xml paths. Commits nothing if every tool is already present."""
    result = SetupResult()
    for pom in inheritance_roots(reactor):
        label = os.path.relpath(pom, worktree)
        original = pom.read_bytes().decode("utf-8")  # bytes, so CRLF line endings survive
        edited = setup_root(original, reactor, label, result)
        if edited != original:
            pom.write_bytes(edited.encode("utf-8"))
            result.changed_files.append(label)
    if result.changed_files:
        lines = "\n".join(f"- {entry}" for entry in result.added)
        message = (
            "[giml-setup] add quality tooling for assessment\n\n"
            f"giml added the quality tooling this project did not declare yet:\n{lines}\n\n"
            "Versions are pinned by giml. Cherry-pick this commit to keep the tooling in your\n"
            "own branch; it is separate from any upgrade commits."
        )
        result.commit = commit_all(worktree, message)
    return result
