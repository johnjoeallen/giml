from pathlib import Path

import pytest

from giml.core.model import Coordinate
from giml.maven.declarations import (
    BUILTIN, COMPLEX, EXTERNAL, LITERAL, MANAGED, PROPERTY, UNRESOLVED, read_declarations,
)  # fmt: skip
from giml.maven.pom_xml import PomError
from giml.maven.project import discover_reactor

REDKITE = Path(__file__).resolve().parents[1] / "fixtures" / "poms" / "redkite"


def c(text: str) -> Coordinate:
    return Coordinate.parse(text)


def dep(group: str, artifact: str, version: str | None = None, scope: str | None = None, kind: str | None = None) -> str:
    parts = [f"<groupId>{group}</groupId>", f"<artifactId>{artifact}</artifactId>"]
    if version is not None:
        parts.append(f"<version>{version}</version>")
    if scope:
        parts.append(f"<scope>{scope}</scope>")
    if kind:
        parts.append(f"<type>{kind}</type>")
    return "<dependency>" + "".join(parts) + "</dependency>"


def pom(artifact="m", parent: str | None = None, deps=(), managed=(), props: dict | None = None, extra="", group="g",
        version="1.0") -> str:  # fmt: skip
    header = f"<parent>{parent}</parent>" if parent else f"<groupId>{group}</groupId><version>{version}</version>"
    properties = "<properties>" + "".join(f"<{k}>{v}</{k}>" for k, v in (props or {}).items()) + "</properties>" if props else ""
    management = f"<dependencyManagement><dependencies>{''.join(managed)}</dependencies></dependencyManagement>" if managed else ""
    dependencies = f"<dependencies>{''.join(deps)}</dependencies>" if deps else ""
    return (f'<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>{header}'
            f"<artifactId>{artifact}</artifactId>{properties}{management}{dependencies}{extra}</project>")  # fmt: skip


def parent_ref(group="g", artifact="root", version="1.0") -> str:
    return f"<groupId>{group}</groupId><artifactId>{artifact}</artifactId><version>{version}</version>"


def write(tmp_path: Path, files: dict[str, str]) -> list[Path]:
    paths = []
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    return paths


def one(declarations, coordinate: str, pom_path: Path | None = None):
    found = [d for d in declarations.for_coordinate(c(coordinate)) if pom_path is None or d.pom == pom_path]
    assert len(found) == 1, found
    return found[0]


def test_literal_version_and_its_editable_span(tmp_path):
    (path,) = write(tmp_path, {"pom.xml": pom(deps=[dep("org.x", "lib", "1.2.3")])})
    declaration = one(read_declarations([path]), "org.x:lib")
    assert (declaration.origin, declaration.version, declaration.raw) == (LITERAL, "1.2.3", "1.2.3")
    assert (declaration.section, declaration.profile, declaration.scope, declaration.is_bom) == (
        "dependencies", None, None, False)  # fmt: skip
    site = declaration.site
    assert site.pom == path and site.kind == "version" and site.name is None
    assert path.read_text()[slice(*site.span)] == "1.2.3"
    assert site.line == 1


def test_site_line_counts_from_one_across_lines(tmp_path):
    text = "<project>\n  <artifactId>m</artifactId>\n  <dependencies>\n    <dependency>\n      <groupId>o</groupId>\n" \
           "      <artifactId>l</artifactId>\n      <version>9</version>\n    </dependency>\n  </dependencies>\n</project>\n"  # fmt: skip
    (path,) = write(tmp_path, {"pom.xml": text})
    assert one(read_declarations([path]), "o:l").site.line == 7


def test_property_in_the_same_pom(tmp_path):
    (path,) = write(tmp_path, {"pom.xml": pom(deps=[dep("o", "l", "${l.version}")], props={"l.version": "4.5"})})
    declaration = one(read_declarations([path]), "o:l")
    assert (declaration.origin, declaration.version, declaration.raw) == (PROPERTY, "4.5", "${l.version}")
    assert (declaration.site.kind, declaration.site.name) == ("property", "l.version")
    assert path.read_text()[slice(*declaration.site.span)] == "4.5"


def test_chained_properties_edit_the_one_holding_the_literal(tmp_path):
    (path,) = write(tmp_path, {"pom.xml": pom(deps=[dep("o", "l", "${a}")], props={"a": "${b}", "b": "7.1"})})
    declaration = one(read_declarations([path]), "o:l")
    assert (declaration.origin, declaration.version, declaration.site.name) == (PROPERTY, "7.1", "b")


def test_property_defined_in_the_reactor_parent(tmp_path):
    paths = write(tmp_path, {
        "pom.xml": pom("root", props={"l.version": "2.0"}, deps=[]),
        "core/pom.xml": pom("core", parent=parent_ref(), deps=[dep("o", "l", "${l.version}")]),
    })  # fmt: skip
    declaration = one(read_declarations(paths), "o:l")
    assert (declaration.origin, declaration.version, declaration.pom) == (PROPERTY, "2.0", paths[1])
    assert declaration.site.pom == paths[0]


