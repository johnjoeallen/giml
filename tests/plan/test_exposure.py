from pathlib import Path

from giml.core.model import Coordinate, Finding, Severity, SeverityRating, SeveritySource
from giml.maven.tree import Dependency, ModuleTree, parse_tree
from giml.plan.exposure import Exposure, exposure_of, resolve_exposure

TREES = Path(__file__).resolve().parents[1] / "fixtures" / "trees"


def c(text: str) -> Coordinate:
    return Coordinate.parse(text)


def finding(advisory: str, coordinate: str, version: str, rating=SeverityRating.HIGH, fixed="9.9") -> Finding:
    return Finding(advisory, (f"CVE-{advisory}",), Severity(rating, SeveritySource.LABEL), c(coordinate), version, "0", fixed)


class FakeAdvisories:
    """An AdvisorySource with a fixed answer per (coordinate, version)."""

    snapshot_id = "osv-fake"

    def __init__(self, *findings: Finding):
        self.findings = findings
        self.queries: list[tuple[str, str]] = []

    def affecting(self, coordinate, version):
        self.queries.append((str(coordinate), version))
        return [f for f in self.findings if f.coordinate == coordinate and f.version == version]


def dep(coordinate: str, version: str, scope="compile", via=()) -> Dependency:
    return Dependency(c(coordinate), version, scope, "jar", "", False, tuple(c(v) for v in via))


def module(name: str, *dependencies: Dependency) -> ModuleTree:
    return ModuleTree(c(f"g:{name}"), "1", tuple(dependencies))


def test_exposure_of_nothing_is_clean_and_beats_everything():
    clean = exposure_of([])
    assert clean == Exposure(None, 0, 0) and clean.key == (-1, 0, 0)
    assert clean.key < exposure_of([finding("A", "o:l", "1", SeverityRating.LOW)]).key


def test_an_advisory_is_one_vulnerability_however_many_artifacts_it_affects():
    findings = [finding("A", "o:one", "1"), finding("A", "o:two", "1"), finding("B", "o:one", "1", SeverityRating.LOW)]
    assert exposure_of(findings) == Exposure(SeverityRating.HIGH, 1, 2)


def test_exposure_orders_by_worst_severity_then_count_at_it_then_total():
    high1 = exposure_of([finding("A", "o:l", "1", SeverityRating.HIGH)])
    high2 = exposure_of([finding("A", "o:l", "1", SeverityRating.HIGH), finding("B", "o:l", "1", SeverityRating.HIGH)])
    high1_low = exposure_of([finding("A", "o:l", "1", SeverityRating.HIGH), finding("C", "o:l", "1", SeverityRating.LOW)])
    critical = exposure_of([finding("D", "o:l", "1", SeverityRating.CRITICAL)])
    assert critical.key > high2.key > high1_low.key > high1.key
    assert sorted([critical, high1, high2, high1_low], key=lambda e: e.key) == [high1, high1_low, high2, critical]


def test_unknown_severity_sorts_below_low_but_above_clean():
    unknown = exposure_of([finding("U", "o:l", "1", SeverityRating.UNKNOWN)])
    assert exposure_of([]).key < unknown.key < exposure_of([finding("L", "o:l", "1", SeverityRating.LOW)]).key


def test_vulnerable_dependency_is_found_with_its_findings_and_where_it_comes_from():
    trees = [module("core", dep("o:direct", "1.0"), dep("o:parent", "2.0"), dep("o:deep", "3.0", via=["o:parent"]))]
    advisories = FakeAdvisories(finding("A", "o:deep", "3.0"), finding("B", "o:deep", "3.0", SeverityRating.MEDIUM))
    result = resolve_exposure(trees, advisories)
    assert [str(d.coordinate) for d in result.dependencies] == ["o:deep", "o:direct", "o:parent"]
    deep = result.dependencies[0]
    assert (deep.version, deep.direct, deep.modules) == ("3.0", False, (c("g:core"),))
    assert [f.advisory_id for f in deep.findings] == ["A", "B"]
    assert result.vulnerable == (deep,) and result.exposure == Exposure(SeverityRating.HIGH, 1, 2)
    assert result.snapshot_id == "osv-fake"


