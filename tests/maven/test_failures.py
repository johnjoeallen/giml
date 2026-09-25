from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from giml.maven.failures import (
    COMPILE, DUPLICATE_CLASSES, ENFORCER_CONVERGENCE, INFRASTRUCTURE, INTEGRATION_TEST, RESOLUTION, TIMEOUT, UNIT_TEST, UNKNOWN,
    Failure, classify, normalise,
)  # fmt: skip

LOGS = Path(__file__).resolve().parents[1] / "fixtures" / "logs"
SCENARIOS = {
    "compile-error": COMPILE, "test-failure": UNIT_TEST, "unresolvable": RESOLUTION,
    "convergence": ENFORCER_CONVERGENCE, "duplicate-classes": DUPLICATE_CLASSES,
}  # fmt: skip


def real(name: str, copy: str) -> Failure:
    return classify((LOGS / f"{name}-{copy}.log").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", SCENARIOS)
def test_real_maven_failures_are_classified(name):
    assert real(name, "a").failure_class == SCENARIOS[name] == real(name, "b").failure_class


@pytest.mark.parametrize("name", SCENARIOS)
def test_a_signature_is_stable_across_paths_timestamps_and_durations(name):
    a, b = real(name, "a"), real(name, "b")
    assert a.signature == b.signature and a.key_lines == b.key_lines
    joined = "\n".join(a.key_lines)
    for leak in ("/home/dev", "/srv/ci", "build-one", "other-checkout", "2026-"):
        assert leak not in joined


def test_the_two_copies_really_do_differ_before_normalising():
    # Guards the fixtures: if the logs were identical the stability tests would prove nothing.
    for name in SCENARIOS:
        a = (LOGS / f"{name}-a.log").read_text()
        b = (LOGS / f"{name}-b.log").read_text()
        assert a != b, name


def test_signatures_differ_between_different_failures():
    signatures = {real(name, "a").signature for name in SCENARIOS}
    assert len(signatures) == len(SCENARIOS) and all(len(s) == 16 for s in signatures)


def test_compile_error_key_lines():
    lines = real("compile-error", "a").key_lines
    assert lines[0] == "COMPILATION ERROR :"
    assert "App.java:[<n>,<n>] cannot find symbol" in lines
    assert "symbol: variable undefinedSymbol" in lines and "location: class t.App" in lines
    assert any(line.startswith("Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.13.0:compile") for line in lines)
    assert all("on project compile-error" not in line for line in lines)  # project names are not part of the signature


def test_test_failure_key_lines_drop_times_and_line_numbers():
    lines = real("test-failure", "a").key_lines
    assert "Tests run: 1, Failures: 1, Errors: 0, Skipped: 0, Time elapsed: <t> <<< FAILURE! -- in t.CalcTest" in lines
    assert "CalcTest.adds:<n> expected: <3> but was: <4>" in lines
    assert not any(line.startswith("Please refer to") for line in lines)


def test_enforcer_key_lines_name_the_rule_and_the_artifacts():
    convergence = "\n".join(real("convergence", "a").key_lines)
    assert "DependencyConvergence failed with message" in convergence
    assert "Dependency convergence error for com.fasterxml.jackson.core:jackson-core:jar:2.15.0. Paths to dependency are:" in convergence
    duplicates = "\n".join(real("duplicate-classes", "a").key_lines)
    assert "org.slf4j:jcl-over-slf4j:jar:1.7.36:compile" in duplicates and "commons-logging:commons-logging:jar:1.2:compile" in duplicates


def test_resolution_key_lines_name_the_missing_artifact_without_the_project():
    lines = real("unresolvable", "a").key_lines
    assert "Could not find artifact org.nonexistent.giml:no-such-artifact:jar:1.0 in https://repo.maven.apache.org/maven2" in lines
    assert "Failed to execute goal on project <p>: Could not resolve dependencies for project <p>" in lines


def error_log(*lines: str) -> str:
    return "\n".join(f"[ERROR] {line}" for line in lines) + "\n"


@pytest.mark.parametrize(("text", "expected"), [
    (error_log("Could not transfer artifact org.x:y:pom:1 from/to central (https://repo/): Connect timed out"), INFRASTRUCTURE),
    (error_log("Could not transfer artifact org.x:y:pom:1 from/to central: repo.example: Name or service not known",
               "java.net.UnknownHostException: repo.example"), INFRASTRUCTURE),
    (error_log("Transfer failed for https://repo/x.pom 503 Service Unavailable"), INFRASTRUCTURE),
    ("java.lang.OutOfMemoryError: Java heap space\n[ERROR] Failed to execute goal ...surefire...:test", INFRASTRUCTURE),
    (error_log("No space left on device"), INFRASTRUCTURE),
    (error_log("Failed to execute goal on project x: Could not resolve dependencies for project g:x:jar:1",
               "Could not transfer artifact g:y:pom:1 from/to central: Read timed out"), INFRASTRUCTURE),
    (error_log("Non-resolvable parent POM for g:x:1: The following artifacts could not be resolved: org.p:parent:pom:9"), RESOLUTION),
    (error_log("Failed to read artifact descriptor for org.x:y:jar:1"), RESOLUTION),
    (error_log("Could not transfer artifact org.x:y:pom:1 from/to central (https://repo/): status code: 404, reason phrase: Not Found (404)"), RESOLUTION),
    (error_log("Plugin org.p:plugin:1 or one of its dependencies could not be resolved"), RESOLUTION),
    (error_log("Failed to execute goal org.apache.maven.plugins:maven-failsafe-plugin:3.2.5:verify (default) on project x: There are test failures."), INTEGRATION_TEST),
    (error_log("Rule 1: org.apache.maven.enforcer.rules.dependency.BanDuplicatePomDependencyVersions failed with message:"), ENFORCER_CONVERGENCE),
    (error_log("Failed to execute goal org.apache.maven.plugins:maven-surefire-plugin:3.2.5:test (default-test) on project x: There are test failures."), UNIT_TEST),
    (error_log("Rule 0: org.apache.maven.enforcer.rules.RequireJavaVersion failed with message: nope"), UNKNOWN),
    ("just some words\n", UNKNOWN),
    ("", UNKNOWN),
])  # fmt: skip
def test_classification_of_other_failures(text, expected):
    assert classify(text).failure_class == expected


def test_a_timeout_wins_over_whatever_the_log_says():
    failure = classify(error_log("COMPILATION ERROR"), timed_out=True)
    assert (failure.failure_class, failure.key_lines) == (TIMEOUT, ("timed out",))
    assert failure.signature == classify("", timed_out=True).signature


def test_infrastructure_outranks_resolution_and_compile():
    text = error_log("COMPILATION ERROR", "Could not resolve dependencies", "Connection refused")
    assert classify(text).failure_class == INFRASTRUCTURE


def test_an_unclassified_failure_still_gets_a_signature_from_its_errors():
    a = classify(error_log("Something odd happened in /tmp/x/y/Thing.java:12"))
    b = classify(error_log("Something odd happened in /other/dir/Thing.java:99"))
    assert (a.failure_class, a.signature) == (UNKNOWN, b.signature) and a.key_lines == ("Something odd happened in Thing.java:<n>",)


def test_the_class_is_part_of_the_signature():
    same_words = error_log("boom")
    assert classify(same_words).signature == classify(same_words).signature
    assert classify(error_log("COMPILATION ERROR", "boom")).signature != classify(error_log("boom")).signature


def test_a_failure_cached_by_the_resolver_reads_like_the_original():
    first = error_log("Could not find artifact org.x:y:jar:1.0 in central (https://repo.example/maven2)")
    repeat = error_log("org.x:y:jar:1.0 was not found in https://repo.example/maven2 during a previous attempt. This failure was "
                       "cached in the local repository and resolution is not reattempted until the update interval of central "
                       "has elapsed or updates are forced")  # fmt: skip
    assert classify(first) == classify(repeat)
    assert classify(first).key_lines == ("Could not find artifact org.x:y:jar:1.0 in https://repo.example/maven2",)


def duplicates_log(artifacts, classes):
    return error_log("Rule 0: org.codehaus.mojo.extraenforcer.dependencies.BanDuplicateClasses failed with message:",
                     "Duplicate classes found:", "  Found in:", *[f"    {a}" for a in artifacts], "  Duplicate classes:",
                     *[f"    {c}" for c in classes], "Failed to execute goal ...enforcer... on project app: x")  # fmt: skip


def test_the_order_of_the_offending_artifacts_and_classes_does_not_change_the_signature():
    artifacts = ["org.slf4j:jcl-over-slf4j:jar:1.7.36:compile", "commons-logging:commons-logging:jar:1.2:compile"]
    classes = ["org/apache/commons/logging/Log.class", "org/apache/commons/logging/LogFactory.class"]
    first, second = duplicates_log(artifacts, classes), duplicates_log(artifacts[::-1], classes[::-1])
    assert classify(first) == classify(second)
    assert classify(first).key_lines[3:5] == ("commons-logging:commons-logging:jar:1.2:compile", "org.slf4j:jcl-over-slf4j:jar:1.7.36:compile")


def test_different_offending_artifacts_give_different_signatures():
    one = duplicates_log(["a:b:jar:1:compile", "c:d:jar:2:compile"], ["x/Y.class"])
    other = duplicates_log(["a:b:jar:1:compile", "e:f:jar:3:compile"], ["x/Y.class"])
    assert classify(one).signature != classify(other).signature


def test_key_lines_skip_boilerplate_blanks_and_repeats_and_are_capped():
    text = error_log("", "boom", "boom", "-> [Help 1]", "To see the full stack trace of the errors, re-run Maven with the -e switch.",
                     "Re-run Maven using the -X switch to enable full debug logging.", "For more information about the errors and possible solutions, please read the following articles:",
                     "After correcting the problems, you can resume the build with the command", "  mvn <args> -rf :core",
                     "[Help 1] http://cwiki.apache.org/confluence/display/MAVEN/MojoFailureException",
                     *[f"distinct {i}" for i in range(12)])  # fmt: skip
    lines = classify(text).key_lines
    assert lines == ("boom", *[f"distinct {i}" for i in range(7)])


def test_long_key_lines_are_truncated():
    assert len(classify(error_log("x" * 1000)).key_lines[0]) == 300


@pytest.mark.parametrize(("raw", "expected"), [
    ("/home/me/proj/src/main/java/t/App.java:[3,20] cannot find symbol", "App.java:[<n>,<n>] cannot find symbol"),
    ("Please refer to /home/me/proj/target/surefire-reports for details", "Please refer to <dir> for details"),
    ("at 2026-09-25T12:30:45.123+01:00 something", "at <ts> something"),
    ("at 2026-09-25 12:30:45,123 something", "at <ts> something"),
    ("Time elapsed: 0.014 s <<< FAILURE!", "Time elapsed: <t> <<< FAILURE!"),
    ("took 250 ms and 3 min", "took <t> and <t>"),
    ("CalcTest.adds:5 expected: <3> but was: <4>", "CalcTest.adds:<n> expected: <3> but was: <4>"),
    ("in run 20260924T145745Z-154040 and 0123456789abcdef0123", "in run <id> and <id>"),
    ("junit-317825126815449955/file-repo", "junit-<id>/file-repo"),
    ("failed for project t:unresolvable:jar:1", "failed for project <p>"),
    ("on project my-app: broke", "on project <p>: broke"),
    ("Could not find org.x:y:jar:2.15.0 in central (https://repo.maven.apache.org/maven2)",
     "Could not find org.x:y:jar:2.15.0 in central (https://repo.maven.apache.org/maven2)"),
    ("  lots   of\tspace  ", "lots of space"),
])  # fmt: skip
def test_normalise(raw, expected):
    assert normalise(raw) == expected


def test_normalise_keeps_versions_intact():
    assert normalise("org.slf4j:jcl-over-slf4j:jar:1.7.36:compile") == "org.slf4j:jcl-over-slf4j:jar:1.7.36:compile"
    assert normalise("com.fasterxml.jackson.core:jackson-core:jar:2.15.0") == "com.fasterxml.jackson.core:jackson-core:jar:2.15.0"


segment = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=1, max_size=12)


@given(st.lists(segment, min_size=1, max_size=6), st.lists(segment, min_size=1, max_size=6), st.integers(1, 9999), st.integers(1, 9999))
def test_the_directory_and_the_line_numbers_never_change_a_signature(first, second, line1, line2):
    a = error_log(f"/{'/'.join(first)}/src/App.java:[{line1},{line2}] cannot find symbol")
    b = error_log(f"/{'/'.join(second)}/src/App.java:[{line2},{line1}] cannot find symbol")
    assert classify(a) == classify(b)