def test_child_property_overrides_the_parents(tmp_path):
    paths = write(tmp_path, {
        "pom.xml": pom("root", props={"v": "1"}),
        "core/pom.xml": pom("core", parent=parent_ref(), props={"v": "2"}, deps=[dep("o", "l", "${v}")]),
    })  # fmt: skip
    declaration = one(read_declarations(paths), "o:l")
    assert (declaration.version, declaration.site.pom) == ("2", paths[1])


def test_version_from_the_reactor_parents_dependency_management(tmp_path):
    paths = write(tmp_path, {
        "pom.xml": pom("root", props={"l.version": "3.3"}, managed=[dep("o", "l", "${l.version}")]),
        "core/pom.xml": pom("core", parent=parent_ref(), deps=[dep("o", "l")]),
    })  # fmt: skip
    declarations = read_declarations(paths)
    managing, inheriting = one(declarations, "o:l", paths[0]), one(declarations, "o:l", paths[1])
    assert (managing.section, managing.origin, managing.version) == ("dependencyManagement", PROPERTY, "3.3")
    assert (inheriting.origin, inheriting.version, inheriting.managed_by) == (MANAGED, "3.3", managing)
    assert inheriting.site == managing.site and inheriting.site.pom == paths[0]


def test_own_version_beats_management(tmp_path):
    paths = write(tmp_path, {
        "pom.xml": pom("root", managed=[dep("o", "l", "1.0")]),
        "core/pom.xml": pom("core", parent=parent_ref(), deps=[dep("o", "l", "2.0")]),
    })  # fmt: skip
    inheriting = one(read_declarations(paths), "o:l", paths[1])
    assert (inheriting.origin, inheriting.version, inheriting.managed_by) == (LITERAL, "2.0", None)


def test_nearest_management_wins_up_the_chain(tmp_path):
    paths = write(tmp_path, {
        "pom.xml": pom("root", managed=[dep("o", "l", "1.0")]),
        "mid/pom.xml": pom("mid", parent=parent_ref(), managed=[dep("o", "l", "2.0")]),
        "mid/leaf/pom.xml": pom("leaf", parent=parent_ref(artifact="mid"), deps=[dep("o", "l")]),
    })  # fmt: skip
    assert one(read_declarations(paths), "o:l", paths[2]).version == "2.0"


def test_external_parent_is_recorded_and_versionless_dependencies_are_external(tmp_path):
    parent = parent_ref("org.springframework.boot", "spring-boot-starter-parent", "3.5.0")
    (path,) = write(tmp_path, {"pom.xml": pom("app", parent=parent, deps=[dep("o", "web"), dep("o", "p", "${web.v}")])})
    declarations = read_declarations([path])
    (external,) = declarations.parents
    assert (external.pom, external.coordinate, external.version) == (
        path, c("org.springframework.boot:spring-boot-starter-parent"), "3.5.0")  # fmt: skip
    assert path.read_text()[slice(*external.site.span)] == "3.5.0" and external.site.kind == "version"
    assert (one(declarations, "o:web").origin, one(declarations, "o:web").version) == (EXTERNAL, None)
    assert one(declarations, "o:p").origin == EXTERNAL  # the property may come from the external parent


def test_reactor_parents_are_not_external_parents(tmp_path):
    paths = write(tmp_path, {"pom.xml": pom("root"), "core/pom.xml": pom("core", parent=parent_ref())})
    assert read_declarations(paths).parents == ()


def test_parent_group_is_inherited_when_matching_the_reactor(tmp_path):
    paths = write(tmp_path, {
        "pom.xml": pom("root", group="com.acme"),
        "core/pom.xml": pom("core", parent=parent_ref("com.acme", "root"), deps=[dep("o", "l", "1")]),
        "app/pom.xml": pom("app", parent=parent_ref("com.other", "root"), deps=[dep("o", "l", "1")]),
    })  # fmt: skip
    # com.other:root is not the reactor's root, so `app` has an external parent.
    assert [str(p.coordinate) for p in read_declarations(paths).parents] == ["com.other:root"]


def test_bom_import_and_dependencies_it_manages(tmp_path):
    bom = dep("org.spring", "bom", "6.0", scope="import", kind="pom")
    (path,) = write(tmp_path, {"pom.xml": pom("app", managed=[bom], deps=[dep("org.spring", "core")])})
    declarations = read_declarations([path])
    imported = one(declarations, "org.spring:bom")
    assert (imported.is_bom, imported.scope, imported.origin, imported.version) == (True, "import", LITERAL, "6.0")
    assert one(declarations, "org.spring:core").origin == EXTERNAL


