import datetime
from pathlib import Path

import pytest

from giml.core.config import PlanningSettings
from giml.core.model import Coordinate, Finding, Severity, SeverityRating, SeveritySource, VersionRelease
from giml.maven.declarations import Declaration, Declarations, ExternalParent, Site
from giml.maven.tree import ResolutionError
from giml.plan.exposure import ResolvedDependency, TreeExposure, exposure_of
from giml.plan.parents import ChangeUnit, change_units, plan_change_unit, with_version

NOW = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.UTC)
OLD = NOW - datetime.timedelta(days=90)
PARENT = Coordinate.parse("org.boot:starter-parent")
SITE = Site(Path("pom.xml"), 10, (0, 5), "version")
UNIT = ChangeUnit("parent", PARENT, "3.3.5", SITE)


def options(strategy="conservative", major_updates="disallowed", scope="cve", cooldown=7, test_scope="disallowed"):
    return PlanningSettings(cooldown, 60, 120, strategy, scope, major_updates, 7, test_scope)


def releases(*versions, date=OLD):
    return [VersionRelease(v, date) for v in versions]


def tree(**deps) -> TreeExposure:
    """{"o:lib": ("2.17.1", ["A", "B"])}: each advisory is HIGH unless it starts with 'm' (MEDIUM).

    A third item sets the scope (default compile): {"o:lib": ("2.17.1", [], "test")}.
    """
    resolved = []
    for name, (version, advisories, *scope) in deps.items():
        coordinate = Coordinate.parse(name)
        findings = tuple(Finding(a, (), Severity(SeverityRating.MEDIUM if a.startswith("m") else SeverityRating.HIGH,
                                                 SeveritySource.LABEL), coordinate, version, "0", None) for a in advisories)  # fmt: skip
        resolved.append(ResolvedDependency(coordinate, version, (Coordinate.parse("g:m"),), tuple(scope or ["compile"]), True, findings))
    return TreeExposure(tuple(resolved), exposure_of(f for d in resolved for f in d.findings), "osv-1")


CURRENT = tree(**{"o:lib": ("2.17.1", ["A", "B", "C"]), "o:other": ("1.0", [])})


class Resolver:
    def __init__(self, table: dict, failing=()):
        self.table, self.failing, self.calls = table, set(failing), []

    def __call__(self, version):
        self.calls.append(version)
        if version in self.failing:
            raise ResolutionError(f"dependency resolution failed for {version}")
        return self.table[version]


def plan(resolver, available, current=CURRENT, opts=None, unit=UNIT, **kw):
    return plan_change_unit(unit, available, current, resolver, opts or options(), NOW, **kw)


def picks(result):
    return [(p.kind, p.version) for p in result.picks]


TABLE = {
    "3.3.6": tree(**{"o:lib": ("2.17.1", ["A", "B", "C"])}),  # nothing changes
    "3.3.7": tree(**{"o:lib": ("2.17.2", ["B", "C"])}),  # clears A
    "3.4.0": tree(**{"o:lib": ("2.18.0", ["C"])}),  # clears A and B
    "3.4.1": tree(**{"o:lib": ("2.18.1", [])}),  # clears everything
    "3.5.0": tree(**{"o:lib": ("2.19.0", [])}),
}


def test_conservative_picks_the_smallest_step_that_reaches_the_best_exposure():
    resolver = Resolver(TABLE)
    result = plan(resolver, releases("3.3.6", "3.3.7", "3.4.0", "3.4.1", "3.5.0"))
    assert (result.status, picks(result)) == ("improves", [("parent_minor", "3.4.1")])
    assert resolver.calls == ["3.3.6", "3.3.7", "3.4.0", "3.4.1", "3.5.0"]
    by_version = {e.version: e for e in result.evaluations}
    assert by_version["3.3.7"].cleared == ("A",) and by_version["3.3.7"].introduced == ()
    assert by_version["3.4.1"].cleared == ("A", "B", "C") and by_version["3.4.1"].exposure.total == 0
    assert (by_version["3.3.6"].changed, by_version["3.3.7"].changed, by_version["3.3.6"].level) == (0, 1, "patch")
    assert by_version["3.4.0"].level == "minor" and result.current.total == 3 and not result.truncated


def test_a_patch_step_wins_when_it_already_reaches_the_best():
    table = {"3.3.6": tree(**{"o:lib": ("2.17.9", [])}), "3.3.7": tree(**{"o:lib": ("2.17.9", [])}),
             "3.4.0": tree(**{"o:lib": ("2.18.0", [])})}  # fmt: skip
    assert picks(plan(Resolver(table), releases("3.3.6", "3.3.7", "3.4.0"))) == [("parent_patch", "3.3.6")]


