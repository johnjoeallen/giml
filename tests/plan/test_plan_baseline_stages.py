from giml.core.config import default_gate_config_path, load_gate_config
from giml.plan.baseline_run import verification_stages
from giml.plan.planner import pit_floors
from giml.plan.baseline import Baseline, StageBaseline

POM = ('<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><groupId>g</groupId>'
       "<artifactId>a</artifactId><version>1</version>{extra}</project>")


def project(tmp_path, extra="", tests=()):
    (tmp_path / "pom.xml").write_text(POM.format(extra=extra))
    for name in tests:
        path = tmp_path / "src" / "test" / "java" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("class X {}")
    return tmp_path


def test_a_project_without_integration_tests_or_a_tier_gets_the_three_stages(tmp_path):
    config = load_gate_config(default_gate_config_path())
    assert verification_stages(config, project(tmp_path), has_tier=False) == ["compile", "unit_test", "enforcer"]


def test_integration_tests_and_a_tier_add_their_stages(tmp_path):
    config = load_gate_config(default_gate_config_path())
    assert verification_stages(config, project(tmp_path, tests=["CartIT.java"]), has_tier=True) == [
        "compile", "unit_test", "enforcer", "integration", "pit"]  # fmt: skip


def test_a_declared_failsafe_counts_as_integration_tests(tmp_path):
    config = load_gate_config(default_gate_config_path())
    plugin = "<build><plugins><plugin><artifactId>maven-failsafe-plugin</artifactId></plugin></plugins></build>"
    assert "integration" in verification_stages(config, project(tmp_path, plugin), has_tier=False)


def test_the_floor_is_the_tier_threshold_or_the_baseline_when_lower():
    from giml.maven.build import StageOutcome
    from pathlib import Path

    config = load_gate_config(default_gate_config_path())
    tier = config.tiers["B"]

    def baseline(**counts):
        outcome = StageOutcome("pit", True, 1.0, Path("/x"), None, details={"mutations": counts})
        return Baseline((StageBaseline("pit", "passed", 1, outcome),), None, False)

    assert pit_floors(baseline(KILLED=95, SURVIVED=5), tier) == (tier.pit_mutation_coverage, tier.pit_test_strength)
    assert pit_floors(baseline(KILLED=70, SURVIVED=30), tier) == (70.0, 70.0)
    assert pit_floors(Baseline((), None, False), tier) is None and pit_floors(baseline(KILLED=1), None) is None
