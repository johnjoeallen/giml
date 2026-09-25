import datetime
from pathlib import Path

import pytest

from giml.core.config import PlanningSettings
from giml.core.model import Coordinate, Finding, Severity, SeverityRating, SeveritySource, VersionRelease
from giml.maven.declarations import Site, read_declarations
from giml.maven.pom_change import AddPin, SetVersion
from giml.maven.tree import Dependency, ModuleTree
from giml.maven.version import ComparableVersion
from giml.plan.analysis import Analysis
from giml.plan.candidates import plan_dependency
from giml.plan.exposure import resolve_exposure
from giml.plan.parents import ChangeUnit, Evaluation, Pick, UnitPlan
from giml.plan.steps import dependency_proposals, unit_proposals

NOW = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.UTC)
OLD = NOW - datetime.timedelta(days=90)

POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <groupId>g</groupId>
    <artifactId>app</artifactId>
    <version>1</version>
    <properties>
        <jackson.version>2.18.2</jackson.version>
    </properties>
    <dependencies>
        <dependency>
            <groupId>com.fasterxml.jackson.core</groupId>
            <artifactId>jackson-core</artifactId>
            <version>${jackson.version}</version>
        </dependency>
        <dependency>
            <groupId>com.fasterxml.jackson.core</groupId>
            <artifactId>jackson-databind</artifactId>
            <version>${jackson.version}</version>
        </dependency>
        <dependency>
            <groupId>o</groupId>
            <artifactId>lib</artifactId>
            <version>1.0.0</version>
        </dependency>
        <dependency>
            <groupId>o</groupId>
            <artifactId>clean</artifactId>
            <version>1.0.0</version>
        </dependency>
    </dependencies>
