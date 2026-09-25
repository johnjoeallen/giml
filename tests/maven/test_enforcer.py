from pathlib import Path

import pytest

from giml.maven.enforcer import Violation, new_violations, parse_enforcer, resolved_violations

LOGS = Path(__file__).resolve().parents[1] / "fixtures" / "logs"


def real(name: str) -> tuple[Violation, ...]:
    return parse_enforcer((LOGS / f"{name}.log").read_text(encoding="utf-8"))


def test_a_convergence_violation_names_the_artifact_the_versions_and_the_paths():
    (violation,) = real("convergence-a")
    assert (violation.rule, violation.subject) == ("DependencyConvergence", "com.fasterxml.jackson.core:jackson-core")
    assert violation.identity == "DependencyConvergence:com.fasterxml.jackson.core:jackson-core"
    assert violation.detail["versions"] == ["2.13.0", "2.15.0"]
    assert violation.detail["paths"] == [
        ["t:convergence:jar:1", "com.fasterxml.jackson.core:jackson-databind:jar:2.15.0:compile",
         "com.fasterxml.jackson.core:jackson-core:jar:2.15.0:compile"],
        ["t:convergence:jar:1", "com.fasterxml.jackson.core:jackson-core:jar:2.13.0:compile"],
    ]  # fmt: skip


def test_duplicate_classes_name_the_artifacts_whatever_order_maven_lists_them_in():
    first, second = real("duplicate-classes-a"), real("duplicate-classes-b")
    (violation,) = first
    assert (violation.rule, violation.subject) == ("BanDuplicateClasses", "commons-logging:commons-logging + org.slf4j:jcl-over-slf4j")
    assert violation.detail["artifacts"] == ["commons-logging:commons-logging:1.2", "org.slf4j:jcl-over-slf4j:1.7.36"]
    assert violation.detail["classes"] >= 6 and "org/apache/commons/logging/Log.class" in violation.detail["sample"]
    assert first == second


def test_duplicate_pom_dependency_versions_name_each_declaration():
    violations = real("enforcer-duplicate-pom-versions")
    assert [(v.rule, v.subject, v.detail["times"]) for v in violations] == [
        ("BanDuplicatePomDependencyVersions", "commons-io:commons-io:jar", 2),
        ("BanDuplicatePomDependencyVersions", "org.apache.commons:commons-lang3:jar", 2)]  # fmt: skip
    assert violations[0].detail["declared_in"] == "dependencies.dependency"


def test_other_rules_are_kept_with_their_message():
    (violation,) = real("enforcer-require-java")
    assert violation.rule == "RequireJavaVersion"
    assert "is version 21.0.9 which is not in the allowed range [99,)." in violation.subject
    assert "/home/dev" not in violation.subject  # directories are stripped so the same failure has one identity


def test_several_failing_rules_in_one_run_are_all_reported_in_a_fixed_order():
    violations = real("enforcer-multi")
    assert [v.rule for v in violations] == ["BanDuplicateClasses", "DependencyConvergence", "RequireJavaVersion"]
    assert len({v.identity for v in violations}) == 3


def test_a_passing_run_has_no_violations():
    assert parse_enforcer("[INFO] BUILD SUCCESS\n[INFO] Rule 0: org.x.Foo passed\n") == ()
    assert parse_enforcer("") == ()


def block(*lines: str) -> str:
    return "\n".join(f"[ERROR] {line}" for line in lines) + "\n"


def test_convergence_with_three_paths_and_a_classifier_node():
    text = block("Rule 0: org.apache.maven.enforcer.rules.dependency.DependencyConvergence failed with message:",
                 "Failed while enforcing releasability.",
                 "Dependency convergence error for org.x:lib:jar:1.0. Paths to dependency are:",
                 "+-g:app:jar:1", "  +-org.a:a:jar:1.0:compile", "    +-org.x:lib:jar:1.0:compile", "and",
                 "+-g:app:jar:1", "  +-org.b:b:jar:2.0:compile", "    +-org.x:lib:jar:tests:2.0:compile", "and",
                 "+-g:app:jar:1", "  +-org.x:lib:jar:3.0:compile", "-> [Help 1]")  # fmt: skip
    (violation,) = parse_enforcer(text)
    assert violation.detail["versions"] == ["1.0", "2.0", "3.0"] and len(violation.detail["paths"]) == 3


