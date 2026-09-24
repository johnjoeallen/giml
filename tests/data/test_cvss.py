import json
from pathlib import Path

import pytest

from giml.data.cvss import CvssError, base_score, parse_vector

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "cvss" / "first-examples.json"
EXAMPLES = json.loads(FIXTURE.read_text(encoding="utf-8"))["examples"]


@pytest.mark.parametrize("example", EXAMPLES, ids=[e["vector"] for e in EXAMPLES])
def test_first_worked_examples(example):
    assert base_score(example["vector"]).base_score == example["base_score"]


@pytest.mark.parametrize(
    ("vector", "score"),
    [
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H", 10.0),  # capped at 10
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N", 0.0),  # no impact
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:N/I:N/A:N", 0.0),
        ("CVSS:3.1/AV:P/AC:H/PR:H/UI:R/S:U/C:L/I:N/A:N", 1.6),
    ],
)
def test_boundary_scores(vector, score):
    assert base_score(vector).base_score == score


def test_temporal_and_environmental_metrics_are_ignored():
    base = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
    assert base_score(base + "/E:U/RL:O/RC:C/CR:H/MAV:L").base_score == 9.8


def test_score_carries_version_and_trimmed_vector():
    result = base_score(" CVSS:3.0/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H ")
    assert (result.version, result.vector) == ("3.0", "CVSS:3.0/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")


def test_v31_roundup_avoids_floating_point_error():
    # 4.0000001-style inputs must not round up to 4.1 (spec 3.1 Appendix A).
    from giml.data.cvss import _roundup_v31

    assert _roundup_v31(4.000002) == 4.0
    assert _roundup_v31(4.02) == 4.1
    assert _roundup_v31(4.0) == 4.0


@pytest.mark.parametrize(
    ("vector", "message"),
    [
        ("AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", "not a CVSS v3.0/v3.1 vector"),
        ("CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N", "not a CVSS v3.0/v3.1"),
        ("CVSS:2.0/AV:N", "not a CVSS v3.0/v3.1 vector"),
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H", "base metric A missing"),
        ("CVSS:3.1/AV:N/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", "metric AV repeated"),
        ("CVSS:3.1/AV:X/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", "invalid value AV:X"),
        ("CVSS:3.1/AV:N/AC:L/PR:X/UI:N/S:U/C:H/I:H/A:H", "invalid value PR:X"),
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:X/C:H/I:H/A:H", "invalid value S:X"),
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H/", "malformed metric ''"),
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:Hx", "malformed metric"),
    ],
)
def test_malformed_vectors_are_rejected(vector, message):
    with pytest.raises(CvssError, match=message):
        base_score(vector)


def test_parse_vector_returns_metrics():
    version, metrics = parse_vector("CVSS:3.1/AV:L/AC:H/PR:L/UI:R/S:C/C:L/I:N/A:H")
    assert version == "3.1"
    assert metrics == {"AV": "L", "AC": "H", "PR": "L", "UI": "R", "S": "C", "C": "L", "I": "N", "A": "H"}