def test_partial_improvement_is_still_an_improvement_when_nothing_does_better():
    table = {"3.3.6": tree(**{"o:lib": ("2.17.2", ["B", "C"])}), "3.3.7": tree(**{"o:lib": ("2.17.3", ["C"])}),
             "3.3.8": tree(**{"o:lib": ("2.17.3", ["C"])})}  # fmt: skip
    result = plan(Resolver(table), releases("3.3.6", "3.3.7", "3.3.8"))
    assert (result.status, picks(result)) == ("improves", [("parent_patch", "3.3.7")])


def test_no_improvement():
    table = {"3.3.6": TABLE["3.3.6"], "3.3.7": tree(**{"o:lib": ("2.17.1", ["A", "B", "C", "D"])})}
    result = plan(Resolver(table), releases("3.3.6", "3.3.7"))
    assert (result.status, result.picks) == ("no_improvement", ())
    assert [e.introduced for e in result.evaluations] == [(), ("D",)]


def test_a_more_severe_advisory_outweighs_a_lower_count():
    table = {"3.3.6": tree(**{"o:lib": ("2.17.2", ["m1"])})}  # fewer, but the current worst is HIGH: MEDIUM is better
    assert picks(plan(Resolver(table), releases("3.3.6"))) == [("parent_patch", "3.3.6")]
    worse = {"3.3.6": tree(**{"o:lib": ("2.17.2", ["A", "B", "C", "D"])})}
    assert plan(Resolver(worse), releases("3.3.6")).status == "no_improvement"


def test_major_parent_versions_are_not_resolved_when_major_updates_are_disallowed():
    resolver = Resolver({"3.3.6": TABLE["3.3.6"]})
    result = plan(resolver, releases("3.3.6", "4.0.0", "4.0.1"))
    assert resolver.calls == ["3.3.6"] and result.skipped_majors == 2 and result.status == "no_improvement"
    assert [e.version for e in result.evaluations] == ["3.3.6"]


def test_major_parent_versions_are_evaluated_when_allowed():
    table = {"3.3.6": TABLE["3.3.6"], "4.0.0": tree(**{"o:lib": ("2.19.0", [])})}
    result = plan(Resolver(table), releases("3.3.6", "4.0.0"), opts=options(major_updates="allowed"))
    assert (result.status, picks(result), result.skipped_majors) == ("improves", [("parent_major", "4.0.0")], 0)


def test_a_dependency_changing_major_through_the_parent_is_blocked():
    table = {"3.3.6": tree(**{"o:lib": ("3.0.0", [])}), "3.3.7": tree(**{"o:lib": ("2.17.2", ["B", "C"])})}
    result = plan(Resolver(table), releases("3.3.6", "3.3.7"))
    blocked = result.evaluations[0]
    assert blocked.blocked == "major update: changes o:lib from 2.x to 3.x; major_updates is disallowed"
    assert blocked.exposure.total == 0  # it was resolved, so its effect is known
    assert (result.status, picks(result)) == ("improves", [("parent_patch", "3.3.7")])


def test_a_major_change_in_a_test_only_dependency_says_so_when_it_blocks():
    current = tree(**{"o:lib": ("2.17.1", ["A", "B", "C"]), "o:hamcrest": ("2.2", [], "test")})
    table = {"3.3.6": tree(**{"o:lib": ("2.17.3", []), "o:hamcrest": ("3.0", [], "test")})}
    result = plan(Resolver(table), releases("3.3.6"), current=current)
    assert result.evaluations[0].blocked == ("major update: changes o:hamcrest from 2.x to 3.x; major_updates is disallowed "
                                             "(test scope only; `major_updates_test_scope` is `disallowed`)")  # fmt: skip
    assert result.status == "blocked_major"


def test_a_test_only_major_change_passes_when_the_test_scope_is_allowed():
    current = tree(**{"o:lib": ("2.17.1", ["A", "B", "C"]), "o:hamcrest": ("2.2", [], "test")})
    table = {"3.3.6": tree(**{"o:lib": ("2.17.3", []), "o:hamcrest": ("3.0", [], "test")})}
    result = plan(Resolver(table), releases("3.3.6"), current=current, opts=options(test_scope="allowed"))
    assert (result.evaluations[0].blocked, result.status, picks(result)) == (None, "improves", [("parent_patch", "3.3.6")])
    assert result.evaluations[0].changed == 2


