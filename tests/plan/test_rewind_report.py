from giml.core.model import SeverityRating
from giml.plan.rewind_report import compare_states, render_comparison
from tests.plan.test_exposure import finding, tree_exposure


def tree(*pairs):
    """A TreeExposure of (coordinate, version, advisory-or-None) triples."""
    from giml.plan.exposure import ResolvedDependency, TreeExposure, exposure_of
    from giml.core.model import Coordinate

    deps, findings = [], []
    for coordinate, version, advisory in pairs:
        found = (finding(advisory, coordinate, version, SeverityRating.HIGH),) if advisory else ()
        deps.append(ResolvedDependency(Coordinate.parse(coordinate), version, (), (), True, found))
        findings += found
    return TreeExposure(tuple(deps), exposure_of(findings), "osv-fake")


def test_each_dependency_is_shown_in_the_three_states_with_its_advisories():
    rewound = tree(("o:lib", "1.0", "A"), ("o:same", "2.0", None), ("o:gone", "1.0", None))
    result = tree(("o:lib", "1.2", None), ("o:same", "2.0", None))
    base = tree(("o:lib", "1.3", None), ("o:same", "2.0", None), ("o:new", "5", None))
    comparison = compare_states(rewound, result, base)
    rows = {r["coordinate"]: r for r in comparison["rows"]}
    assert rows["o:lib"]["rewound"] == {"versions": ["1.0"], "advisories": 1} and rows["o:lib"]["base"]["versions"] == ["1.3"]
    assert rows["o:gone"]["result"] is None and rows["o:new"]["rewound"] is None
    assert comparison["changed"] == ["o:gone", "o:lib", "o:new"]  # o:same is identical everywhere
    assert comparison["exposure"]["rewound"]["total"] == 1 and comparison["exposure"]["result"]["max_severity"] is None


def test_the_markdown_table_lists_only_what_differs():
    comparison = compare_states(tree(("o:lib", "1.0", "A"), ("o:same", "2", None)), tree(("o:lib", "1.2", None), ("o:same", "2", None)),
                                tree(("o:lib", "1.3", None), ("o:same", "2", None)))  # fmt: skip
    lines = render_comparison(comparison)
    assert lines[2] == "| o:lib | 1.0 (1 adv.) | 1.2 (0 adv.) | 1.3 (0 adv.) |" and len(lines) == 3
