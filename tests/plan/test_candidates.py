import datetime
from pathlib import Path

import pytest

from giml.core.config import PlanningSettings
from giml.core.model import Coordinate, Finding, Severity, SeverityRating, SeveritySource, VersionRelease
from giml.maven.declarations import BUILTIN, EXTERNAL, LITERAL, MANAGED, PROPERTY, Declaration, Site
from giml.maven.version import ComparableVersion
from giml.plan.candidates import is_prerelease, level, plan_dependency
from giml.plan.exposure import ResolvedDependency

NOW = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.UTC)
OLD = NOW - datetime.timedelta(days=90)
LIB = Coordinate.parse("o:lib")


def settings(strategy="conservative", scope="cve", major_updates="disallowed", cooldown=7) -> PlanningSettings:
    return PlanningSettings(cooldown, 60, 120, strategy, scope, major_updates)


class FakeAdvisories:
    """Advisories that affect every version below their fixed version (Maven ordering)."""

    snapshot_id = "osv-fake"

    def __init__(self, **fixed_in: str):
        self.fixed_in = fixed_in  # advisory id -> first fixed version
        self.queries: list[str] = []

    def affecting(self, coordinate, version):
        self.queries.append(version)
        return [Finding(a, (f"CVE-{a}",), Severity(SeverityRating.HIGH, SeveritySource.LABEL), coordinate, version, "0", fixed)
                for a, fixed in self.fixed_in.items() if ComparableVersion(version) < ComparableVersion(fixed)]  # fmt: skip


def releases(*versions, date=OLD) -> list[VersionRelease]:
    return sorted((VersionRelease(v, date) for v in versions), key=lambda r: ComparableVersion(r.version))


def resolved(version: str, advisories: FakeAdvisories) -> ResolvedDependency:
    return ResolvedDependency(LIB, version, (Coordinate.parse("g:m"),), ("compile",), True,
                              tuple(advisories.affecting(LIB, version)))  # fmt: skip


def declaration(origin=LITERAL, with_site=True) -> Declaration:
    site = Site(Path("pom.xml"), 3, (10, 15), "version") if with_site else None
    return Declaration(LIB, Path("pom.xml"), "dependencies", None, None, False, "1", "1", origin, site)


def plan(version, advisories, available, options=None, declarations=None, now=NOW):
    dependency = resolved(version, advisories)
    return plan_dependency(dependency, declarations if declarations is not None else [declaration()], available,
                           advisories, options or settings(), now)  # fmt: skip


def kinds(result) -> list[tuple[str, str]]:
    return [(k, c.version) for c in result.candidates for k in c.kinds]


@pytest.mark.parametrize(("current", "candidate", "expected"), [
    ("2.17.1", "2.17.3", "patch"), ("2.17.1", "2.18.0", "minor"), ("2.17.1", "3.0.0", "major"),
    ("1.6.3", "1.6.3.1", "patch"), ("3.1.5.RELEASE", "3.1.6.RELEASE", "patch"), ("1", "1.1", "minor"),
    ("1.2", "1.2.1", "patch"), ("5.4.3.Final", "6.0.0.Final", "major"), ("2024.01", "2025.01", "major"),
])  # fmt: skip
def test_level(current, candidate, expected):
    assert level(current, candidate) == expected


@pytest.mark.parametrize(("version", "expected"), [
    ("1.0.0", False), ("1.0.0-rc1", True), ("1.0.0.RELEASE", False), ("2.0.0-alpha.3", True), ("2.0.0-M1", True),
    ("1.0-SNAPSHOT", True), ("1.0-SP1", False), ("3.1.5.Final", False), ("4.0.0-beta-2", True), ("20040616", False),
    ("1.0.0-CR1", True), ("1.2.3-jre", False),
])  # fmt: skip
def test_is_prerelease(version, expected):
    assert is_prerelease(version) is expected


def test_conservative_cve_ladder_lists_patch_minor_and_major_in_order():
    advisories = FakeAdvisories(A="2.17.3")
    result = plan("2.17.1", advisories, releases("2.17.2", "2.17.3", "2.17.4", "2.18.0", "2.19.1", "3.0.0", "3.0.1"),
                  settings(major_updates="allowed"))  # fmt: skip
    assert kinds(result) == [("cve_patch", "2.17.3"), ("cve_minor", "2.18.0"), ("cve_major", "3.0.0")]
    assert [c.clears for c in result.candidates] == [True, True, True]
    assert (result.cve_affected, result.advisories, result.status) == (True, ("A",), "fix_available")


def test_each_ladder_step_is_the_lowest_version_that_clears():
    advisories = FakeAdvisories(A="2.17.3", B="2.18.1")
    result = plan("2.17.1", advisories, releases("2.17.2", "2.17.3", "2.17.9", "2.18.0", "2.18.1", "2.18.2"))
    # Nothing in 2.17.x clears B, so there is no cve_patch; 2.18.0 still has B, so the minor step is 2.18.1.
    assert kinds(result) == [("cve_minor", "2.18.1")]


