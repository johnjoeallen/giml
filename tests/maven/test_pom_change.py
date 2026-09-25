from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from giml.core.model import Coordinate
from giml.maven.declarations import LITERAL, PROPERTY, read_declarations
from giml.maven.pom_change import AddExclusion, AddPin, ChangeError, SetVersion, apply_changes, describe
from giml.maven.project import discover_reactor

REDKITE = Path(__file__).resolve().parents[1] / "fixtures" / "poms" / "redkite"


def c(text: str) -> Coordinate:
    return Coordinate.parse(text)


POM = """<?xml version="1.0" encoding="UTF-8"?>
<!-- keep me -->
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <groupId>g</groupId>
    <artifactId>app</artifactId>
    <version>1</version>
    <properties>
        <lib.version>2.17.1</lib.version> <!-- trailing note -->
    </properties>
    <dependencies>
        <dependency>
            <groupId>o</groupId>
            <artifactId>lib</artifactId>
            <version>${lib.version}</version>
        </dependency>
        <dependency>
            <groupId>o</groupId>
            <artifactId>plain</artifactId>
            <version>1.0.0</version>
        </dependency>
    </dependencies>
</project>
"""


def write(tmp_path: Path, text: str = POM, name: str = "pom.xml") -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return path


def declaration(path: Path, coordinate: str):
    (found,) = [d for d in read_declarations([path]).declared if str(d.coordinate) == coordinate]
    return found


def test_a_literal_version_is_replaced_and_nothing_else_moves(tmp_path):
    path = write(tmp_path)
    site = declaration(path, "o:plain").site
    apply_changes([SetVersion(site, "1.0.0", "1.0.3")])
    assert path.read_text() == POM.replace("<version>1.0.0</version>", "<version>1.0.3</version>")


def test_a_property_is_edited_where_it_holds_the_literal(tmp_path):
    path = write(tmp_path)
    site = declaration(path, "o:lib").site
    assert site.kind == "property"
    apply_changes([SetVersion(site, "2.17.1", "2.17.3")])
    assert path.read_text() == POM.replace("<lib.version>2.17.1</lib.version>", "<lib.version>2.17.3</lib.version>")
    assert declaration(path, "o:lib").version == "2.17.3" and declaration(path, "o:lib").origin == PROPERTY


def test_several_edits_to_one_file_do_not_disturb_each_others_positions(tmp_path):
    path = write(tmp_path)
    first, second = declaration(path, "o:lib").site, declaration(path, "o:plain").site
    apply_changes([SetVersion(first, "2.17.1", "2.17.30000"), SetVersion(second, "1.0.0", "9")])
    text = path.read_text()
    assert "<lib.version>2.17.30000</lib.version>" in text and "<version>9</version>" in text
    assert declaration(path, "o:plain").version == "9" and declaration(path, "o:lib").version == "2.17.30000"


def test_crlf_files_stay_crlf_and_only_the_version_changes(tmp_path):
    crlf = POM.replace("\n", "\r\n")
    path = write(tmp_path, crlf)
    site = declaration(path, "o:plain").site
    apply_changes([SetVersion(site, "1.0.0", "1.0.3")])
    assert path.read_bytes() == crlf.replace("<version>1.0.0</version>", "<version>1.0.3</version>").encode("utf-8")


def test_a_stale_site_is_refused_and_writes_nothing(tmp_path):
    path = write(tmp_path)
    site = declaration(path, "o:plain").site
    with pytest.raises(ChangeError, match=r"pom\.xml:\d+: expected 9.9.9 but the file has 1\.0\.0"):
        apply_changes([SetVersion(site, "9.9.9", "1.0.3")])
    assert path.read_text() == POM


def test_overlapping_edits_are_refused(tmp_path):
    path = write(tmp_path)
    site = declaration(path, "o:plain").site
    with pytest.raises(ChangeError, match="more than one change to the same version"):
        apply_changes([SetVersion(site, "1.0.0", "1.0.1"), SetVersion(site, "1.0.0", "1.0.2")])
    assert path.read_text() == POM


def test_a_failing_change_leaves_every_file_untouched(tmp_path):
    path = write(tmp_path)
    good = SetVersion(declaration(path, "o:plain").site, "1.0.0", "1.0.3")
    with pytest.raises(ChangeError):
        apply_changes([good, AddExclusion(path, c("nope:nope"), c("a:b"))])
    assert path.read_text() == POM


# pins -------------------------------------------------------------------------------------------------------------


