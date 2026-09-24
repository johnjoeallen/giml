import pytest
from hypothesis import given
from hypothesis import strategies as st

from giml.maven.pom_edit import (
    PomEditError,
    append_child,
    ensure_path,
    find,
    find_plugin,
    indent_unit,
    parse,
    text_of,
)

POM = """<?xml version="1.0" encoding="UTF-8"?>
<!-- a comment with <tags> & stuff -->
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <artifactId>demo</artifactId>
    <description><![CDATA[uses <b>markup</b>]]></description>
    <build>
        <plugins>
            <plugin>
                <artifactId>maven-surefire-plugin</artifactId>
            </plugin>
        </plugins>
    </build>
</project>
"""

PLUGIN = "<plugin>\n\t<groupId>org.jacoco</groupId>\n\t<artifactId>jacoco-maven-plugin</artifactId>\n</plugin>"


def test_parse_finds_elements_and_skips_comments_cdata_and_declarations():
    root = parse(POM)
    assert root.name == "project"
    assert [c.name for c in root.children] == ["modelVersion", "artifactId", "description", "build"]
    assert text_of(POM, root.child("artifactId")) == "demo"
    assert text_of(POM, root.child("build")) is None  # not a leaf


def test_append_child_before_end_tag_on_its_own_line():
    root = parse(POM)
    edited = append_child(POM, root, find(root, ["build", "plugins"]), PLUGIN)
    assert edited == POM.replace(
        "            </plugin>\n        </plugins>",
        "            </plugin>\n"
        "            <plugin>\n"
        "                <groupId>org.jacoco</groupId>\n"
        "                <artifactId>jacoco-maven-plugin</artifactId>\n"
        "            </plugin>\n"
        "        </plugins>",
    )
    assert find_plugin(edited, parse(edited), "org.jacoco", "jacoco-maven-plugin") is not None


def test_crlf_and_tab_indentation_are_preserved():
    pom = "<project>\r\n\t<build>\r\n\t\t<plugins>\r\n\t\t</plugins>\r\n\t</build>\r\n</project>\r\n"
    root = parse(pom)
    assert indent_unit(pom, root) == "\t"
    edited = append_child(pom, root, find(root, ["build", "plugins"]), "<plugin>\n\t<artifactId>x</artifactId>\n</plugin>")
    assert edited == (
        "<project>\r\n\t<build>\r\n\t\t<plugins>\r\n"
        "\t\t\t<plugin>\r\n\t\t\t\t<artifactId>x</artifactId>\r\n\t\t\t</plugin>\r\n"
        "\t\t</plugins>\r\n\t</build>\r\n</project>\r\n"
    )


def test_self_closing_parent_is_expanded():
    pom = '<project>\n  <build>\n    <plugins attr="1" />\n  </build>\n</project>\n'
    root = parse(pom)
    assert indent_unit(pom, root) == "  "
    edited = append_child(pom, root, find(root, ["build", "plugins"]), "<plugin/>")
    assert edited == '<project>\n  <build>\n    <plugins attr="1">\n      <plugin/>\n    </plugins>\n  </build>\n</project>\n'


def test_parent_closed_on_the_same_line():
    pom = "<project>\n  <build><plugins></plugins></build>\n</project>\n"
    root = parse(pom)
    edited = append_child(pom, root, find(root, ["build", "plugins"]), "<plugin/>")
    assert edited == "<project>\n  <build><plugins>\n    <plugin/>\n  </plugins></build>\n</project>\n"


def test_ensure_path_creates_only_missing_elements():
    pom = "<project>\n    <artifactId>a</artifactId>\n</project>\n"
    edited, created = ensure_path(pom, ["build", "plugins"])
    assert created == ["build", "build/plugins"]
    assert edited == "<project>\n    <artifactId>a</artifactId>\n    <build>\n        <plugins>\n        </plugins>\n    </build>\n</project>\n"
    again, created_again = ensure_path(edited, ["build", "plugins"])
    assert (again, created_again) == (edited, [])


def test_ensure_path_reuses_existing_build():
    edited, created = ensure_path(POM, ["build", "pluginManagement", "plugins"])
    assert created == ["build/pluginManagement", "build/pluginManagement/plugins"]
    assert edited.startswith(POM[: POM.index("        </plugins>\n    </build>")])


def test_find_plugin_checks_management_and_default_group():
    pom = POM.replace("<plugins>", "<pluginManagement><plugins><plugin><groupId>org.pitest</groupId>"
                      "<artifactId>pitest-maven</artifactId></plugin></plugins></pluginManagement><plugins>", 1)  # fmt: skip
    root = parse(pom)
    assert find_plugin(pom, root, "org.apache.maven.plugins", "maven-surefire-plugin") is not None
    assert find_plugin(pom, root, "org.pitest", "pitest-maven") is not None
    assert find_plugin(pom, root, "org.jacoco", "jacoco-maven-plugin") is None
    assert find_plugin("<project/>", parse("<project/>"), "g", "a") is None


def test_find_reports_missing_path():
    with pytest.raises(PomEditError, match="<project/build/nope> does not exist"):
        find(parse(POM), ["build", "nope"])


def test_default_indent_unit_without_nested_children():
    assert indent_unit("<project/>", parse("<project/>")) == "    "


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("<project><a></b></project>", "unexpected </b>"),
        ("<project><a>", "<a> is never closed"),
        ("<project/><other/>", "second root element"),
        ("<!-- no end", "unterminated <!--"),
        ("<project>< broken</project>", "malformed markup"),
        ("just text", "no root element"),
    ],
)
def test_malformed_documents_are_rejected(text, message):
    with pytest.raises(PomEditError, match=message):
        parse(text)


xml_text = st.text(alphabet=st.characters(blacklist_characters="<>&", blacklist_categories=("Cs",)), max_size=20)


@given(xml_text, xml_text, st.sampled_from(["\n", "\r\n"]), st.sampled_from(["  ", "    ", "\t"]))
def test_text_outside_the_insertion_is_unchanged(before, comment, nl, unit):
    comment = comment.replace("--", "-")
    pom = (f"<!--{comment}-->{nl}<project>{nl}{unit}<name>{before}</name>{nl}{unit}<build>{nl}"
           f"{unit}{unit}<plugins>{nl}{unit}{unit}</plugins>{nl}{unit}</build>{nl}</project>{nl}")  # fmt: skip
    root = parse(pom)
    plugins = find(root, ["build", "plugins"])
    edited = append_child(pom, root, plugins, "<plugin/>")
    cut = pom.rfind(nl, 0, plugins.end_start) + len(nl)
    inserted = f"{unit}{unit}{unit}<plugin/>{nl}"
    assert edited == pom[:cut] + inserted + pom[cut:]