def test_the_same_dependency_in_several_modules_is_queried_once_and_lists_them():
    trees = [module("b", dep("o:l", "1", scope="test")), module("a", dep("o:l", "1", scope="compile"))]
    advisories = FakeAdvisories()
    result = resolve_exposure(trees, advisories)
    (only,) = result.dependencies
    assert only.modules == (c("g:a"), c("g:b")) and only.scopes == ("compile", "test")
    assert advisories.queries == [("o:l", "1")]


def test_different_versions_in_different_modules_are_separate_entries_in_version_order():
    trees = [module("a", dep("o:l", "1.10")), module("b", dep("o:l", "1.9")), module("c", dep("o:l", "1.9.1"))]
    assert [d.version for d in resolve_exposure(trees, FakeAdvisories()).dependencies] == ["1.9", "1.9.1", "1.10"]


def test_direct_in_any_module_counts_as_direct():
    trees = [module("a", dep("o:l", "1")), module("b", dep("o:x", "1"), dep("o:l", "1", via=["o:x"]))]
    assert [(str(d.coordinate), d.direct) for d in resolve_exposure(trees, FakeAdvisories()).dependencies] == [
        ("o:l", True), ("o:x", True)]  # fmt: skip


def test_the_reactors_own_modules_are_not_dependencies():
    trees = [module("core"), module("app", dep("g:core", "1"), dep("o:l", "1"))]
    assert [str(d.coordinate) for d in resolve_exposure(trees, FakeAdvisories()).dependencies] == ["o:l"]


def test_result_does_not_depend_on_module_order():
    trees = [module("a", dep("o:x", "1"), dep("o:y", "2")), module("b", dep("o:y", "2", scope="test"))]
    assert resolve_exposure(trees, FakeAdvisories()) == resolve_exposure(list(reversed(trees)), FakeAdvisories())


def test_scopes_that_are_blank_are_left_out():
    (only,) = resolve_exposure([module("a", dep("o:l", "1", scope=""))], FakeAdvisories()).dependencies
    assert only.scopes == ()


def test_real_redkite_trees():
    core = parse_tree((TREES / "redkite-core.json").read_text())
    server = parse_tree((TREES / "redkite-server.json").read_text())
    advisories = FakeAdvisories(finding("GHSA-x", "org.thymeleaf:thymeleaf", "3.1.5.RELEASE", SeverityRating.CRITICAL))
    assert resolve_exposure([core], advisories).vulnerable == ()
    result = resolve_exposure([core, server], advisories)
    assert [str(d.coordinate) for d in result.vulnerable] == ["org.thymeleaf:thymeleaf"]
    assert result.exposure == Exposure(SeverityRating.CRITICAL, 1, 1)
    assert "com.redkite:red-kite-core" not in {str(d.coordinate) for d in result.dependencies}  # a sibling module
    ognl = next(d for d in result.dependencies if str(d.coordinate) == "ognl:ognl")
    assert (ognl.direct, ognl.modules) == (False, (server.coordinate,))


def tree_exposure(*findings):
    from giml.plan.exposure import ResolvedDependency, TreeExposure

    deps = tuple(ResolvedDependency(f.coordinate, f.version, (), (), True, (f,)) for f in findings)
    return TreeExposure(deps, exposure_of(findings), "osv-fake")


def test_a_candidate_is_worse_only_when_the_exposure_ordering_says_so():
    from giml.plan.exposure import worse_exposure

    base = tree_exposure(finding("A", "o:l", "1", SeverityRating.HIGH))
    assert worse_exposure(base, base) == ()
    assert worse_exposure(base, tree_exposure()) == ()  # a fix
    assert worse_exposure(base, tree_exposure(finding("B", "o:m", "1", SeverityRating.LOW))) == ()  # a swap for a lesser one
    more = worse_exposure(base, tree_exposure(finding("A", "o:l", "1"), finding("B", "o:m", "1")))
    assert more == ("exposure rose from HIGH x1 (1 in all) to HIGH x2 (2 in all)", "new advisory B")
    assert worse_exposure(tree_exposure(), tree_exposure(finding("C", "o:l", "1", SeverityRating.LOW)))[0].startswith("exposure rose from none")
