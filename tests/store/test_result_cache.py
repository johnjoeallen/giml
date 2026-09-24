import hashlib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from giml.store.result_cache import FileResultCache, canonical_json

json_scalars = st.none() | st.booleans() | st.integers() | st.text(max_size=8)
json_values = st.recursive(
    json_scalars,
    lambda children: st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=5), children, max_size=3),
    max_leaves=10,
)


def test_key_is_sha256_of_canonical_json(tmp_path):
    cache = FileResultCache(tmp_path)
    parts = {"stage": "compile", "jdk": "21"}
    expected = hashlib.sha256(b'{"jdk":"21","stage":"compile"}').hexdigest()
    assert cache.key(parts) == expected


@given(st.dictionaries(st.text(max_size=5), json_values, max_size=5))
def test_key_ignores_insertion_order(parts):
    cache = FileResultCache(None)
    reversed_parts = dict(reversed(list(parts.items())))
    assert cache.key(parts) == cache.key(reversed_parts)


def test_different_parts_give_different_keys(tmp_path):
    cache = FileResultCache(tmp_path)
    assert cache.key({"stage": "compile"}) != cache.key({"stage": "unit"})


def test_put_then_get_round_trips_and_counts_hits_and_misses(tmp_path):
    cache = FileResultCache(tmp_path)
    key = cache.key({"a": 1})
    assert cache.get(key) is None
    cache.put(key, {"outcome": "pass", "duration_ms": 1200})
    assert cache.get(key) == {"outcome": "pass", "duration_ms": 1200}
    assert (cache.hits, cache.misses) == (1, 1)
    assert (tmp_path / key[:2] / f"{key}.json").is_file()


def test_put_overwrites_and_leaves_no_temp_files(tmp_path):
    cache = FileResultCache(tmp_path)
    key = cache.key({"a": 1})
    cache.put(key, {"v": 1})
    cache.put(key, {"v": 2})
    assert cache.get(key) == {"v": 2}
    assert [p.name for p in (tmp_path / key[:2]).iterdir()] == [f"{key}.json"]


def test_failed_write_leaves_no_temp_file(tmp_path):
    cache = FileResultCache(tmp_path)
    key = cache.key({"a": 1})
    with pytest.raises(ValueError):
        cache.put(key, {"bad": float("nan")})
    assert list((tmp_path / key[:2]).iterdir()) == []


@pytest.mark.parametrize("key", ["", "../etc/passwd", "A" * 64, "0" * 63])
def test_malformed_keys_are_rejected(tmp_path, key):
    cache = FileResultCache(tmp_path)
    with pytest.raises(ValueError, match="not a cache key"):
        cache.get(key)


def test_canonical_json_is_compact_and_sorted():
    assert canonical_json({"b": [1, {"d": 1, "c": 2}], "a": "é"}) == '{"a":"é","b":[1,{"c":2,"d":1}]}'
