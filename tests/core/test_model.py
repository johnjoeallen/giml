import pytest

from giml.core.model import Coordinate, SeverityRating


def test_coordinate_parse_and_str_round_trip():
    coordinate = Coordinate.parse(" com.fasterxml.jackson.core:jackson-databind ")
    assert coordinate == Coordinate("com.fasterxml.jackson.core", "jackson-databind")
    assert str(coordinate) == "com.fasterxml.jackson.core:jackson-databind"


@pytest.mark.parametrize("text", ["onlygroup", "a:b:c", ":b", "a:", "a b:c", "a:${x}"])
def test_coordinate_parse_rejects_malformed_text(text):
    with pytest.raises(ValueError):
        Coordinate.parse(text)


def test_coordinates_sort_by_group_then_artifact():
    unsorted = [Coordinate("b", "a"), Coordinate("a", "z"), Coordinate("a", "b")]
    assert [str(c) for c in sorted(unsorted)] == ["a:b", "a:z", "b:a"]


@pytest.mark.parametrize(
    ("score", "rating"),
    [(0.0, "NONE"), (0.1, "LOW"), (3.9, "LOW"), (4.0, "MEDIUM"), (6.9, "MEDIUM"),
     (7.0, "HIGH"), (8.9, "HIGH"), (9.0, "CRITICAL"), (10.0, "CRITICAL")],
)  # fmt: skip
def test_rating_from_cvss_score_boundaries(score, rating):
    assert SeverityRating.from_score(score) is SeverityRating[rating]


@pytest.mark.parametrize(
    ("label", "rating"),
    [("critical", "CRITICAL"), (" High ", "HIGH"), ("MODERATE", "MEDIUM"), ("low", "LOW"),
     ("bogus", "UNKNOWN"), ("", "UNKNOWN")],
)  # fmt: skip
def test_rating_from_label(label, rating):
    assert SeverityRating.from_label(label) is SeverityRating[rating]


def test_ratings_order_worst_last():
    assert max(SeverityRating.LOW, SeverityRating.CRITICAL, SeverityRating.UNKNOWN) is SeverityRating.CRITICAL


def test_invalid_coordinate_part_is_named():
    with pytest.raises(ValueError, match=r"invalid Maven coordinate part 'a b'"):
        Coordinate("a b", "c")
