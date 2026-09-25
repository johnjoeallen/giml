"""Classify a failed Maven stage and give it a signature that survives paths, times and line numbers.

Spec sections 9 and 15. The class says what kind of failure it was (the candidate's fault, or not);
the signature, a hash of the normalised error lines, lets the same failure be recognised across runs,
worktrees and projects, so known-bad transitions can be remembered (spec 10). Both come from Maven's
log text: there is no structured output for a failure. The patterns are tested against real Maven
output captured in tests/fixtures/logs.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

RESOLUTION = "resolution"
COMPILE = "compile"
ENFORCER_CONVERGENCE = "enforcer_convergence"
DUPLICATE_CLASSES = "duplicate_classes"
UNIT_TEST = "unit_test"
INTEGRATION_TEST = "integration_test"
MUTATION_DROPPED = "mutation_score_dropped"  # PIT ran but the candidate's scores fell below the floor
TIMEOUT = "timeout"
INFRASTRUCTURE = "infrastructure"  # not the candidate's fault: retry or ignore
VULNERABILITY_WORSE = "vulnerability_worse"  # judged from the resolved tree and the advisory snapshot, without a build
UNKNOWN = "unknown"

MAX_KEY_LINES = 8
MAX_LINE_LENGTH = 300

# In priority order: what the environment did wrong outranks what the build says about the candidate.
_INFRASTRUCTURE = re.compile(
    r"No space left on device|java\.lang\.OutOfMemoryError|Connect timed out|Read timed out|Connection (?:refused|reset)"
    r"|Name or service not known|UnknownHostException|Unknown host|Network is unreachable|Temporary failure in name resolution"
    r"|Transfer failed for \S+ 5\d\d|status code: 5\d\d"
)  # fmt: skip
_CONVERGENCE = re.compile(r"(?:DependencyConvergence|BanDuplicatePomDependencyVersions) failed")
_DUPLICATE_CLASSES = re.compile(r"BanDuplicateClasses failed")
_COMPILE = re.compile(r"COMPILATION ERROR|Compilation failure|maven-compiler-plugin")
_RESOLUTION = re.compile(
    r"Could not resolve (?:dependencies|artifact)|Could not find artifact|Could not transfer artifact|Non-resolvable parent POM"
    r"|Failed to (?:read artifact descriptor|collect dependencies)|artifacts? could not be resolved|dependencies could not be resolved"
    r"|was not found in \S+ during a previous attempt"
)  # fmt: skip
_INTEGRATION = re.compile(r"maven-failsafe-plugin")
_UNIT = re.compile(r"maven-surefire-plugin|There are test failures")

# Lines that say nothing about this failure.
_BOILERPLATE = re.compile(
    r"^(?:-> \[Help \d+\]|\[Help \d+\]|To see the full stack trace|Re-run Maven using|For more information about the errors"
    r"|After correcting the problems|mvn <args>|Please refer to)"
)  # fmt: skip

_ABSOLUTE_PATH = re.compile(r"(?<![\w:/.])/(?:[\w.\-$@+]+/)*[\w.\-$@+]*")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
_DURATION = re.compile(r"(?<![\w.\-])\d+(?:\.\d+)?\s?(?:ms|s|min)\b")
_SOURCE_POSITION = re.compile(r":\[\d+,\d+\]")
_LINE_REFERENCE = re.compile(r"(\w\.\w{2,12}):\d+\b")
_RUN_ID = re.compile(r"\b\d{8}T\d{6}Z-[0-9a-f]+\b")
_LONG_ID = re.compile(r"\b[0-9a-f]{16,}\b")
_JUNIT_DIRECTORY = re.compile(r"junit-\d+")
_CLASS_FILE = re.compile(r"^[\w/$.\-]+\.class$")
_CACHED_MISSING = re.compile(r"^(\S+) was not found in (\S+) during a previous attempt\..*")
_MISSING = re.compile(r"^Could not find artifact (\S+) in \S+ \((\S+)\)")
_ON_PROJECT = re.compile(r"on project \S+?:")
_FOR_PROJECT = re.compile(r"for project \S+")


@dataclass(frozen=True)
class Failure:
    failure_class: str
    signature: str  # 16 hex digits
    key_lines: tuple[str, ...]  # the normalised lines the signature was made from


def _path(match: re.Match) -> str:
    """A file keeps its name (it identifies the failure); a directory is only noise."""
    last = match.group().rstrip("/").rsplit("/", 1)[-1]
    return last if "." in last else "<dir>"


def normalise(line: str) -> str:
    """Strip what differs between runs of the same failure: directories, times, positions, ids, project names."""
    line = re.sub(r"\s+", " ", line).strip()
    line = _CACHED_MISSING.sub(r"Could not find artifact \1 in \2", line)  # Maven caches a miss and words it differently
    line = _MISSING.sub(r"Could not find artifact \1 in \2", line)
    line = _ON_PROJECT.sub("on project <p>:", line)
    line = _FOR_PROJECT.sub("for project <p>", line)
    line = _TIMESTAMP.sub("<ts>", line)
    line = _ABSOLUTE_PATH.sub(_path, line)
    line = _SOURCE_POSITION.sub(":[<n>,<n>]", line)
    line = _LINE_REFERENCE.sub(r"\1:<n>", line)
    line = _DURATION.sub("<t>", line)
    line = _RUN_ID.sub("<id>", line)
    line = _LONG_ID.sub("<id>", line)
    line = _JUNIT_DIRECTORY.sub("junit-<id>", line)
    return re.sub(r"\s+", " ", line).strip()


def _error_lines(log_text: str) -> list[str]:
    found: list[str] = []
    for raw in log_text.splitlines():
        if not raw.startswith("[ERROR]"):
            continue
        text = raw.removeprefix("[ERROR]").strip()
        if not text or _BOILERPLATE.match(text):
            continue
        line = normalise(text)[:MAX_LINE_LENGTH]
        if line and line not in found:
            found.append(line)
    return found


def _canonical_duplicates(lines: list[str]) -> list[str]:
    """BanDuplicateClasses lists the offending artifacts and classes in no fixed order; sort them."""
    if "Found in:" not in lines or "Duplicate classes:" not in lines:
        return lines
    start, middle = lines.index("Found in:"), lines.index("Duplicate classes:")
    classes = [line for line in lines[middle + 1:] if _CLASS_FILE.match(line)]
    rest = [line for line in lines[middle + 1:] if not _CLASS_FILE.match(line)]
    return [*lines[: start + 1], *sorted(lines[start + 1 : middle]), "Duplicate classes:", *sorted(classes), *rest]


def _key_lines(log_text: str, failure_class: str) -> tuple[str, ...]:
    lines = _error_lines(log_text)
    if failure_class == DUPLICATE_CLASSES:
        lines = _canonical_duplicates(lines)
    return tuple(lines[:MAX_KEY_LINES])


def _class_of(log_text: str) -> str:
    if _INFRASTRUCTURE.search(log_text):
        return INFRASTRUCTURE
    for pattern, failure_class in ((_DUPLICATE_CLASSES, DUPLICATE_CLASSES), (_CONVERGENCE, ENFORCER_CONVERGENCE),
                                   (_COMPILE, COMPILE), (_RESOLUTION, RESOLUTION),
                                   (_INTEGRATION, INTEGRATION_TEST), (_UNIT, UNIT_TEST)):  # fmt: skip
        if pattern.search(log_text):
            return failure_class
    return UNKNOWN


def classify(log_text: str, timed_out: bool = False) -> Failure:
    """The class and signature of a failed stage's log. A timeout is a timeout whatever the log says."""
    failure_class = TIMEOUT if timed_out else _class_of(log_text)
    key_lines = ("timed out",) if timed_out else _key_lines(log_text, failure_class)
    digest = hashlib.sha256("\n".join((failure_class, *key_lines)).encode("utf-8")).hexdigest()
    return Failure(failure_class, digest[:16], key_lines)
