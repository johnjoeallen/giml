"""The naive baseline giml is compared with (spec section 14): each dependency bumped to its newest release on its own.

Every declared, editable dependency that has a newer release gets one change to that release (any major, no
cooldown, no CVE or API filtering) and is built independently from the baseline state with the same verification
as giml's trials (without PIT, and without failing on a worse exposure, since the naive approach does not look).
The result says what that costs and gets: builds spent, how many bumps passed, which advisories they cleared and
how many dependencies were touched, to set beside what giml did.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from giml.plan.analysis import Analysis, newest_release
from giml.plan.candidates import EDIT
from giml.plan.execute import verdict_of
from giml.plan.steps import site_text
from giml.maven.pom_change import SetVersion
from giml.plan.trial import TrialResult


@dataclass(frozen=True)
class NaiveBump:
    coordinates: tuple[str, ...]  # what shares the edited declaration
    from_version: str
    to_version: str
    passed: bool
    failed_stage: str | None
    builds: int


@dataclass(frozen=True)
class NaiveComparison:
    bumps: tuple[NaiveBump, ...]
    cleared: tuple[str, ...]  # advisories gone from the tree of at least one passing bump

    @property
    def touched(self) -> int:
        return len(self.bumps)

    @property
    def passed(self) -> int:
        return sum(1 for b in self.bumps if b.passed)

    @property
    def builds(self) -> int:
        return sum(b.builds for b in self.bumps)

    def as_dict(self) -> dict:
        return {"touched": self.touched, "passed": self.passed, "failed": self.touched - self.passed, "builds": self.builds,
                "cleared": list(self.cleared),
                "bumps": [{"coordinates": list(b.coordinates), "from": b.from_version, "to": b.to_version, "passed": b.passed,
                           "failed_stage": b.failed_stage, "builds": b.builds} for b in self.bumps]}  # fmt: skip


def naive_changes(analysis: Analysis) -> list[tuple[tuple[str, ...], str, str, list[SetVersion]]]:
    """(coordinates, current, newest, edits) per declaration that can be edited and has a newer release, by coordinate."""
    groups: dict[frozenset, list] = {}
    for plan in analysis.plans:
        newest = newest_release(analysis.available.get(plan.coordinate))
        if plan.change != EDIT or not plan.sites or newest is None or newest == plan.version:
            continue
        groups.setdefault(frozenset((s.pom, s.span) for s in plan.sites), []).append((plan, newest))
    found = []
    for members in groups.values():
        plan, newest = sorted(members, key=lambda m: str(m[0].coordinate))[0]
        found.append((tuple(str(p.coordinate) for p, _ in members), plan.version, newest,
                      [SetVersion(site, site_text(site), newest) for site in plan.sites]))
    return sorted(found)


def compare_naive(analysis: Analysis, verify: Callable[[list[SetVersion]], TrialResult], changes=None) -> NaiveComparison:
    """Verify every naive bump on its own with ``verify`` (a trial from the baseline state).

    ``changes`` are ``naive_changes(analysis)`` taken before anything was committed: they hold the text each edit expects,
    which is read from the files when they are made.
    """
    before = {f.advisory_id for d in analysis.exposure.dependencies for f in d.findings}
    bumps: list[NaiveBump] = []
    cleared: set[str] = set()
    for coordinates, current, newest, edits in (naive_changes(analysis) if changes is None else changes):
        result = verify(edits)
        bumps.append(NaiveBump(coordinates, current, newest, result.passed, result.failed_stage, verdict_of(result).cost))
        if result.passed and result.exposure is not None:
            cleared |= before - {f.advisory_id for d in result.exposure.dependencies for f in d.findings}
    return NaiveComparison(tuple(bumps), tuple(sorted(cleared)))
