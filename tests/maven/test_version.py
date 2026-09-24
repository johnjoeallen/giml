"""Tests for the ComparableVersion port.

The ordered lists and equalities are copied from Maven's own ComparableVersionTest at tag
maven-3.9.11 (maven-artifact/src/test/java/.../ComparableVersionTest.java).
"""

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from giml.maven.version import ComparableVersion, compare_versions

VERSIONS_QUALIFIER = [
    "1-alpha2snapshot", "1-alpha2", "1-alpha-123", "1-beta-2", "1-beta123", "1-m2", "1-m11",
    "1-rc", "1-cr2", "1-rc123", "1-SNAPSHOT", "1", "1-sp", "1-sp2", "1-sp123", "1-abc", "1-def",
    "1-pom-1", "1-1-snapshot", "1-1", "1-2", "1-123",
]  # fmt: skip

VERSIONS_NUMBER = [
    "2.0", "2.0.a", "2-1", "2.0.2", "2.0.123", "2.1.0", "2.1-a", "2.1b", "2.1-c", "2.1-1",
    "2.1.0.1", "2.2", "2.123", "11.a2", "11.a11", "11.b2", "11.b11", "11.m2", "11.m11", "11",
    "11.a", "11b", "11c", "11m",
]  # fmt: skip

EQUAL_PAIRS = [
    ("1", "1"), ("1", "1.0"), ("1", "1.0.0"), ("1.0", "1.0.0"), ("1", "1-0"), ("1", "1.0-0"),
    ("1.0", "1.0-0"),
    ("1a", "1-a"), ("1a", "1.0-a"), ("1a", "1.0.0-a"), ("1.0a", "1-a"), ("1.0.0a", "1-a"),
    ("1x", "1-x"), ("1x", "1.0-x"), ("1x", "1.0.0-x"), ("1.0x", "1-x"), ("1.0.0x", "1-x"),
    ("1ga", "1"), ("1release", "1"), ("1final", "1"), ("1cr", "1rc"),
    ("1a1", "1-alpha-1"), ("1b2", "1-beta-2"), ("1m3", "1-milestone-3"),
    ("1X", "1x"), ("1A", "1a"), ("1B", "1b"), ("1M", "1m"), ("1Ga", "1"), ("1GA", "1"),
    ("1RELEASE", "1"), ("1release", "1"), ("1RELeaSE", "1"), ("1Final", "1"), ("1FinaL", "1"),
    ("1FINAL", "1"), ("1Cr", "1Rc"), ("1cR", "1rC"), ("1m3", "1Milestone3"),
    ("1m3", "1MileStone3"), ("1m3", "1MILESTONE3"),
    ("1-abcdefghijklmnopqrstuvwxyz", "1-ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
]  # fmt: skip

ORDERED_PAIRS = [
    ("1", "2"), ("1.5", "2"), ("1", "2.5"), ("1.0", "1.1"), ("1.1", "1.2"), ("1.0.0", "1.1"),
    ("1.0.1", "1.1"), ("1.1", "1.2.0"), ("1.0-alpha-1", "1.0"), ("1.0-alpha-1", "1.0-alpha-2"),
    ("1.0-alpha-1", "1.0-beta-1"), ("1.0-beta-1", "1.0-SNAPSHOT"), ("1.0-SNAPSHOT", "1.0"),
    ("1.0-alpha-1-SNAPSHOT", "1.0-alpha-1"), ("1.0", "1.0-1"), ("1.0-1", "1.0-2"),
    ("1.0.0", "1.0-1"), ("2.0-1", "2.0.1"), ("2.0.1-klm", "2.0.1-lmn"), ("2.0.1", "2.0.1-xyz"),
    ("2.0.1", "2.0.1-123"), ("2.0.1-xyz", "2.0.1-123"),
    # MNG-5568
    ("6.1.0rc3", "6.1.0"), ("6.1.0rc3", "6.1H.5-beta"), ("6.1.0", "6.1H.5-beta"),
    # MNG-6572
    ("20190126.230843", "1234567890.12345"), ("1234567890.12345", "123456789012345.1H.5-beta"),
    ("20190126.230843", "123456789012345.1H.5-beta"),
    ("123456789012345.1H.5-beta", "12345678901234567890.1H.5-beta"),
    ("1234567890.12345", "12345678901234567890.1H.5-beta"),
    ("20190126.230843", "12345678901234567890.1H.5-beta"),
    # MNG-6964
    ("1-0.alpha", "1"), ("1-0.beta", "1"), ("1-0.alpha", "1-0.beta"),
    # Where RedKite's comparator disagreed with Maven.
    ("1.0", "1.0-sp1"),
]  # fmt: skip

MNG_7644_QUALIFIERS = ["abc", "alpha", "a", "beta", "b", "def", "milestone", "m", "RC"]


def assert_canonical_stable(version: str) -> ComparableVersion:
    parsed = ComparableVersion(version)
    assert ComparableVersion(parsed.canonical).canonical == parsed.canonical, version
    return parsed


def assert_equal(v1: str, v2: str) -> None:
    c1, c2 = assert_canonical_stable(v1), assert_canonical_stable(v2)
    assert c1.compare_to(c2) == 0 and c2.compare_to(c1) == 0, (v1, v2)
    assert c1 == c2 and hash(c1) == hash(c2), (v1, v2)


def assert_ordered(low: str, high: str) -> None:
    c_low, c_high = assert_canonical_stable(low), assert_canonical_stable(high)
    assert c_low.compare_to(c_high) < 0, f"expected {low} < {high}"
    assert c_high.compare_to(c_low) > 0, f"expected {high} > {low}"
    assert c_low < c_high and c_high > c_low


@pytest.mark.parametrize("versions", [VERSIONS_QUALIFIER, VERSIONS_NUMBER], ids=["qualifier", "number"])
def test_upstream_version_lists_are_strictly_ordered(versions):
    for low, high in itertools.combinations(versions, 2):
        assert_ordered(low, high)


@pytest.mark.parametrize(("v1", "v2"), EQUAL_PAIRS)
def test_upstream_equal_versions(v1, v2):
    assert_equal(v1, v2)


@pytest.mark.parametrize(("low", "high"), ORDERED_PAIRS)
def test_upstream_ordered_pairs(low, high):
    assert_ordered(low, high)


@pytest.mark.parametrize("value", ["1", "0"])
def test_leading_zeroes_are_equal_for_every_length(value):
    variants = [value.rjust(length, "0") for length in range(1, 20)]
    for v1, v2 in itertools.combinations_with_replacement(variants, 2):
        assert_equal(v1, v2)


@pytest.mark.parametrize("x", MNG_7644_QUALIFIERS)
def test_mng_7644_dot_qualifier_is_treated_as_dash(x):
    assert_ordered(f"1.0.0.{x}1", f"1.0.0-{x}2")
    assert_equal(f"2-{x}", f"2.0.{x}")
    assert_equal(f"2-{x}", f"2.0.0.{x}")
    assert_equal(f"2.0.{x}", f"2.0.0.{x}")


@pytest.mark.parametrize(
    ("version", "canonical"),
    [("1.0-sp1", "1-sp-1"), ("1.0.RELEASE", "1"), ("1.0", "1"), ("2.18.4", "2.18.4")],
)
def test_canonical_form_matches_maven(version, canonical):
    # Expected values printed by maven-artifact-3.9.11.jar's main().
    assert ComparableVersion(version).canonical == canonical


def test_number_type_decides_cross_length_comparison():
    # Ten zeros parse as a Java long, which outranks any int item regardless of value.
    assert_ordered("1.5.5", "1.0000000000.5")


def test_compare_versions_and_str():
    assert compare_versions("1.0", "1.1") < 0
    assert compare_versions("1.1", "1.0") > 0
    assert compare_versions("1.0", "1") == 0
    assert str(ComparableVersion("1.0-RC1")) == "1.0-RC1"
    assert repr(ComparableVersion("1.0")) == "ComparableVersion('1.0')"


def test_comparison_with_other_types_is_not_supported():
    assert ComparableVersion("1") != "1"
    with pytest.raises(TypeError):
        _ = ComparableVersion("1") < "2"


# Realistic versions: numeric release parts, then optional qualifier parts. Maven's ordering is
# not a total order on arbitrary strings (see test_maven_ordering_has_cycles_on_degenerate_input),
# so the order laws are checked on the shapes real artifacts use.
_number = st.integers(min_value=0, max_value=30).map(str)
_qualifier = st.sampled_from(
    ["alpha", "beta", "milestone", "rc", "cr", "SNAPSHOT", "ga", "final", "RELEASE", "sp", "jre", "x"]
)
_suffix = st.tuples(st.sampled_from(["-", "."]), _qualifier, st.sampled_from(["", "1", "-2", "12"]))
version_text = st.builds(
    lambda release, suffixes: ".".join(release) + "".join(sep + q + n for sep, q, n in suffixes),
    st.lists(_number, min_size=1, max_size=4),
    st.lists(_suffix, max_size=2),
)


@given(version_text, version_text)
def test_comparison_is_antisymmetric(a, b):
    ca, cb = ComparableVersion(a), ComparableVersion(b)
    assert (ca.compare_to(cb) > 0) == (cb.compare_to(ca) < 0)
    assert (ca.compare_to(cb) == 0) == (cb.compare_to(ca) == 0)


@given(version_text, version_text, version_text)
def test_comparison_is_transitive(a, b, c):
    ca, cb, cc = ComparableVersion(a), ComparableVersion(b), ComparableVersion(c)
    if ca.compare_to(cb) <= 0 and cb.compare_to(cc) <= 0:
        assert ca.compare_to(cc) <= 0


def test_canonicalisation_is_not_always_idempotent():
    # Maven only guarantees a stable canonical form for its curated versions (checked above by
    # assert_canonical_stable). maven-artifact-3.9.11.jar prints "0.alpha-ga -> 0.alpha" and
    # "0.alpha -> alpha"; reproduced faithfully.
    assert ComparableVersion("0.alpha-ga").canonical == "0.alpha"
    assert ComparableVersion("0.alpha").canonical == "alpha"


def test_version_need_not_equal_its_canonical_form():
    # maven-artifact-3.9.11.jar prints: "0A -> a" and "0A > a".
    assert ComparableVersion("0A").canonical == "a"
    assert_ordered("a", "0A")


def test_maven_ordering_has_cycles_on_degenerate_input():
    # maven-artifact-3.9.11.jar prints: "" < A, A < 0A0, 0A0 < "". Reproduced faithfully.
    assert_ordered("", "A")
    assert_ordered("A", "0A0")
    assert_ordered("0A0", "")