def test_only_test_only_dependencies_are_exempt_and_others_still_block():
    current = tree(**{"o:lib": ("2.17.1", ["A"]), "o:hamcrest": ("2.2", [], "test"), "o:jackson": ("2.0", [])})
    table = {"3.3.6": tree(**{"o:lib": ("2.17.3", []), "o:hamcrest": ("3.0", [], "test"), "o:jackson": ("3.0", [])})}
    result = plan(Resolver(table), releases("3.3.6"), current=current, opts=options(test_scope="allowed"))
    assert result.evaluations[0].blocked == "major update: changes o:jackson from 2.x to 3.x; major_updates is disallowed"


def test_a_dependency_that_is_not_test_only_in_both_trees_is_not_exempt():
    current = tree(**{"o:lib": ("2.17.1", ["A"]), "o:x": ("2.0", [])})
    table = {"3.3.6": tree(**{"o:lib": ("2.17.3", []), "o:x": ("3.0", [], "test")})}  # compile today, test after
    result = plan(Resolver(table), releases("3.3.6"), current=current, opts=options(test_scope="allowed"))
    assert result.evaluations[0].blocked.startswith("major update: changes o:x from 2.x to 3.x")


def test_the_ml_reason_still_names_the_missing_evidence_for_test_only_majors():
    current = tree(**{"o:lib": ("2.17.1", ["A"]), "o:h": ("2.0", [], "test")})
    table = {"3.3.6": tree(**{"o:lib": ("2.17.3", []), "o:h": ("3.0", [], "test")})}
    blocked = plan(Resolver(table), releases("3.3.6"), current=current, opts=options(major_updates="ml")).evaluations[0].blocked
    assert blocked == ("major update: changes o:h from 2.x to 3.x; major_updates is ml and there is no ML evidence "
                       "(test scope only; `major_updates_test_scope` is `disallowed`)")  # fmt: skip


def test_only_a_blocked_improvement_is_reported_as_blocked():
    table = {"3.3.6": tree(**{"o:lib": ("3.0.0", [])})}
    result = plan(Resolver(table), releases("3.3.6"))
    assert (result.status, result.picks) == ("blocked_major", ())
    ml = plan(Resolver(table), releases("3.3.6"), opts=options(major_updates="ml"))
    assert ml.evaluations[0].blocked == "major update: changes o:lib from 2.x to 3.x; major_updates is ml and there is no ML evidence"
    allowed = plan(Resolver(table), releases("3.3.6"), opts=options(major_updates="allowed"))
    assert (allowed.evaluations[0].blocked, allowed.status) == (None, "improves")


def test_new_and_removed_dependencies_are_not_major_changes():
    table = {"3.3.6": tree(**{"o:lib": ("2.17.2", ["B", "C"]), "o:new": ("9.0", [])})}
    assert plan(Resolver(table), releases("3.3.6")).evaluations[0].blocked is None
    table = {"3.3.6": tree(**{"o:other": ("1.0", [])})}  # o:lib disappears
    assert plan(Resolver(table), releases("3.3.6")).evaluations[0].blocked is None


def test_a_failed_resolution_is_recorded_and_skipped():
    resolver = Resolver({"3.3.7": TABLE["3.3.7"]}, failing={"3.3.6"})
    result = plan(resolver, releases("3.3.6", "3.3.7"))
    assert result.evaluations[0].failed == "dependency resolution failed for 3.3.6" and result.evaluations[0].exposure is None
    assert picks(result) == [("parent_patch", "3.3.7")]


def test_no_metadata():
    resolver = Resolver({})
    result = plan(resolver, None)
    assert (result.status, result.evaluations, resolver.calls) == ("no_metadata", (), [])


def test_nothing_is_evaluated_without_a_cve_to_fix():
    resolver = Resolver({})
    clean = tree(**{"o:lib": ("2.17.1", [])})
    result = plan(resolver, releases("3.3.6"), current=clean)
    assert (result.status, resolver.calls) == ("not_evaluated", []) and "no CVE" in result.note


def test_versions_in_cooldown_are_named_not_evaluated():
    fresh = [VersionRelease("3.3.6", OLD), VersionRelease("3.3.7", NOW - datetime.timedelta(days=1))]
    resolver = Resolver({"3.3.6": TABLE["3.3.6"]})
    result = plan(resolver, fresh)
    assert result.held_by_cooldown == ("3.3.7",) and resolver.calls == ["3.3.6"]