def test_two_conflicts_in_one_rule_are_two_violations():
    text = block("Rule 0: org.apache.maven.enforcer.rules.dependency.DependencyConvergence failed with message:",
                 "Dependency convergence error for org.x:one:jar:1. Paths to dependency are:", "+-g:app:jar:1", "  +-org.x:one:jar:1:compile", "and",
                 "+-g:app:jar:1", "  +-org.p:p:jar:1:compile", "    +-org.x:one:jar:2:compile",
                 "Dependency convergence error for org.y:two:jar:5. Paths to dependency are:", "+-g:app:jar:1", "  +-org.y:two:jar:5:compile", "and",
                 "+-g:app:jar:1", "  +-org.q:q:jar:1:compile", "    +-org.y:two:jar:6:compile")  # fmt: skip
    assert [v.subject for v in parse_enforcer(text)] == ["org.x:one", "org.y:two"]


def test_several_duplicate_class_groups():
    text = block("Rule 1: org.codehaus.mojo.extraenforcer.dependencies.BanDuplicateClasses failed with message:",
                 "Duplicate classes found:", "", "  Found in:", "    b:b:jar:1:compile", "    a:a:jar:1:compile", "  Duplicate classes:", "    x/One.class",
                 "Duplicate classes found:", "", "  Found in:", "    d:d:jar:2:compile", "    c:c:jar:2:compile", "  Duplicate classes:", "    y/Two.class", "    y/Three.class")  # fmt: skip
    violations = parse_enforcer(text)
    assert [(v.subject, v.detail["classes"]) for v in violations] == [("a:a + b:b", 1), ("c:c + d:d", 2)]


def test_the_same_block_printed_twice_counts_once():
    once = block("Rule 0: org.apache.maven.enforcer.rules.BanDuplicatePomDependencyVersions failed with message:",
                 "Found 1 duplicate dependency declarations in this project:", " - dependencies.dependency[g:a:jar] (2 times)")  # fmt: skip
    assert parse_enforcer(once + once) == parse_enforcer(once) and len(parse_enforcer(once)) == 1


def test_a_management_section_duplicate():
    text = block("Rule 0: org.apache.maven.enforcer.rules.BanDuplicatePomDependencyVersions failed with message:",
                 " - dependencyManagement.dependencies.dependency[g:a:jar] (3 times)")  # fmt: skip
    (violation,) = parse_enforcer(text)
    assert (violation.detail["declared_in"], violation.detail["times"]) == ("dependencyManagement.dependencies.dependency", 3)


def test_a_violation_survives_a_round_trip_through_json():
    for violation in real("enforcer-multi"):
        assert Violation.from_dict(violation.to_dict()) == violation


def test_new_and_resolved_violations_compare_by_rule_and_subject_not_by_versions():
    reference = real("convergence-a") + real("duplicate-classes-a")
    same_conflict_other_versions = parse_enforcer(block(
        "Rule 0: org.apache.maven.enforcer.rules.dependency.DependencyConvergence failed with message:",
        "Dependency convergence error for com.fasterxml.jackson.core:jackson-core:jar:2.16.0. Paths to dependency are:", "+-t:convergence:jar:1",
        "  +-com.fasterxml.jackson.core:jackson-databind:jar:2.16.0:compile", "    +-com.fasterxml.jackson.core:jackson-core:jar:2.16.0:compile", "and",
        "+-t:convergence:jar:1", "  +-com.fasterxml.jackson.core:jackson-core:jar:2.13.0:compile"))  # fmt: skip
    assert new_violations(reference, same_conflict_other_versions) == ()
    assert [v.rule for v in resolved_violations(reference, same_conflict_other_versions)] == ["BanDuplicateClasses"]


def test_a_violation_outside_the_reference_is_new():
    reference = real("convergence-a")
    candidate = real("enforcer-multi")
    assert [v.rule for v in new_violations(reference, candidate)] == ["BanDuplicateClasses", "RequireJavaVersion"]
    assert new_violations(candidate, candidate) == () and resolved_violations(candidate, ()) == candidate


def test_parsing_is_deterministic():
    text = (LOGS / "enforcer-multi.log").read_text(encoding="utf-8")
    assert parse_enforcer(text) == parse_enforcer(text)


@pytest.mark.parametrize("junk", ["[ERROR] Rule 0: broken line", "[ERROR] Rule x: a.B failed with message:", "Rule 0: a.B failed with message:"])
def test_lines_that_are_not_rule_blocks_are_ignored(junk):
    assert parse_enforcer(junk + "\n") == ()
