"""Maven version ordering: a port of ``org.apache.maven.artifact.versioning.ComparableVersion``.

Ported from maven-artifact 3.9.11 so that giml orders versions exactly as the Maven build it
drives does. Deliberately faithful rather than idiomatic: odd cases (numbers whose type depends
on their digit count, string comparison by UTF-16 code units) are reproduced because OSV ranges
and candidate lists are only correct if they agree with Maven.

Known limit: lower-casing uses Python's ``str.lower``, which can differ from Java's
``toLowerCase(Locale.ENGLISH)`` for a few non-ASCII characters. Real Maven versions are ASCII.
"""

from __future__ import annotations

import functools
import unicodedata
from typing import Union

_MAX_INTITEM_LENGTH = 9
_MAX_LONGITEM_LENGTH = 18

# Java distinguishes int, long and BigInteger items; cross-type comparison is by type, not value.
_INT, _LONG, _BIG = 0, 1, 2

_QUALIFIERS = ("alpha", "beta", "milestone", "rc", "snapshot", "", "sp")
_ALIASES = {"ga": "", "final": "", "release": "", "cr": "rc"}
_RELEASE_VERSION_INDEX = str(_QUALIFIERS.index(""))
_SHORTHAND = {"a": "alpha", "b": "beta", "m": "milestone"}


class _Num:
    __slots__ = ("kind", "value")

    def __init__(self, digits: str) -> None:
        digits = _strip_leading_zeroes(digits)
        if len(digits) <= _MAX_INTITEM_LENGTH:
            self.kind = _INT
        elif len(digits) <= _MAX_LONGITEM_LENGTH:
            self.kind = _LONG
        else:
            self.kind = _BIG
        self.value = int(digits)

    def is_null(self) -> bool:
        return self.value == 0

    def key(self) -> tuple:
        return ("n", self.kind, self.value)

    def __str__(self) -> str:
        return str(self.value)


class _Str:
    __slots__ = ("value",)

    def __init__(self, value: str, followed_by_digit: bool) -> None:
        if followed_by_digit and len(value) == 1:
            value = _SHORTHAND.get(value, value)
        self.value = _ALIASES.get(value, value)

    def is_null(self) -> bool:
        return _comparable_qualifier(self.value) == _RELEASE_VERSION_INDEX

    def key(self) -> tuple:
        return ("s", self.value)

    def __str__(self) -> str:
        return self.value


class _List(list):
    def is_null(self) -> bool:
        return len(self) == 0

    def normalize(self) -> None:
        for i in range(len(self) - 1, -1, -1):
            last = self[i]
            if last.is_null():
                del self[i]
            elif not isinstance(last, _List):
                break

    def key(self) -> tuple:
        return ("l", tuple(item.key() for item in self))

    def __str__(self) -> str:
        buffer = ""
        for item in self:
            if buffer:
                buffer += "-" if isinstance(item, _List) else "."
            buffer += str(item)
        return buffer


_Item = Union[_Num, _Str, _List]


def _strip_leading_zeroes(buf: str) -> str:
    if not buf:
        return "0"
    for i, c in enumerate(buf):
        if c != "0":
            return buf[i:]
    return buf


def _comparable_qualifier(qualifier: str) -> str:
    if qualifier in _QUALIFIERS:
        return str(_QUALIFIERS.index(qualifier))
    return f"{len(_QUALIFIERS)}-{qualifier}"


def _java_string_compare(a: str, b: str) -> int:
    # Java's String.compareTo compares UTF-16 code units; big-endian UTF-16 bytes sort the same way.
    ea = a.encode("utf-16-be", "surrogatepass")
    eb = b.encode("utf-16-be", "surrogatepass")
    return (ea > eb) - (ea < eb)


def _is_digit(c: str) -> bool:
    # Java's Character.isDigit(char): a BMP code unit in category Nd. Astral digits are surrogate
    # pairs in Java and so never digits.
    return ord(c) <= 0xFFFF and unicodedata.category(c) == "Nd"