def test_prereleases_are_not_evaluated():
    resolver = Resolver({"3.3.6": TABLE["3.3.6"]})
    plan(resolver, releases("3.4.0-rc1", "3.3.6"))
    assert resolver.calls == ["3.3.6"]


def test_at_most_the_limit_of_versions_are_evaluated_nearest_first():
    resolver = Resolver({v: TABLE["3.3.6"] for v in ("3.3.6", "3.3.7", "3.3.8")})
    result = plan(resolver, releases("3.3.6", "3.3.7", "3.3.8", "3.3.9"), limit=3)
    assert resolver.calls == ["3.3.6", "3.3.7", "3.3.8"] and result.truncated is True
    assert plan(Resolver({v: TABLE["3.3.6"] for v in ("3.3.6", "3.3.7")}), releases("3.3.6", "3.3.7"), limit=2).truncated is False


def test_latest_strategy_aims_for_the_newest_and_keeps_the_conservative_pick_as_the_floor():
    resolver = Resolver(TABLE)
    result = plan(resolver, releases("3.3.6", "3.3.7", "3.4.0", "3.4.1", "3.5.0"), opts=options(strategy="latest"))
    assert picks(result) == [("parent_latest", "3.5.0"), ("parent_minor", "3.4.1")]


def test_latest_strategy_with_one_best_version_has_no_separate_floor():
    result = plan(Resolver({"3.3.6": TABLE["3.4.1"]}), releases("3.3.6"), opts=options(strategy="latest"))
    assert picks(result) == [("parent_patch", "3.3.6")]


def test_bom_units_use_their_own_kind_in_pick_names():
    unit = ChangeUnit("bom", Coordinate.parse("org.spring:bom"), "6.0.0", SITE)
    result = plan(Resolver({"6.0.1": TABLE["3.4.1"]}), releases("6.0.1"), unit=unit)
    assert picks(result) == [("bom_patch", "6.0.1")]


def test_planning_is_deterministic():
    available = releases("3.3.6", "3.3.7", "3.4.0", "3.4.1", "3.5.0")
    assert plan(Resolver(TABLE), available) == plan(Resolver(TABLE), list(reversed(available)))


# change units and editing ------------------------------------------------------------------------------------------

def declaration(coordinate, version, is_bom, site=SITE, origin="literal"):
    return Declaration(Coordinate.parse(coordinate), Path("pom.xml"), "dependencyManagement", None,
                       "import" if is_bom else None, is_bom, version, version, origin, site)  # fmt: skip


def test_change_units_are_external_parents_and_editable_boms():
    parent = ExternalParent(Path("pom.xml"), PARENT, "3.3.5", SITE)
    other_site = Site(Path("core/pom.xml"), 4, (1, 2), "property", "bom.version")
    declared = (declaration("org.spring:bom", "6.0.0", True, other_site), declaration("o:lib", "1", False),
                declaration("o:external-bom", "1", True, None, "external"),
                declaration("org.spring:bom", "6.0.0", True, other_site))  # fmt: skip
    units = change_units(Declarations(declared, (parent,), ()))
    assert [(u.kind, str(u.coordinate), u.version, u.site) for u in units] == [
        ("parent", "org.boot:starter-parent", "3.3.5", SITE), ("bom", "org.spring:bom", "6.0.0", other_site)]  # fmt: skip


def test_with_version_replaces_the_text_and_restores_it(tmp_path):
    pom = tmp_path / "pom.xml"
    pom.write_text("<version>3.3.5</version>", encoding="utf-8")
    site = Site(pom, 1, (9, 14), "version")
    with with_version(site, "3.4.0"):
        assert pom.read_text() == "<version>3.4.0</version>"
    assert pom.read_text() == "<version>3.3.5</version>"
    with pytest.raises(RuntimeError), with_version(site, "9.9.9"):
        raise RuntimeError("resolution blew up")
    assert pom.read_text() == "<version>3.3.5</version>"


def test_with_version_refuses_a_site_that_no_longer_matches(tmp_path):
    pom = tmp_path / "pom.xml"
    pom.write_text("<version>3.3.5</version>", encoding="utf-8")
    with pytest.raises(ValueError, match="does not hold 3.3.5"), with_version(Site(pom, 1, (10, 15), "version"), "3.4.0", expected="3.3.5"):
        pass
    assert pom.read_text() == "<version>3.3.5</version>"