def test_unresolvable_versions(tmp_path):
    (path,) = write(tmp_path, {"pom.xml": pom(deps=[
        dep("o", "none"), dep("o", "missing", "${nowhere}"), dep("o", "self", "${project.version}"),
        dep("o", "mixed", "${a}.${b}"), dep("o", "loop", "${x}"),
    ], props={"x": "${y}", "y": "${x}"})})  # fmt: skip
    declarations = read_declarations([path])
    assert one(declarations, "o:none").origin == UNRESOLVED
    assert one(declarations, "o:missing").origin == UNRESOLVED
    assert one(declarations, "o:self").origin == BUILTIN and one(declarations, "o:self").site is None
    assert one(declarations, "o:mixed").origin == COMPLEX and one(declarations, "o:mixed").site is None
    assert one(declarations, "o:loop").origin == UNRESOLVED
    assert all(d.version is None for d in declarations.declared if d.origin != LITERAL)


def test_profile_declarations_are_flagged_with_their_profile(tmp_path):
    profile = "<profiles><profile><id>fast</id><dependencies>" + dep("o", "p", "5") + "</dependencies></profile></profiles>"
    (path,) = write(tmp_path, {"pom.xml": pom(extra=profile, deps=[dep("o", "top", "1")])})
    declarations = read_declarations([path])
    assert one(declarations, "o:p").profile == "fast" and one(declarations, "o:top").profile is None


def test_coordinates_that_cannot_be_read_are_skipped_and_named(tmp_path):
    (path,) = write(tmp_path, {"pom.xml": pom(deps=[dep("${g}", "a", "1"), dep("o", "b", "1")])})
    declarations = read_declarations([path])
    assert [str(d.coordinate) for d in declarations.declared] == ["o:b"]
    assert len(declarations.skipped) == 1 and "${g}:a" in declarations.skipped[0] and str(path) in declarations.skipped[0]


def test_one_coordinate_declared_in_several_modules(tmp_path):
    paths = write(tmp_path, {
        "pom.xml": pom("root"),
        "a/pom.xml": pom("a", parent=parent_ref(), deps=[dep("o", "l", "1")]),
        "b/pom.xml": pom("b", parent=parent_ref(), deps=[dep("o", "l", "2")]),
    })  # fmt: skip
    assert [(d.pom, d.version) for d in read_declarations(paths).for_coordinate(c("o:l"))] == [
        (paths[1], "1"), (paths[2], "2")]  # fmt: skip
    assert read_declarations(paths).for_coordinate(c("o:unknown")) == []


def test_comments_crlf_and_plain_documents(tmp_path):
    text = ('<project>\r\n<!-- <dependency><groupId>x</groupId><artifactId>y</artifactId><version>0</version></dependency> -->\r\n'
            "<artifactId>m</artifactId><dependencies><dependency><groupId>o</groupId><artifactId>l</artifactId>"
            "<version>1</version></dependency></dependencies></project>")  # fmt: skip
    (path,) = write(tmp_path, {"pom.xml": text})
    declarations = read_declarations([path])
    assert [str(d.coordinate) for d in declarations.declared] == ["o:l"]
    assert declarations.declared[0].site.line == 3


def test_a_malformed_or_missing_pom_names_the_file(tmp_path):
    (bad,) = write(tmp_path, {"pom.xml": "<project><dependencies></project>"})
    with pytest.raises(PomError, match=r"pom\.xml: .*never closed|pom\.xml: .*unexpected"):
        read_declarations([bad])
    with pytest.raises(PomError, match=r"absent\.xml: cannot read"):
        read_declarations([tmp_path / "absent.xml"])


def test_real_redkite_poms():
    poms = discover_reactor(REDKITE)
    assert [p.parent.name for p in poms] == ["redkite", "red-kite-core", "red-kite-maven", "red-kite-metadata", "red-kite-server"]
    declarations = read_declarations(poms)
    assert declarations.parents == () and declarations.skipped == ()
    # Everything is editable except the modules' ${project.version} references to each other.
    assert {str(d.coordinate) for d in declarations.declared if d.origin == BUILTIN} == {
        "com.redkite:red-kite-core", "com.redkite:red-kite-maven", "com.redkite:red-kite-metadata"}  # fmt: skip
    assert all(d.origin in (LITERAL, PROPERTY) and d.site is not None for d in declarations.declared
               if d.origin != BUILTIN)  # fmt: skip
    gson = one(declarations, "com.google.code.gson:gson")
    assert (gson.origin, gson.version, gson.site.name, gson.site.pom.parent.name) == (
        PROPERTY, "2.14.0", "gson.version", "red-kite-metadata")  # fmt: skip
    assert one(declarations, "org.yaml:snakeyaml").version == "2.7"
    pin = one(declarations, "org.springframework:spring-core")
    assert (pin.section, pin.origin, pin.version) == ("dependencyManagement", LITERAL, "6.2.19")
    assert pin.site.pom == poms[0]
