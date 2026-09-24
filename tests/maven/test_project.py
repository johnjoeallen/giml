import pytest

from giml.maven.pom_xml import PomError
from giml.maven.project import MissingPomError, UnsupportedProjectError, discover_reactor

NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'


def pom(directory, body: str = ""):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pom.xml").write_text(f"<project {NS}>{body}</project>")
    return directory / "pom.xml"


def modules(*names: str) -> str:
    return "<modules>" + "".join(f"<module>{n}</module>" for n in names) + "</modules>"


def test_single_module_project_is_a_one_pom_reactor(tmp_path):
    root = pom(tmp_path, "<artifactId>a</artifactId>")
    assert discover_reactor(tmp_path) == [root.resolve()]


def test_empty_modules_element_adds_nothing(tmp_path):
    root = pom(tmp_path, "<modules/><modules><module> </module></modules>")
    assert discover_reactor(tmp_path) == [root.resolve()]


def test_reactor_is_root_first_then_modules_depth_first(tmp_path):
    root = pom(tmp_path, modules("core", "server"))
    core = pom(tmp_path / "core", modules("core-api"))
    api = pom(tmp_path / "core" / "core-api")
    server = pom(tmp_path / "server")
    assert discover_reactor(tmp_path) == [p.resolve() for p in (root, core, api, server)]


def test_profile_modules_and_explicit_pom_paths_are_included(tmp_path):
    body = modules("a") + f"<profiles><profile><id>extra</id>{modules('b/custom-pom.xml')}</profile></profiles>"
    root = pom(tmp_path, body)
    a = pom(tmp_path / "a")
    (tmp_path / "b").mkdir()
    b = tmp_path / "b" / "custom-pom.xml"
    b.write_text(f"<project {NS}/>")
    assert discover_reactor(tmp_path) == [root.resolve(), a.resolve(), b.resolve()]


def test_module_listed_twice_is_visited_once(tmp_path):
    root = pom(tmp_path, modules("a", "./a") + f"<profiles><profile>{modules('a')}</profile></profiles>")
    a = pom(tmp_path / "a")
    assert discover_reactor(tmp_path) == [root.resolve(), a.resolve()]


def test_missing_root_pom(tmp_path):
    with pytest.raises(MissingPomError, match="no pom.xml"):
        discover_reactor(tmp_path)


def test_declared_module_without_pom(tmp_path):
    pom(tmp_path, modules("ghost"))
    with pytest.raises(MissingPomError, match="ghost/pom.xml: declared module has no pom.xml"):
        discover_reactor(tmp_path)


def test_module_outside_the_project_is_unsupported(tmp_path):
    pom(tmp_path / "outside")
    pom(tmp_path / "project", modules("../outside"))
    with pytest.raises(UnsupportedProjectError, match="module outside the project directory"):
        discover_reactor(tmp_path / "project")


def test_unparseable_module_pom(tmp_path):
    pom(tmp_path, modules("bad"))
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "pom.xml").write_text("<project>")
    with pytest.raises(PomError, match="cannot parse"):
        discover_reactor(tmp_path)
