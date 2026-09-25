"""Edited POMs mean what they say to real Maven (slow). Run with `pytest -m slow`."""

import os
import pwd
import shutil
from pathlib import Path

import pytest

from giml.core.model import Coordinate
from giml.maven.declarations import read_declarations
from giml.maven.pom_change import AddExclusion, AddPin, SetVersion, apply_changes
from giml.maven.runner import run_maven
from giml.maven.tree import resolve_reactor

pytestmark = pytest.mark.slow

POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <groupId>t</groupId>
    <artifactId>edited</artifactId>
    <version>1</version>
    <properties>
        <lang.version>3.14.0</lang.version>
    </properties>
    <dependencies>
        <dependency>
            <groupId>org.apache.commons</groupId>
            <artifactId>commons-lang3</artifactId>
            <version>${lang.version}</version>
        </dependency>
        <dependency>
            <groupId>commons-beanutils</groupId>
            <artifactId>commons-beanutils</artifactId>
            <version>1.9.4</version>
        </dependency>
    </dependencies>
</project>
"""


@pytest.fixture
def real_home(monkeypatch):
    monkeypatch.setenv("HOME", str(Path(pwd.getpwuid(os.getuid()).pw_dir)))


def resolved(tmp_path, pom):
    (tree,) = resolve_reactor(tmp_path, [pom], tmp_path / "tree.log", 600, run_maven, None)
    return {str(d.coordinate): d.version for d in tree.dependencies}


def test_real_maven_resolves_the_edited_pom_as_intended(tmp_path, real_home):
    if shutil.which("mvn") is None or shutil.which("java") is None:
        pytest.skip("mvn and java are required")
    pom = tmp_path / "pom.xml"
    pom.write_text(POM)
    before = resolved(tmp_path, pom)
    assert before["org.apache.commons:commons-lang3"] == "3.14.0"
    assert before["commons-logging:commons-logging"] and before["commons-collections:commons-collections"]  # via beanutils

    declarations = read_declarations([pom])
    lang = next(d for d in declarations.declared if str(d.coordinate) == "org.apache.commons:commons-lang3")
    apply_changes([
        SetVersion(lang.site, "3.14.0", "3.17.0"),  # edits the property the dependency points at
        AddPin(pom, Coordinate.parse("commons-collections:commons-collections"), "3.2.2", note="pinned by the test"),
        AddExclusion(pom, Coordinate.parse("commons-beanutils:commons-beanutils"), Coordinate.parse("commons-logging:commons-logging")),
    ])
    after = resolved(tmp_path, pom)
    assert after["org.apache.commons:commons-lang3"] == "3.17.0"  # the version edit took effect through the property
    assert after["commons-collections:commons-collections"] == "3.2.2"  # the pin manages a transitive dependency
    assert "commons-logging:commons-logging" not in after  # the exclusion removed it
    assert "<!-- giml: pinned by the test -->" in pom.read_text()
