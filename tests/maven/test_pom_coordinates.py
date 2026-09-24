from pathlib import Path

import pytest

from giml.maven.pom_coordinates import PomError, read_pom_coordinates

SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "poms" / "sample-pom.xml"


def test_sample_pom_declared_coordinates():
    result = read_pom_coordinates(SAMPLE)
    assert sorted(str(c) for c in result.coordinates) == [
        "com.fasterxml.jackson.core:jackson-databind",
        "org.apache.maven.plugins:maven-failsafe-plugin",
        "org.apache.maven.plugins:maven-surefire-plugin",
        "org.jacoco:jacoco-maven-plugin",
        "org.junit:junit-bom",
        "org.pitest:pitest-maven",
        "org.springframework.boot:example-shared",  # ${project.groupId} inherited from parent
        "org.springframework.boot:spring-boot-starter-parent",
        "org.testcontainers:postgresql",
    ]


def test_unresolvable_and_cyclic_properties_are_reported_not_guessed():
    result = read_pom_coordinates(SAMPLE)
    assert result.unresolved == ["dependency ${undefined.group}:mystery", "dependency ${loop.a}:cyclic"]


def test_pom_without_namespace(tmp_path):
    pom = tmp_path / "pom.xml"
    pom.write_text(
        "<project><groupId>a.b</groupId><artifactId>x</artifactId>"
        "<dependencies><dependency><groupId>${project.groupId}</groupId><artifactId>y</artifactId>"
        "</dependency></dependencies></project>"
    )
    assert [str(c) for c in read_pom_coordinates(pom).coordinates] == ["a.b:y"]


def test_missing_artifact_id_is_unresolved(tmp_path):
    pom = tmp_path / "pom.xml"
    pom.write_text("<project><dependencies><dependency><groupId>g</groupId></dependency></dependencies></project>")
    assert read_pom_coordinates(pom).unresolved == ["dependency g:None"]


def test_malformed_coordinate_text_is_unresolved(tmp_path):
    pom = tmp_path / "pom.xml"
    pom.write_text("<project><dependencies><dependency><groupId>has space</groupId>"
                   "<artifactId>a</artifactId></dependency></dependencies></project>")  # fmt: skip
    assert read_pom_coordinates(pom).unresolved == ["dependency has space:a"]


@pytest.mark.parametrize(
    ("content", "message"),
    [("<project>", "cannot parse"), ("<settings/>", "expected <project>")],
)
def test_invalid_poms_are_rejected(tmp_path, content, message):
    pom = tmp_path / "pom.xml"
    pom.write_text(content)
    with pytest.raises(PomError, match=message):
        read_pom_coordinates(pom)


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(PomError, match="cannot parse"):
        read_pom_coordinates(tmp_path / "pom.xml")