def test_a_candidate_must_clear_every_advisory_of_the_current_version():
    advisories = FakeAdvisories(A="1.0.2", B="1.0.4")
    assert kinds(plan("1.0.0", advisories, releases("1.0.1", "1.0.2", "1.0.3", "1.0.4"))) == [("cve_patch", "1.0.4")]


def test_major_only_fix_is_blocked_by_default_and_reported():
    advisories = FakeAdvisories(A="3.0.1")
    result = plan("2.17.1", advisories, releases("2.17.2", "2.18.0", "3.0.0", "3.0.1"))
    (candidate,) = result.candidates
    assert (candidate.kinds, candidate.version, candidate.clears) == (("cve_major",), "3.0.1", True)
    assert candidate.blocked == "major update: major_updates is disallowed"
    assert result.status == "fix_blocked_major"


@pytest.mark.parametrize(("mode", "blocked", "status"), [
    ("disallowed", "major update: major_updates is disallowed", "fix_blocked_major"),
    ("ml", "major update: major_updates is ml and there is no ML evidence", "fix_blocked_major"),
    ("allowed", None, "fix_available"),
])  # fmt: skip
def test_major_update_modes(mode, blocked, status):
    result = plan("2.17.1", FakeAdvisories(A="3.0.1"), releases("3.0.1"), settings(major_updates=mode))
    assert (result.candidates[0].blocked, result.status) == (blocked, status)


def test_no_fixing_version_available():
    result = plan("1.0.0", FakeAdvisories(A="9.0.0"), releases("1.0.1", "1.1.0", "2.0.0"))
    assert result.candidates == () and result.status == "no_fix"


def test_versions_in_cooldown_are_skipped_and_named():
    fresh = NOW - datetime.timedelta(days=2)
    available = releases("1.0.1", "1.0.2", date=OLD)[:1] + [VersionRelease("1.0.2", fresh), VersionRelease("1.0.3", fresh)]
    result = plan("1.0.0", FakeAdvisories(A="1.0.2"), available)
    assert result.candidates == () and result.held_by_cooldown == ("1.0.2", "1.0.3") and result.status == "no_fix"
    assert plan("1.0.0", FakeAdvisories(A="1.0.2"), available, settings(cooldown=1)).candidates[0].version == "1.0.2"
    assert plan("1.0.0", FakeAdvisories(A="1.0.2"), available, settings(cooldown=0)).candidates[0].version == "1.0.2"


def test_a_version_older_than_the_cooldown_is_not_held():
    just_old = NOW - datetime.timedelta(days=7)
    result = plan("1.0.0", FakeAdvisories(A="1.0.1"), [VersionRelease("1.0.1", just_old)])
    assert result.candidates[0].version == "1.0.1" and result.held_by_cooldown == ()


def test_unknown_release_date_is_allowed_but_flagged():
    result = plan("1.0.0", FakeAdvisories(A="1.0.1"), [VersionRelease("1.0.1", None)])
    assert result.candidates[0].date_unknown is True and result.candidates[0].released_at is None
    assert plan("1.0.0", FakeAdvisories(A="1.0.1"), releases("1.0.1")).candidates[0].date_unknown is False


def test_prereleases_are_never_candidates_unless_current_is_one():
    advisories = FakeAdvisories(A="1.0.1")
    assert plan("1.0.0", advisories, releases("1.0.1-rc1", "1.0.1")).candidates[0].version == "1.0.1"
    assert plan("1.0.0", advisories, releases("1.0.1-rc1")).candidates == ()
    assert plan("1.0.0-rc1", FakeAdvisories(A="1.0.0-rc2"), releases("1.0.0-rc2")).candidates[0].version == "1.0.0-rc2"


def test_only_newer_versions_are_candidates():
    assert plan("1.5.0", FakeAdvisories(A="1.5.1"), releases("1.0.0", "1.4.0", "1.5.0", "1.5.1")).candidates[0].version == "1.5.1"


def test_a_fix_that_brings_new_advisories_is_reported():
    class Advisories(FakeAdvisories):
        def affecting(self, coordinate, version):
            found = super().affecting(coordinate, version)
            if version == "1.0.1":
                found.append(Finding("NEW", (), Severity(SeverityRating.LOW, SeveritySource.LABEL), coordinate, version, "1.0.1", None))
            return found

    result = plan("1.0.0", Advisories(A="1.0.1"), releases("1.0.1"))
    assert result.candidates[0].clears is True and result.candidates[0].new_advisories == ("NEW",)


def test_candidates_that_do_not_clear_are_not_listed():
    advisories = FakeAdvisories(A="2.0.0")
    result = plan("1.0.0", advisories, releases("1.0.1", "1.1.0", "2.0.0"), settings(major_updates="allowed"))
    assert kinds(result) == [("cve_major", "2.0.0")]


def test_not_cve_affected_stays_put_under_scope_cve():
    result = plan("1.0.0", FakeAdvisories(), releases("1.0.1", "1.1.0"))
    assert (result.cve_affected, result.candidates, result.status) == (False, (), "unchanged_scope")