def test_a_pin_creates_dependency_management_in_the_documents_own_style(tmp_path):
    path = write(tmp_path)
    apply_changes([AddPin(path, c("org.spring:spring-core"), "6.2.19")])
    expected_tail = """    <dependencyManagement>
        <dependencies>
            <dependency>
                <groupId>org.spring</groupId>
                <artifactId>spring-core</artifactId>
                <version>6.2.19</version>
            </dependency>
        </dependencies>
    </dependencyManagement>
</project>
"""
    text = path.read_text()
    assert text.endswith(expected_tail) and text.startswith(POM[: POM.index("</project>")])
    found = declaration(path, "org.spring:spring-core")
    assert (found.section, found.origin, found.version) == ("dependencyManagement", LITERAL, "6.2.19")


def test_a_pin_joins_existing_management_and_keeps_its_comments(tmp_path):
    with_management = POM.replace("</project>", """    <dependencyManagement>
        <dependencies>
            <!-- an existing pin -->
            <dependency>
                <groupId>a</groupId>
                <artifactId>b</artifactId>
                <version>1</version>
            </dependency>
        </dependencies>
    </dependencyManagement>
</project>""")
    path = write(tmp_path, with_management)
    apply_changes([AddPin(path, c("c:d"), "2")])
    text = path.read_text()
    assert "<!-- an existing pin -->" in text and text.count("<dependencyManagement>") == 1
    assert text.index("<artifactId>b</artifactId>") < text.index("<artifactId>d</artifactId>")


def test_a_pin_can_carry_a_note_as_a_comment(tmp_path):
    path = write(tmp_path)
    apply_changes([AddPin(path, c("c:d"), "2", note="CVE-2026-1 fix -- re-evaluate on a new release")])
    assert "<!-- giml: CVE-2026-1 fix - re-evaluate on a new release -->" in path.read_text()
    assert declaration(path, "c:d").version == "2"


def test_pinning_something_already_managed_here_is_refused(tmp_path):
    path = write(tmp_path)
    apply_changes([AddPin(path, c("c:d"), "2")])
    with pytest.raises(ChangeError, match=r"c:d is already managed in .*pom\.xml; change its version instead"):
        apply_changes([AddPin(path, c("c:d"), "3")])


def test_pin_values_are_escaped(tmp_path):
    path = write(tmp_path)
    apply_changes([AddPin(path, c("c:d"), "1.0&<x>")])
    assert "<version>1.0&amp;&lt;x&gt;</version>" in path.read_text()


def test_two_pins_in_one_call(tmp_path):
    path = write(tmp_path)
    apply_changes([AddPin(path, c("c:d"), "2"), AddPin(path, c("e:f"), "3")])
    assert declaration(path, "c:d").version == "2" and declaration(path, "e:f").version == "3"
    assert path.read_text().count("<dependencyManagement>") == 1


# exclusions -------------------------------------------------------------------------------------------------------


def test_an_exclusion_is_added_to_the_holding_dependency(tmp_path):
    path = write(tmp_path)
    apply_changes([AddExclusion(path, c("o:plain"), c("commons-logging:commons-logging"))])
    expected = """        <dependency>
            <groupId>o</groupId>
            <artifactId>plain</artifactId>
            <version>1.0.0</version>
            <exclusions>
                <exclusion>
                    <groupId>commons-logging</groupId>
                    <artifactId>commons-logging</artifactId>
                </exclusion>
            </exclusions>
        </dependency>
"""
    assert expected in path.read_text()


def test_a_second_exclusion_joins_the_first_and_a_repeat_is_a_no_op(tmp_path):
    path = write(tmp_path)
    apply_changes([AddExclusion(path, c("o:plain"), c("a:b"))])
    once = path.read_text()
    apply_changes([AddExclusion(path, c("o:plain"), c("a:b"))])
    assert path.read_text() == once
    apply_changes([AddExclusion(path, c("o:plain"), c("c:d"))])
    assert path.read_text().count("<exclusions>") == 1 and path.read_text().count("<exclusion>") == 2


def test_an_exclusion_needs_its_holder_in_that_pom(tmp_path):
    path = write(tmp_path)
    with pytest.raises(ChangeError, match=r"o:absent is not a dependency declared in .*pom\.xml"):
        apply_changes([AddExclusion(path, c("o:absent"), c("a:b"))])


# descriptions -----------------------------------------------------------------------------------------------------


def test_describe_names_what_a_change_does(tmp_path):
    path = write(tmp_path)
    site = declaration(path, "o:lib").site
    assert describe(SetVersion(site, "2.17.1", "2.17.3"), tmp_path) == "set property lib.version 2.17.1 → 2.17.3 (pom.xml:9)"
    literal = declaration(path, "o:plain").site
    assert describe(SetVersion(literal, "1.0.0", "1.0.3"), tmp_path) == "set version 1.0.0 → 1.0.3 (pom.xml:20)"
    assert describe(AddPin(path, c("c:d"), "2"), tmp_path) == "pin c:d at 2 in pom.xml"
    assert describe(AddExclusion(path, c("o:plain"), c("a:b")), tmp_path) == "exclude a:b from o:plain in pom.xml"


