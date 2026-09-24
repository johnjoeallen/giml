"""CVSS v3.0 and v3.1 base score calculator (FIRST specification, section 7.1).

Only the base score is computed; temporal and environmental metrics in a vector are validated for
shape and otherwise ignored. v3.0 and v3.1 differ only in how the final score is rounded up.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

_WEIGHTS = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2},
    "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62},
    "C": {"H": 0.56, "L": 0.22, "N": 0.0},
    "I": {"H": 0.56, "L": 0.22, "N": 0.0},
    "A": {"H": 0.56, "L": 0.22, "N": 0.0},
}
_PR_UNCHANGED = {"N": 0.85, "L": 0.62, "H": 0.27}
_PR_CHANGED = {"N": 0.85, "L": 0.68, "H": 0.5}
_BASE_METRICS = ("AV", "AC", "PR", "UI", "S", "C", "I", "A")
_PREFIX = re.compile(r"^CVSS:(3\.[01])/")
_METRIC = re.compile(r"^([A-Z]{1,3}):([A-Z])$")


class CvssError(ValueError):
    """The vector is not a well-formed CVSS v3.0/v3.1 vector."""


@dataclass(frozen=True)
class CvssScore:
    version: str
    base_score: float
    vector: str


def _roundup_v31(value: float) -> float:
    # Spec 3.1 Appendix A: integer arithmetic avoids floating-point surprises such as 4.000001.
    int_input = round(value * 100000)
    if int_input % 10000 == 0:
        return int_input / 100000.0
    return (math.floor(int_input / 10000) + 1) / 10.0


def _roundup_v30(value: float) -> float:
    return math.ceil(value * 10) / 10


def parse_vector(vector: str) -> tuple[str, dict[str, str]]:
    """Split a vector into its version and metric values. Raises CvssError."""
    text = vector.strip()
    prefix = _PREFIX.match(text)
    if not prefix:
        raise CvssError(f"not a CVSS v3.0/v3.1 vector: {vector!r}")
    metrics: dict[str, str] = {}
    for part in text[prefix.end():].split("/"):
        match = _METRIC.match(part)
        if not match:
            raise CvssError(f"malformed metric {part!r} in {vector!r}")
        name, value = match.groups()
        if name in metrics:
            raise CvssError(f"metric {name} repeated in {vector!r}")
        metrics[name] = value
    for name in _BASE_METRICS:
        if name not in metrics:
            raise CvssError(f"base metric {name} missing from {vector!r}")
    if metrics["S"] not in ("U", "C"):
        raise CvssError(f"invalid value S:{metrics['S']} in {vector!r}")
    pr_table = _PR_CHANGED if metrics["S"] == "C" else _PR_UNCHANGED
    for name in _BASE_METRICS:
        allowed = pr_table if name == "PR" else _WEIGHTS.get(name, {"U": 0, "C": 0})
        if metrics[name] not in allowed:
            raise CvssError(f"invalid value {name}:{metrics[name]} in {vector!r}")
    return prefix.group(1), metrics


def base_score(vector: str) -> CvssScore:
    """The CVSS base score of a v3.0 or v3.1 vector. Raises CvssError."""
    version, m = parse_vector(vector)
    changed = m["S"] == "C"
    iss = 1 - (1 - _WEIGHTS["C"][m["C"]]) * (1 - _WEIGHTS["I"][m["I"]]) * (1 - _WEIGHTS["A"][m["A"]])
    if changed:
        impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15
    else:
        impact = 6.42 * iss
    privileges = (_PR_CHANGED if changed else _PR_UNCHANGED)[m["PR"]]
    exploitability = 8.22 * _WEIGHTS["AV"][m["AV"]] * _WEIGHTS["AC"][m["AC"]] * privileges * _WEIGHTS["UI"][m["UI"]]
    roundup = _roundup_v31 if version == "3.1" else _roundup_v30
    if impact <= 0:
        score = 0.0
    elif changed:
        score = roundup(min(1.08 * (impact + exploitability), 10))
    else:
        score = roundup(min(impact + exploitability, 10))
    return CvssScore(version, score, vector.strip())
