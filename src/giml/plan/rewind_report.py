"""The rewind comparison (spec section 5.3, item 7): three states of the same project side by side.

A rewind run starts from old POMs, so the project's own later history is a known-good answer to compare
with: for every dependency, the version in the rewound state, in giml's result, and at the base commit,
each with how many advisories affect it.
"""

from __future__ import annotations

from giml.plan.exposure import TreeExposure

STATES = ("rewound", "result", "base")


def _by_coordinate(tree: TreeExposure) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for dependency in tree.dependencies:
        entry = found.setdefault(str(dependency.coordinate), {"versions": [], "advisories": set()})
        entry["versions"].append(dependency.version)
        entry["advisories"] |= {f.advisory_id for f in dependency.findings}
    return {coordinate: {"versions": entry["versions"], "advisories": len(entry["advisories"])} for coordinate, entry in found.items()}


def compare_states(rewound: TreeExposure, result: TreeExposure, base: TreeExposure) -> dict:
    """Per dependency: {state: {versions, advisories}} (None where the state does not have it), plus each state's overall exposure."""
    tables = dict(zip(STATES, (_by_coordinate(rewound), _by_coordinate(result), _by_coordinate(base)), strict=True))
    coordinates = sorted(set().union(*tables.values()))
    rows = [{"coordinate": c, **{state: tables[state].get(c) for state in STATES}} for c in coordinates]
    exposure = {state: {"max_severity": tree.exposure.max_severity.name if tree.exposure.max_severity else None,
                        "at_max": tree.exposure.at_max, "total": tree.exposure.total}
                for state, tree in zip(STATES, (rewound, result, base), strict=True)}  # fmt: skip
    return {"rows": rows, "exposure": exposure,
            "changed": [r["coordinate"] for r in rows if r["rewound"] != r["result"] or r["result"] != r["base"]]}  # fmt: skip


def render_comparison(comparison: dict) -> list[str]:
    """Markdown lines: a table of the dependencies whose version differs between any two states."""
    changed = set(comparison["changed"])
    lines = ["| dependency | rewound | giml result | base commit |", "| --- | --- | --- | --- |"]

    def cell(entry: dict | None) -> str:
        return "-" if entry is None else f"{', '.join(entry['versions'])} ({entry['advisories']} adv.)"

    lines += [f"| {r['coordinate']} | {cell(r['rewound'])} | {cell(r['result'])} | {cell(r['base'])} |"
              for r in comparison["rows"] if r["coordinate"] in changed]  # fmt: skip
    return lines