# real POMs --------------------------------------------------------------------------------------------------------


def copy_redkite(tmp_path: Path) -> list[Path]:
    import shutil

    shutil.copytree(REDKITE, tmp_path / "redkite")
    return discover_reactor(tmp_path / "redkite")


def test_real_redkite_edit_of_a_module_property_changes_only_that_line(tmp_path):
    poms = copy_redkite(tmp_path)
    before = {p: p.read_bytes() for p in poms}
    declarations = read_declarations(poms)
    (gson,) = [d for d in declarations.declared if str(d.coordinate) == "com.google.code.gson:gson"]
    apply_changes([SetVersion(gson.site, "2.14.0", "2.14.1")])
    changed = [p for p in poms if p.read_bytes() != before[p]]
    assert [p.parent.name for p in changed] == ["red-kite-metadata"]
    old, new = before[changed[0]].decode().splitlines(), changed[0].read_text().splitlines()
    assert [(a, b) for a, b in zip(old, new, strict=True) if a != b] == [("    <gson.version>2.14.0</gson.version>", "    <gson.version>2.14.1</gson.version>")]
    assert len(old) == len(new)


def test_real_redkite_pin_edit_of_an_existing_managed_entry(tmp_path):
    poms = copy_redkite(tmp_path)
    declarations = read_declarations(poms)
    (pin,) = [d for d in declarations.declared if str(d.coordinate) == "org.springframework:spring-core"]
    apply_changes([SetVersion(pin.site, "6.2.19", "6.2.20")])
    text = poms[0].read_text()
    assert "<version>6.2.20</version>" in text and "redkite:dependency-management pin" in text  # its comment is untouched
    assert [d.version for d in read_declarations(poms).declared if str(d.coordinate) == "org.springframework:spring-core"] == ["6.2.20"]


# properties -------------------------------------------------------------------------------------------------------

versions = st.from_regex(r"[0-9]{1,3}(\.[0-9]{1,3}){0,2}", fullmatch=True)


@settings(max_examples=60, deadline=None)
@given(st.lists(versions, min_size=2, max_size=2), st.booleans())
def test_the_text_outside_the_edited_versions_never_changes(tmp_path_factory, new_versions, crlf):
    tmp_path = tmp_path_factory.mktemp("prop")
    text = POM.replace("\n", "\r\n") if crlf else POM
    path = write(tmp_path, text)
    first, second = declaration(path, "o:lib").site, declaration(path, "o:plain").site
    apply_changes([SetVersion(first, "2.17.1", new_versions[0]), SetVersion(second, "1.0.0", new_versions[1])])
    expected = text.replace("<lib.version>2.17.1</lib.version>", f"<lib.version>{new_versions[0]}</lib.version>").replace(
        "<version>1.0.0</version>", f"<version>{new_versions[1]}</version>")
    assert path.read_bytes() == expected.encode("utf-8")



# rebasing ---------------------------------------------------------------------------------------------------------


def test_changes_can_be_pointed_at_another_worktree_with_the_same_layout(tmp_path):
    from giml.maven.pom_change import rebase

    old, new = tmp_path / "result", tmp_path / "trial"
    for root in (old, new):
        root.mkdir()
        write(root, POM)
    site = declaration(old / "pom.xml", "o:plain").site
    moved = rebase(SetVersion(site, "1.0.0", "1.0.3"), old, new)
    assert moved.site.pom == new / "pom.xml" and moved.site.span == site.span and moved.site.line == site.line
    assert (moved.expected, moved.version) == ("1.0.0", "1.0.3")
    apply_changes([moved])
    assert (old / "pom.xml").read_text() == POM and "<version>1.0.3</version>" in (new / "pom.xml").read_text()
    pin = rebase(AddPin(old / "pom.xml", c("c:d"), "2", note="n"), old, new)
    exclusion = rebase(AddExclusion(old / "sub" / "pom.xml", c("o:plain"), c("a:b")), old, new)
    assert pin == AddPin(new / "pom.xml", c("c:d"), "2", note="n")
    assert exclusion == AddExclusion(new / "sub" / "pom.xml", c("o:plain"), c("a:b"))


def test_rebasing_a_path_outside_the_root_is_refused(tmp_path):
    from giml.maven.pom_change import rebase

    with pytest.raises(ChangeError, match="is not inside"):
        rebase(AddPin(tmp_path / "elsewhere" / "pom.xml", c("c:d"), "2"), tmp_path / "result", tmp_path / "trial")
