from pathlib import Path

from giml.gate.modules import declares_failsafe, module_facts
from giml.maven.project import discover_reactor

MINI = Path(__file__).resolve().parents[1] / "fixtures" / "maven" / "mini-reactor"


def test_mini_reactor_module_facts():
    parent, core, app = (module_facts(p) for p in discover_reactor(MINI))
    assert (parent.packaging, parent.main_sources, parent.untested) == ("pom", 0, False)
    assert (core.packaging, core.main_sources, core.test_sources, core.untested) == ("jar", 1, 1, False)
    assert (app.main_sources, app.test_sources, app.untested) == (1, 0, True)
    assert core.jacoco_report == MINI.resolve() / "core" / "target" / "site" / "jacoco" / "jacoco.xml"
    assert core.pit_report.name == "mutations.xml" and core.jacoco_exec.name == "jacoco.exec"
    assert core.classes_dir.name == "classes" and core.surefire_reports.name == "surefire-reports"
    assert not declares_failsafe(discover_reactor(MINI))


def test_integration_test_names(tmp_path):
    tests = tmp_path / "src" / "test" / "java" / "p"
    tests.mkdir(parents=True)
    for name in ["FooIT.java", "ITBar.java", "BazITCase.java", "UnitTest.java", "SITE.java"]:
        (tests / name).write_text("class X {}")
    (tmp_path / "pom.xml").write_text("<project><build><plugins><plugin><artifactId>maven-failsafe-plugin"
                                      "</artifactId></plugin></plugins></build></project>")  # fmt: skip
    facts = module_facts(tmp_path / "pom.xml")
    assert (facts.test_sources, facts.integration_tests) == (5, 3)
    assert declares_failsafe([tmp_path / "pom.xml"])
