import pytest

from giml.maven.pom_xml import PomError
from giml.maven.project import MissingPomError, UnsupportedProjectError, check_single_module


def project(tmp_path, body: str):
    (tmp_path / "pom.xml").write_text(f'<project xmlns="http://maven.apache.org/POM/4.0.0">{body}</project>')
    return tmp_path


def test_single_module_project_passes(tmp_path):
    assert check_single_module(project(tmp_path, "<artifactId>a</artifactId>")) == tmp_path / "pom.xml"


def test_empty_modules_element_is_still_single_module(tmp_path):
    assert check_single_module(project(tmp_path, "<modules/>"))


@pytest.mark.parametrize(
    "body",
    [
        "<modules><module>core</module></modules>",
        "<profiles><profile><id>all</id><modules><module>extra</module></modules></profile></profiles>",
    ],
    ids=["top-level", "in-profile"],
)
def test_multi_module_is_refused(tmp_path, body):
    with pytest.raises(UnsupportedProjectError, match="multi-module projects unsupported in phase 1"):
        check_single_module(project(tmp_path, body))


def test_missing_pom(tmp_path):
    with pytest.raises(MissingPomError, match="no pom.xml"):
        check_single_module(tmp_path)


def test_unparseable_pom(tmp_path):
    (tmp_path / "pom.xml").write_text("<project>")
    with pytest.raises(PomError, match="cannot parse"):
        check_single_module(tmp_path)