def _compare(item: _Item, other: _Item | None) -> int:
    if isinstance(item, _Num):
        if other is None:
            return 0 if item.value == 0 else 1
        if isinstance(other, _Num):
            if item.kind != other.kind:
                return -1 if item.kind < other.kind else 1
            return (item.value > other.value) - (item.value < other.value)
        return 1  # 1.1 > 1-sp and 1.1 > 1-1
    if isinstance(item, _Str):
        if other is None:
            # 1-rc < 1, 1-ga > 1
            return _java_string_compare(_comparable_qualifier(item.value), _RELEASE_VERSION_INDEX)
        if isinstance(other, _Str):
            return _java_string_compare(
                _comparable_qualifier(item.value), _comparable_qualifier(other.value)
            )
        return -1  # 1.any < 1.1 and 1.any < 1-1
    if other is None:
        # MNG-6964: compare every item with null, not just the first.
        for child in item:
            result = _compare(child, None)
            if result != 0:
                return result
        return 0
    if isinstance(other, _Num):
        return -1  # 1-1 < 1.0.x
    if isinstance(other, _Str):
        return 1  # 1-1 > 1-sp
    for i in range(max(len(item), len(other))):
        left = item[i] if i < len(item) else None
        right = other[i] if i < len(other) else None
        if left is None:
            result = 0 if right is None else -_compare(right, left)
        else:
            result = _compare(left, right)
        if result != 0:
            return result
    return 0


def _parse_item(is_digit: bool, buf: str) -> _Item:
    return _Num(buf) if is_digit else _Str(buf, False)


def _parse(version: str) -> _List:
    items = _List()
    version = version.lower()
    current = items
    stack: list[_List] = [current]
    is_digit = False
    start = 0

    def push_new_list() -> None:
        nonlocal current
        new = _List()
        current.append(new)
        current = new
        stack.append(new)

    for i, c in enumerate(version):
        if c in ".-":
            if i == start:
                current.append(_Num("0"))
            else:
                current.append(_parse_item(is_digit, version[start:i]))
            start = i + 1
            if c == "-":
                push_new_list()
        elif _is_digit(c):
            if not is_digit and i > start:
                # 1.0.0.X1 < 1.0.0-X2: treat .X as -X for any string qualifier X
                if current:
                    push_new_list()
                current.append(_Str(version[start:i], True))
                start = i
                push_new_list()
            is_digit = True
        else:
            if is_digit and i > start:
                current.append(_parse_item(True, version[start:i]))
                start = i
                push_new_list()
            is_digit = False

    if len(version) > start:
        if not is_digit and current:
            push_new_list()
        current.append(_parse_item(is_digit, version[start:]))

    while stack:
        stack.pop().normalize()
    return items


@functools.total_ordering
class ComparableVersion:
    """A Maven version with Maven's ordering. Equality is structural, as in Maven."""

    __slots__ = ("value", "_items", "_key")

    def __init__(self, version: str) -> None:
        self.value = version
        self._items = _parse(version)
        self._key = self._items.key()

    @property
    def canonical(self) -> str:
        return str(self._items)

    def compare_to(self, other: ComparableVersion) -> int:
        return _compare(self._items, other._items)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ComparableVersion):
            return NotImplemented
        return self._key == other._key

    def __lt__(self, other: ComparableVersion) -> bool:
        if not isinstance(other, ComparableVersion):
            return NotImplemented
        return self.compare_to(other) < 0

    def __hash__(self) -> int:
        return hash(self._key)

    def __str__(self) -> str:
        return self.value

    def __repr__(self) -> str:
        return f"ComparableVersion({self.value!r})"


def compare_versions(a: str, b: str) -> int:
    """Maven ordering of two version strings: negative, zero or positive."""
    return ComparableVersion(a).compare_to(ComparableVersion(b))