def test_scope_general_updates_to_the_next_patch_else_next_minor():
    options = settings(scope="general")
    result = plan("1.0.0", FakeAdvisories(), releases("1.0.1", "1.0.2", "1.1.0", "2.0.0"), options)
    assert kinds(result) == [("next_patch", "1.0.1")] and result.status == "update_available"
    result = plan("1.0.2", FakeAdvisories(), releases("1.1.0", "1.1.1", "1.2.0", "2.0.0"), options)
    assert kinds(result) == [("next_minor", "1.1.0")]
    assert plan("1.0.0", FakeAdvisories(), releases("2.0.0"), options).candidates == ()
    assert plan("1.0.0", FakeAdvisories(), releases("2.0.0"), options).status == "up_to_date"


def test_general_updates_never_cross_a_major_under_conservative():
    result = plan("1.9.9", FakeAdvisories(), releases("2.0.0"), settings(scope="general", major_updates="allowed"))
    assert result.candidates == ()


def test_latest_strategy_lists_the_aspirational_candidate_first_and_the_ladder_below_it():
    advisories = FakeAdvisories(A="2.17.3")
    result = plan("2.17.1", advisories, releases("2.17.3", "2.18.0", "2.19.4", "3.1.0"),
                  settings(strategy="latest", major_updates="allowed"))  # fmt: skip
    # Newest first, the order a failing candidate is demoted in; steps on the same version merge.
    assert [(c.version, c.kinds) for c in result.candidates] == [
        ("3.1.0", ("latest", "cve_major")), ("2.19.4", ("latest_in_major",)), ("2.18.0", ("cve_minor",)),
        ("2.17.3", ("cve_patch",))]  # fmt: skip
    assert [c.clears for c in result.candidates] == [True, True, True, True]


def test_latest_strategy_merges_kinds_that_share_a_version():
    result = plan("2.17.1", FakeAdvisories(A="2.17.3"), releases("2.17.3", "2.17.5"), settings(strategy="latest"))
    assert [c.kinds for c in result.candidates] == [("latest", "latest_in_major"), ("cve_patch",)]
    assert [c.version for c in result.candidates] == ["2.17.5", "2.17.3"]


def test_latest_strategy_blocks_a_major_latest_by_default():
    result = plan("2.17.1", FakeAdvisories(A="2.17.3"), releases("2.17.3", "2.18.0", "3.0.0"), settings(strategy="latest"))
    assert [(c.kinds, c.version, c.blocked is not None) for c in result.candidates] == [
        (("latest", "cve_major"), "3.0.0", True), (("latest_in_major", "cve_minor"), "2.18.0", False),
        (("cve_patch",), "2.17.3", False)]  # fmt: skip


def test_latest_strategy_scope_general_updates_other_dependencies_to_the_newest():
    result = plan("1.0.0", FakeAdvisories(), releases("1.0.5", "1.4.0", "2.0.0"), settings(strategy="latest", scope="general"))
    assert [(c.kinds, c.version) for c in result.candidates] == [(("latest",), "2.0.0"), (("latest_in_major",), "1.4.0")]
    assert result.candidates[0].blocked and not result.candidates[1].blocked


def test_latest_strategy_scope_cve_leaves_other_dependencies_alone():
    assert plan("1.0.0", FakeAdvisories(), releases("1.4.0"), settings(strategy="latest")).candidates == ()


def test_no_metadata_for_the_artifact():
    result = plan("1.0.0", FakeAdvisories(A="1.0.1"), None)
    assert (result.candidates, result.status) == ((), "no_metadata")


@pytest.mark.parametrize(("origin", "with_site", "change"), [
    (LITERAL, True, "edit"), (MANAGED, True, "edit"), (PROPERTY, True, "edit"),
    (EXTERNAL, False, "pin"), (BUILTIN, False, "unsupported"),
])  # fmt: skip
def test_how_the_change_would_be_made(origin, with_site, change):
    result = plan("1.0.0", FakeAdvisories(), [], declarations=[declaration(origin, with_site)])
    assert result.change == change
    assert (len(result.sites) == 1) == (change == "edit")


def test_a_dependency_declared_nowhere_is_pinned():
    result = plan("1.0.0", FakeAdvisories(), [], declarations=[])
    assert (result.change, result.sites) == ("pin", ())


def test_every_declaration_site_is_listed_once():
    first, second = declaration(), declaration(MANAGED)
    assert len(plan("1.0.0", FakeAdvisories(), [], declarations=[first, second, first]).sites) == 1


def test_planning_is_deterministic_and_queries_only_what_it_needs():
    advisories = FakeAdvisories(A="1.0.2")
    available = releases("1.0.1", "1.0.2", "1.0.3", "1.1.0", "1.2.0")
    first = plan("1.0.0", advisories, available)
    assert first == plan("1.0.0", FakeAdvisories(A="1.0.2"), available)
    assert advisories.queries.count("1.2.0") == 0 and "1.0.1" in advisories.queries