</project>
"""

# coordinate -> [(advisory id, first fixed version, rating)]
ADVISORIES = {
    "com.fasterxml.jackson.core:jackson-core": [("GHSA-core", "2.18.8", SeverityRating.HIGH)],
    "com.fasterxml.jackson.core:jackson-databind": [("GHSA-db", "2.18.9", SeverityRating.MEDIUM)],
    "o:lib": [("CVE-lib", "1.0.2", SeverityRating.CRITICAL)],
    "o:deep": [("GHSA-deep", "3.1", SeverityRating.LOW)],
    "o:major": [("GHSA-major", "3.0.1", SeverityRating.HIGH)],
}
RELEASES = {
    "com.fasterxml.jackson.core:jackson-core": ["2.18.2", "2.18.7", "2.18.8", "2.18.9", "2.19.0"],
    "com.fasterxml.jackson.core:jackson-databind": ["2.18.2", "2.18.8", "2.18.9", "2.19.0"],
    "o:lib": ["1.0.0", "1.0.1", "1.0.2", "1.0.3", "1.1.0"],
    "o:clean": ["1.0.0", "1.0.1"],
    "o:deep": ["3.0", "3.1", "3.2"],
    "o:major": ["2.0", "3.0.1"],
}


class Advisories:
    snapshot_id = "osv-fake"

    def affecting(self, coordinate, version):
        return [Finding(a, (f"CVE-{a}",), Severity(r, SeveritySource.LABEL), coordinate, version, "0", fixed)
                for a, fixed, r in ADVISORIES.get(str(coordinate), []) if ComparableVersion(version) < ComparableVersion(fixed)]  # fmt: skip


def dep(coordinate, version, via=()):
    return Dependency(Coordinate.parse(coordinate), version, "compile", "jar", "", False, tuple(Coordinate.parse(v) for v in via))


def options(major="disallowed", strategy="conservative", scope="cve"):
    return PlanningSettings(7, 60, 120, strategy, scope, major, 7, "disallowed")


@pytest.fixture
def world(tmp_path):
    pom = tmp_path / "pom.xml"
    pom.write_bytes(POM.encode())
    declarations = read_declarations([pom])
    tree = ModuleTree(Coordinate.parse("g:app"), "1", (
        dep("com.fasterxml.jackson.core:jackson-core", "2.18.2"), dep("com.fasterxml.jackson.core:jackson-databind", "2.18.2"),
        dep("o:lib", "1.0.0"), dep("o:clean", "1.0.0"), dep("o:deep", "3.0", via=["o:lib"]), dep("o:major", "2.0", via=["o:lib"])))  # fmt: skip
    return pom, declarations, tree


def analysis(world, opts=None, release_overrides=None, units=()):
    pom, declarations, tree = world
    exposure = resolve_exposure([tree], Advisories())
    releases = {c: None if vs is None else [VersionRelease(v, OLD) for v in vs] for c, vs in {**RELEASES, **(release_overrides or {})}.items()}
    available = {d.coordinate: releases.get(str(d.coordinate)) for d in exposure.dependencies}
    plans = [plan_dependency(d, declarations.for_coordinate(d.coordinate), available[d.coordinate], Advisories(), opts or options(), NOW)
             for d in exposure.dependencies]  # fmt: skip
    return Analysis([tree], declarations, exposure, plans, list(units), available)


def by_key(proposals):
    return {p.ladder.key: p for p in proposals}


def test_only_cve_affected_dependencies_get_ladders_worst_first(world):
    proposals = dependency_proposals(analysis(world), options(), world[0])
    assert [p.ladder.key for p in proposals] == [
        "dep:o:lib@1.0.0", "dep:com.fasterxml.jackson.core:jackson-core+com.fasterxml.jackson.core:jackson-databind@2.18.2",
        "dep:o:major@2.0", "dep:o:deep@3.0"]  # fmt: skip
    assert [p.ladder.order for p in proposals] == [0, 1, 2, 3]
    assert not any("clean" in p.ladder.key for p in proposals)


def test_a_declared_literal_dependency_gets_its_ladder_of_version_edits(world):
    pom = world[0]
    proposal = by_key(dependency_proposals(analysis(world), options(), pom))["dep:o:lib@1.0.0"]
    steps = proposal.ladder.steps
    assert [(s.kind, s.rank, s.label) for s in steps] == [
        ("cve_patch", 0, "o:lib 1.0.0 → 1.0.2 (cve_patch)"), ("cve_minor", 1, "o:lib 1.0.0 → 1.1.0 (cve_minor)")]  # fmt: skip
    first = proposal.moves[0].changes
    assert len(first) == 1 and isinstance(first[0], SetVersion)
    assert (first[0].expected, first[0].version, first[0].site.pom) == ("1.0.0", "1.0.2", pom)
    assert proposal.moves[0].clears == ("CVE-lib",) and proposal.ladder.note == ""


def test_dependencies_sharing_a_property_are_one_ladder_with_one_edit(world):
    proposal = by_key(dependency_proposals(analysis(world), options(), world[0]))[
        "dep:com.fasterxml.jackson.core:jackson-core+com.fasterxml.jackson.core:jackson-databind@2.18.2"]
    # core is fixed at 2.18.8 and databind at 2.18.9: only versions that clear both are steps
    assert [(s.kind, s.label) for s in proposal.ladder.steps] == [
        ("cve_patch", "jackson-core+jackson-databind 2.18.2 → 2.18.9 (cve_patch)"), ("cve_minor", "jackson-core+jackson-databind 2.18.2 → 2.19.0 (cve_minor)")]  # fmt: skip
    (edit,) = proposal.moves[0].changes
    assert (edit.site.kind, edit.site.name, edit.expected, edit.version) == ("property", "jackson.version", "2.18.2", "2.18.9")
    assert proposal.moves[0].clears == ("GHSA-core", "GHSA-db") and set(proposal.members) == {
        "com.fasterxml.jackson.core:jackson-core", "com.fasterxml.jackson.core:jackson-databind"}  # fmt: skip


def test_a_transitive_dependency_is_fixed_by_a_pin_in_the_root_pom_with_a_note(world):
    proposal = by_key(dependency_proposals(analysis(world), options(), world[0]))["dep:o:deep@3.0"]
    (pin,) = proposal.moves[0].changes
    assert isinstance(pin, AddPin) and pin.pom == world[0] and (str(pin.coordinate), pin.version) == ("o:deep", "3.1")
    assert pin.note == "CVE-GHSA-deep; re-evaluate on a new release"


def test_a_major_only_fix_has_no_steps_and_says_why(world):
    proposal = by_key(dependency_proposals(analysis(world), options(), world[0]))["dep:o:major@2.0"]
    assert proposal.ladder.steps == () and proposal.moves == ()
    assert proposal.ladder.note == "the only fix is a major update (3.0.1); major update: major_updates is disallowed"
    allowed = by_key(dependency_proposals(analysis(world, options("allowed")), options("allowed"), world[0]))["dep:o:major@2.0"]
    assert [s.kind for s in allowed.ladder.steps] == ["cve_major"] and allowed.ladder.note == ""


def test_notes_for_missing_metadata_and_for_no_fixing_version(world):
    notes = {p.ladder.key: p.ladder.note for p in dependency_proposals(analysis(world, release_overrides={"o:lib": None}), options(), world[0])}
    assert notes["dep:o:lib@1.0.0"] == "no Central metadata for o:lib; run `giml sync --central --coordinate o:lib`"
    stuck = {p.ladder.key: p.ladder.note for p in dependency_proposals(analysis(world, release_overrides={"o:lib": ["1.0.0", "1.0.1"]}), options(), world[0])}
    assert stuck["dep:o:lib@1.0.0"] == "no version that clears it is available"


def test_a_version_giml_cannot_edit_gets_a_ladder_with_no_steps(tmp_path):
    text = POM.replace("<version>1.0.0</version>\n        </dependency>\n        <dependency>\n            <groupId>o</groupId>\n            <artifactId>clean",
                       "<version>${a}.${b}</version>\n        </dependency>\n        <dependency>\n            <groupId>o</groupId>\n            <artifactId>clean", 1)
    pom = tmp_path / "pom.xml"
    pom.write_bytes(text.encode())
    declarations = read_declarations([pom])
    assert declarations.for_coordinate(Coordinate.parse("o:lib"))[0].origin == "complex"
    tree = ModuleTree(Coordinate.parse("g:app"), "1", (dep("o:lib", "1.0.0"),))
    exposure = resolve_exposure([tree], Advisories())
    plans = [plan_dependency(exposure.dependencies[0], declarations.for_coordinate(Coordinate.parse("o:lib")),
                             [VersionRelease(v, OLD) for v in RELEASES["o:lib"]], Advisories(), options(), NOW)]  # fmt: skip
    (proposal,) = dependency_proposals(Analysis([tree], declarations, exposure, plans, [], {}), options(), pom)
    assert proposal.ladder.steps == () and proposal.ladder.note == "giml cannot edit where this version comes from"


def test_a_group_with_no_version_clearing_every_member_is_explained(world):
    limited = {"com.fasterxml.jackson.core:jackson-databind": ["2.18.2", "2.19.0"]}  # databind has no 2.18.x fix: the shared property cannot satisfy both
    proposal = by_key(dependency_proposals(analysis(world, release_overrides=limited), options(), world[0]))[
        "dep:com.fasterxml.jackson.core:jackson-core+com.fasterxml.jackson.core:jackson-databind@2.18.2"]
    assert [s.kind for s in proposal.ladder.steps] == ["cve_minor"]  # only the minor level clears both
    none = {"com.fasterxml.jackson.core:jackson-databind": ["2.18.2"]}
    empty = by_key(dependency_proposals(analysis(world, release_overrides=none), options(), world[0]))[
        "dep:com.fasterxml.jackson.core:jackson-core+com.fasterxml.jackson.core:jackson-databind@2.18.2"]
    assert empty.ladder.steps == () and "no version clears every dependency" in empty.ladder.note


def test_only_declarations_at_the_resolved_version_are_edited(tmp_path):
    other = POM.replace("<version>1.0.0</version>\n        </dependency>\n        <dependency>\n            <groupId>o</groupId>\n            <artifactId>clean</artifactId>",
                        "<version>1.0.5</version>\n        </dependency>\n        <dependency>\n            <groupId>o</groupId>\n            <artifactId>clean</artifactId>", 1)
    pom = tmp_path / "pom.xml"
    pom.write_bytes(other.encode())
    declarations = read_declarations([pom])
    assert [d.version for d in declarations.for_coordinate(Coordinate.parse("o:lib"))] == ["1.0.5"]
    tree = ModuleTree(Coordinate.parse("g:app"), "1", (dep("o:lib", "1.0.0"),))  # resolves to 1.0.0 although declared 1.0.5 (management, mediation)
    exposure = resolve_exposure([tree], Advisories())
    plans = [plan_dependency(exposure.dependencies[0], declarations.for_coordinate(Coordinate.parse("o:lib")),
                             [VersionRelease(v, OLD) for v in RELEASES["o:lib"]], Advisories(), options(), NOW)]  # fmt: skip
    (proposal,) = dependency_proposals(Analysis([tree], declarations, exposure, plans, [], {}), options(), pom)
    assert proposal.ladder.steps and isinstance(proposal.moves[0].changes[0], AddPin)  # no declaration matches what resolved, so it is pinned


def test_the_ladder_keys_and_order_are_deterministic(world):
    first = dependency_proposals(analysis(world), options(), world[0])
    second = dependency_proposals(analysis(world), options(), world[0])
    assert [(p.ladder.key, p.ladder.order, p.ladder.steps) for p in first] == [(p.ladder.key, p.ladder.order, p.ladder.steps) for p in second]


# parent and BOM units -----------------------------------------------------------------------------------------------


def unit(world, version="3.3.5"):
    pom = world[0]
    text = pom.read_text()
    start = text.index("2.18.2")  # any editable text will do for a site
    return ChangeUnit("parent", Coordinate.parse("org.boot:starter-parent"), "2.18.2", Site(pom, 6, (start, start + 6), "property", "jackson.version"))


def evaluation(version, level, total, cleared=(), blocked=None):
    from giml.plan.exposure import Exposure

    return Evaluation(version, level, OLD, Exposure(SeverityRating.HIGH if total else None, 1 if total else 0, total), tuple(cleared), (), 3, blocked, None)


def unit_plan(u, status="improves", evaluations=(), picks=()):
    from giml.plan.exposure import Exposure

    return UnitPlan(u, status, Exposure(SeverityRating.HIGH, 2, 5), tuple(evaluations), tuple(Pick(k, v) for k, v in picks), (), False, 0)


def test_a_parent_unit_becomes_a_ladder_starting_with_its_smallest_best_step(world):
    u = unit(world)
    evals = [evaluation("2.18.7", "patch", 3, ["a"]), evaluation("2.18.9", "patch", 1, ["a", "b", "c"]), evaluation("2.19.0", "minor", 1, ["a", "b", "c"]),
             evaluation("2.20.0", "minor", 4, ["a"]), evaluation("3.0.0", "major", 0, list("abcde"), blocked="major update: x")]  # fmt: skip
    (proposal,) = unit_proposals(analysis(world, units=[unit_plan(u, evaluations=evals, picks=[("parent_patch", "2.18.9")])]))
    assert proposal.ladder.key == "unit:org.boot:starter-parent@2.18.2" and proposal.kind == "unit"
    assert [(s.kind, s.label) for s in proposal.ladder.steps] == [
        ("parent_patch", "parent org.boot:starter-parent 2.18.2 → 2.18.9 (parent_patch)"),
        ("parent_minor", "parent org.boot:starter-parent 2.18.2 → 2.19.0 (parent_minor)")]  # fmt: skip
    (edit,) = proposal.moves[0].changes
    assert isinstance(edit, SetVersion) and (edit.expected, edit.version) == ("2.18.2", "2.18.9")
    assert proposal.moves[0].clears == ("a", "b", "c") and proposal.ladder.order == 0


def test_units_that_do_not_improve_or_were_not_evaluated_have_no_ladder(world):
    u = unit(world)
    plans = [unit_plan(u, "no_improvement"), unit_plan(u, "blocked_major"), unit_plan(u, "no_metadata"), unit_plan(u, "not_evaluated")]
    assert unit_proposals(analysis(world, units=plans)) == []


def test_an_edit_whose_file_has_changed_underneath_is_caught_when_applied(world):
    from giml.maven.pom_change import ChangeError, apply_changes

    proposal = by_key(dependency_proposals(analysis(world), options(), world[0]))["dep:o:lib@1.0.0"]
    world[0].write_text(world[0].read_text().replace("<version>1.0.0</version>\n        </dependency>\n        <dependency>\n            <groupId>o</groupId>\n            <artifactId>clean", "<version>9.9.9</version>\n        </dependency>\n        <dependency>\n            <groupId>o</groupId>\n            <artifactId>clean"))
    with pytest.raises(ChangeError):
        apply_changes(list(proposal.moves[0].changes))


def test_general_scope_adds_one_step_updates_after_the_cve_ladders(world):
    proposals = dependency_proposals(analysis(world, options(scope="general"), release_overrides={"o:clean": ["1.0.0", "1.0.1"]}),
                                     options(scope="general"), world[0])
    keys = [p.ladder.key for p in proposals]
    assert keys[-1] == "upd:o:clean@1.0.0" and keys[:-1] == [k for k in keys if k.startswith("dep:")]
    last = proposals[-1]
    assert [s.kind for s in last.ladder.steps] == ["next_patch"] and last.moves[0].changes[0].version == "1.0.1"
    assert last.ladder.order == len(proposals) - 1


def test_cve_scope_leaves_other_dependencies_alone(world):
    assert not any(p.ladder.key.startswith("upd:") for p in dependency_proposals(analysis(world), options(), world[0]))


def convergence(subject, *versions):
    from giml.maven.enforcer import Violation

    return Violation("DependencyConvergence", subject, {"versions": list(versions)})


def test_a_convergence_conflict_is_aligned_on_its_highest_version(world):
    from giml.plan.steps import enforcer_proposals

    declared, undeclared, single, other = (convergence("o:lib", "1.0.0", "1.0.2"), convergence("o:deep", "3.0", "3.1"),
                                           convergence("o:lib", "1.0.0"), Violation_of("BanDuplicateClasses", "a + b"))
    proposals = by_key(enforcer_proposals([declared, undeclared, single, other], analysis(world), world[0]))
    assert set(proposals) == {"enf:o:lib", "enf:o:deep"}
    (edit,) = proposals["enf:o:lib"].moves[0].changes
    assert isinstance(edit, SetVersion) and (edit.expected, edit.version) == ("1.0.0", "1.0.2")
    (pin,) = proposals["enf:o:deep"].moves[0].changes
    assert isinstance(pin, AddPin) and pin.version == "3.1" and "aligns DependencyConvergence:o:deep" in pin.note
    assert proposals["enf:o:lib"].resolves == (declared.identity,) and proposals["enf:o:lib"].kind == "enforcer"


def Violation_of(rule, subject):
    from giml.maven.enforcer import Violation

    return Violation(rule, subject, {})


def test_latest_gives_every_same_major_version_from_the_lowest_fix_upwards_newest_wanted(world):
    opts = options(strategy="latest")
    proposals = by_key(dependency_proposals(analysis(world, opts), opts, world[0], NOW))
    lib = proposals["dep:o:lib@1.0.0"]
    assert lib.ladder.highest and [s.label.split(" → ")[1].split(" ")[0] for s in lib.ladder.steps] == ["1.0.2", "1.0.3", "1.1.0"]
    assert {s.kind for s in lib.ladder.steps} == {"latest_in_major"}
    assert [m.changes[0].version for m in lib.moves] == ["1.0.2", "1.0.3", "1.1.0"]


def test_latest_falls_back_to_the_conservative_steps_when_the_fix_needs_a_major(world):
    opts = options(strategy="latest")
    major = by_key(dependency_proposals(analysis(world, opts), opts, world[0], NOW))["dep:o:major@2.0"]
    assert not major.ladder.highest and major.ladder.steps == ()  # the only fix is a major, which is gated


def test_latest_general_scope_offers_the_newest_versions_of_other_dependencies(world):
    opts = options(strategy="latest", scope="general")
    clean = by_key(dependency_proposals(analysis(world, opts), opts, world[0], NOW))["upd:o:clean@1.0.0"]
    assert clean.ladder.highest and [m.changes[0].version for m in clean.moves] == ["1.0.1"]


def test_conservative_ladders_are_not_bisected(world):
    proposals = dependency_proposals(analysis(world), options(), world[0], NOW)
    assert not any(p.ladder.highest for p in proposals)


def with_version_advisories(base, table):
    import dataclasses

    return dataclasses.replace(base, version_advisories={(Coordinate.parse(c), v): frozenset(ids) for (c, v), ids in table.items()})


def test_latest_skips_versions_that_carry_advisories_the_fix_does_not(world):
    opts = options(strategy="latest")
    table = {("o:lib", "1.0.2"): [], ("o:lib", "1.0.3"): [], ("o:lib", "1.1.0"): ["GHSA-worse"]}
    proposals = by_key(dependency_proposals(with_version_advisories(analysis(world, opts), table), opts, world[0], NOW))
    assert [m.changes[0].version for m in proposals["dep:o:lib@1.0.0"].moves] == ["1.0.2", "1.0.3"]


def test_latest_keeps_a_clean_dependency_clean(world):
    opts = options(strategy="latest", scope="general")
    table = {("o:clean", "1.0.1"): ["GHSA-new"]}
    proposals = by_key(dependency_proposals(with_version_advisories(analysis(world, opts), table), opts, world[0], NOW))
    assert "upd:o:clean@1.0.0" not in proposals
