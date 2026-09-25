"""Content-addressed caching of stage outcomes (spec section 10).

A result is looked up by a hash of everything that decides it: what the worktree contains, the JDK
and Maven that run it, giml's pinned tool versions, the gate config version and the stage. So a
stale result can never be mistaken for a current one, and the same content anywhere (another
worktree, another run) reuses the answer. A hit returns the stored outcome with its original timing,
and per-stage hit, miss and time figures are kept so "later runs are faster" is measured.

Only outcomes that are properties of the candidate are stored: an infrastructure failure or a
timeout says nothing about it and is never cached.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from giml.core.interfaces import BuildRunner, ResultCache
from giml.gate.setup import tooling
from giml.git.runner import Git
from giml.maven.build import StageOutcome
from giml.maven.failures import INFRASTRUCTURE, TIMEOUT, Failure
from giml.store.result_cache import canonical_json

SCHEMA = 2
_NOT_CACHED = (INFRASTRUCTURE, TIMEOUT)
_MAVEN_VERSION = re.compile(r"Apache Maven (\S+)")


@dataclass(frozen=True)
class BuildEnvironment:
    """The parts of a key that describe the machine and giml, not the project."""

    jdk: str | None  # the JDK's version, from its release file
    maven: str
    tooling: str  # tooling_fingerprint()
    config_version: int


def tooling_fingerprint() -> str:
    """A hash of giml's pinned tool versions (JaCoCo, PIT, the enforcer, ...), which change what a build does."""
    pinned = {name: dataclasses.asdict(plugin) for name, plugin in sorted(tooling().items())}
    return hashlib.sha256(canonical_json(pinned).encode("utf-8")).hexdigest()


def maven_version(environ: Mapping[str, str]) -> str:
    """The version `mvn` reports, or "unknown" when it cannot be run."""
    mvn = shutil.which("mvn", path=environ.get("PATH", ""))
    if mvn is None:
        return "unknown"
    result = subprocess.run([mvn, "--version"], capture_output=True, text=True, timeout=60, check=False, env=dict(environ))
    match = _MAVEN_VERSION.search(result.stdout) if result.returncode == 0 else None
    return match.group(1) if match else "unknown"


def stage_key_parts(worktree: Path, stage: str, environment: BuildEnvironment, dependencies: str | None = None) -> dict[str, Any]:
    """What decides a stage's outcome. Paths are never part of it, so equal content shares a result.

    ``head_tree`` and ``changes`` (a hash of the uncommitted changes to tracked files, that is the
    candidate's POM edits) together say what the worktree contains; untracked build output does not
    count. ``dependencies`` is a hash of the resolved dependency list, when the caller has one.
    """
    git = Git(worktree)
    changes = hashlib.sha256(git.out("diff", "HEAD", "--binary", "--no-ext-diff").encode("utf-8")).hexdigest()
    return {"stage": stage, "head_tree": git.out("rev-parse", "HEAD^{tree}"), "changes": changes,
            "dependencies": dependencies, "jdk": environment.jdk, "maven": environment.maven,
            "tooling": environment.tooling, "config_version": environment.config_version}  # fmt: skip


@dataclass
class CacheMetrics:
    stages: dict[str, dict[str, float]] = field(default_factory=dict)

    def record(self, stage: str, hit: bool, seconds: float) -> None:
        entry = self.stages.setdefault(stage, {"hits": 0, "misses": 0, "seconds_saved": 0.0, "seconds_spent": 0.0})
        entry["hits" if hit else "misses"] += 1
        entry["seconds_saved" if hit else "seconds_spent"] += seconds

    def _total(self, name: str) -> float:
        return sum(entry[name] for entry in self.stages.values())

    @property
    def hits(self) -> int:
        return int(self._total("hits"))

    @property
    def misses(self) -> int:
        return int(self._total("misses"))

    @property
    def seconds_saved(self) -> float:
        return self._total("seconds_saved")

    @property
    def hit_rate(self) -> float:
        return self.hits / (self.hits + self.misses) if self.hits + self.misses else 0.0

    def as_dict(self) -> dict:
        return {"hits": self.hits, "misses": self.misses, "hit_rate": self.hit_rate, "seconds_saved": self.seconds_saved,
                "seconds_spent": self._total("seconds_spent"), "stages": self.stages}  # fmt: skip


def _entry(outcome: StageOutcome) -> dict[str, Any]:
    failure = outcome.failure
    return {"schema": SCHEMA, "stage": outcome.stage, "passed": outcome.passed, "duration_seconds": outcome.duration_seconds,
            "log": str(outcome.log_path), "details": outcome.details,
            "failure": {"class": failure.failure_class, "signature": failure.signature, "key_lines": list(failure.key_lines)}
            if failure else None}  # fmt: skip


def _valid(entry: dict[str, Any] | None) -> bool:
    return entry is not None and entry.get("schema") == SCHEMA and {"stage", "passed", "duration_seconds", "log", "failure", "details"} <= entry.keys()


class CachingBuildRunner:
    """A BuildRunner that answers a repeated stage from the cache."""

    def __init__(self, inner: BuildRunner, cache: ResultCache, key_parts: Callable[[Path, str], dict[str, Any]],
                 logs_dir: Path) -> None:  # fmt: skip
        self.inner, self.cache, self.key_parts, self.logs_dir = inner, cache, key_parts, logs_dir
        self._metrics = CacheMetrics()
        self._hits = 0

    def metrics(self) -> CacheMetrics:
        return self._metrics

    def run_stage(self, worktree: Path, stage: str, timeout_seconds: int) -> StageOutcome:
        key = self.cache.key(self.key_parts(worktree, stage))
        entry = self.cache.get(key)
        if _valid(entry):
            return self._hit(stage, entry, key)
        outcome = dataclasses.replace(self.inner.run_stage(worktree, stage, timeout_seconds), cache_key=key)
        self._metrics.record(stage, False, outcome.duration_seconds)
        if outcome.passed or outcome.failure_class not in _NOT_CACHED:
            self.cache.put(key, _entry(outcome))
        return outcome

    def _hit(self, stage: str, entry: dict[str, Any], key: str) -> StageOutcome:
        self._hits += 1
        failure = entry["failure"]
        log = self.logs_dir / f"cache-hit-{self._hits:02d}-{stage}.log"
        text = f"cache hit: outcome of {entry['log']}, {entry['duration_seconds']} s originally\n"
        if failure:
            text += f"failure: {failure['class']} {failure['signature']}\nkey lines:\n" + "".join(f"  {line}\n" for line in failure["key_lines"])
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(text, encoding="utf-8")
        self._metrics.record(stage, True, entry["duration_seconds"])
        restored = Failure(failure["class"], failure["signature"], tuple(failure["key_lines"])) if failure else None
        return StageOutcome(stage, entry["passed"], entry["duration_seconds"], log, restored, cache_hit=True,
                            details=entry["details"], cache_key=key)  # fmt: skip
