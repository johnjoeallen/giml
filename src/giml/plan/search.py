"""The conservative search over dependency ladders (spec section 8.3), independent of Maven and git.

Each ladder is one dependency's steps in the order to try them (patch, then minor, then major where
permitted). The search verifies every ladder on its own and accepts the first step that passes, then
combines the accepted steps and verifies the combination. If that fails the steps interact: group
testing (ddmin) isolates a minimal set of culprits, each is offered its next ladder step in the
context of the others, and the last one that still cannot be made to fit is deferred, so the earlier,
more important fix stays. A verified combination is the only thing ever committed.

Verification is injected, so the search is tested with a fake that has a hidden truth, and it is
budgeted: a verdict says how many builds it cost (none when it came from the cache), the search stops
at the build or time budget, and everything it did not get to is deferred with the reason.
"""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from math import ceil


@dataclass(frozen=True)
class Step:
    key: str  # the ladder (dependency) it belongs to
    label: str  # for people: "org.x:lib 1.0 → 1.0.3 (cve_patch)"
    kind: str  # cve_patch, cve_minor, ...
    rank: int  # position in its ladder; 0 is tried first
    hint: str = ""  # why it is expected to fail (for example an API break the project uses); such steps are tried last


@dataclass(frozen=True)
class Ladder:
    key: str
    steps: tuple[Step, ...]  # only steps a build may try; blocked ones are not here
    order: int  # processing order: important fixes first
    note: str = ""  # why there are no steps, when there are none
    fallback: tuple[Step, ...] = ()  # steps expected to fail: built only if nothing in ``steps`` passed
    highest: bool = False  # steps ascend and the newest that passes is wanted (the `latest` strategy), not the first


@dataclass(frozen=True)
class Verdict:
    passed: bool
    inconclusive: bool  # the environment failed: neither a pass nor a failure of the candidate
    reason: str
    cost: int  # builds it took; 0 when answered from the cache


Verify = Callable[[Mapping[str, Step]], Verdict]


@dataclass(frozen=True)
class Deferral:
    reason: str
    tried: tuple[tuple[int, str], ...]  # (rank, why it failed) for every step that was built
    interaction: bool = False  # deferred because of what it did together with others, so worth another try once they settle


@dataclass(frozen=True)
class TraceEntry:
    phase: str  # ladder, combine, isolate or advance
    key: str  # the ladder for a single step, else the keys joined with "+"
    step_rank: int | None
    passed: bool
    reason: str


@dataclass(frozen=True)
class Outcome:
    accepted: dict[str, Step]  # verified together, in ladder order
    deferred: dict[str, Deferral]
    builds: int
    stop_reason: str  # complete, budget_builds, budget_time or inconclusive
    stop_detail: str
    trace: tuple[TraceEntry, ...]


@dataclass(frozen=True)
class Budget:
    max_builds: int
    max_minutes: int | None
    clock: Callable[[], datetime.datetime]
    started: datetime.datetime

    def exhausted(self, builds: int) -> str | None:
        if builds >= self.max_builds:
            return "budget_builds"
        if self.max_minutes is not None and self.clock() - self.started >= datetime.timedelta(minutes=self.max_minutes):
            return "budget_time"
        return None

    def untried(self, stop: str, detail: str = "") -> str:
        if stop == "budget_builds":
            return f"not tried: the build budget ({self.max_builds}) was used up"
        if stop == "budget_time":
            return f"not tried: the time budget ({self.max_minutes} minutes) was used up"
        return f"not tried: the search stopped ({detail})"


def ddmin(items: Sequence, fails: Callable[[list], bool]) -> list:
    """A minimal failing subset of ``items`` (Zeller's delta debugging), given that all of them fail together."""
    known: dict[tuple, bool] = {}

    def failing(subset: list) -> bool:
        if tuple(subset) not in known:
            known[tuple(subset)] = fails(subset)
        return known[tuple(subset)]

    items, n = list(items), 2
    while len(items) >= 2:
        size = max(1, ceil(len(items) / n))
        subsets = [items[i : i + size] for i in range(0, len(items), size)]
        reduced = False
        for subset in subsets:
            if failing(subset):
                items, n, reduced = subset, 2, True
                break
        if not reduced:
            for subset in subsets:
                complement = [x for x in items if x not in subset]
                if complement and failing(complement):
                    items, n, reduced = complement, max(n - 1, 2), True
                    break
        if not reduced:
            if n >= len(items):
                break
            n = min(len(items), n * 2)
    return items


class _Stop(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason, self.detail = reason, detail


@dataclass
class _Run:
    verify: Verify
    budget: Budget
    builds: int = 0
    strict: Verify | None = None  # a costlier verification that only what the search means to keep must pass
    cache: dict[tuple[bool, frozenset], Verdict] = field(default_factory=dict)
    trace: list[TraceEntry] = field(default_factory=list)

    def check(self, chosen: Mapping[str, Step], phase: str, strict: bool = False) -> Verdict:
        """Verify a set of steps once: repeats are answered from memory, the budget is enforced, inconclusive stops."""
        strict = strict and self.strict is not None
        signature = (strict, frozenset((key, step.rank) for key, step in chosen.items()))
        if signature in self.cache:
            return self.cache[signature]
        stop = self.budget.exhausted(self.builds)
        if stop:
            raise _Stop(stop)
        verdict = (self.strict if strict else self.verify)(dict(sorted(chosen.items())))
        self.builds += verdict.cost
        self.cache[signature] = verdict
        single = next(iter(chosen.values())) if len(chosen) == 1 else None
        self.trace.append(TraceEntry(phase, single.key if single else "+".join(sorted(chosen)), single.rank if single else None,
                                     verdict.passed, verdict.reason))  # fmt: skip
        if verdict.inconclusive:
            raise _Stop("inconclusive", verdict.reason)
        return verdict


def _bisect(run: _Run, ladder: Ladder, accepted: dict[str, Step], tried: list[tuple[int, str]]) -> int | None:
    """The index of the newest step that passes in the context of what is already accepted, or None.

    The newest is tried first; if it fails the search chops backwards to a step that passes, then narrows
    towards the newest passing one (forward towards the last failure, back again) until the two are adjacent.
    The oldest step failing ends the search: nothing in this ladder can be taken.
    """

    def passes(index: int) -> bool:
        verdict = run.check({**accepted, ladder.key: ladder.steps[index]}, "ladder")
        if not verdict.passed:
            tried.append((ladder.steps[index].rank, verdict.reason))
        return verdict.passed

    top = len(ladder.steps) - 1
    if passes(top):
        return top
    low, high = -1, top
    while high - low > 1:
        middle = (low + high) // 2
        low, high = (middle, high) if passes(middle) else (low, middle)
    return low if low >= 0 else None


def _attempt(run: _Run, ladder: Ladder, accepted: dict[str, Step], tried: list[tuple[int, str]]) -> Step | None:
    """The step a ladder takes: the first that passes alone, or the newest that passes in context (``highest``)."""
    if ladder.highest:
        found = _bisect(run, ladder, accepted, tried)
        return ladder.steps[found] if found is not None else None
    for step in ladder.steps:
        verdict = run.check({ladder.key: step}, "ladder")
        if verdict.passed:
            return step
        tried.append((step.rank, verdict.reason))
    return None


def _no_step_reason(ladder: Ladder, tried: list[tuple[int, str]]) -> str:
    if ladder.highest:
        return f"no version passed, not even the oldest ({ladder.steps[0].label}): {tried[-1][1]}"
    kinds = "; ".join(f"{s.kind} failed: {reason}" for s, (_, reason) in zip(ladder.steps, tried, strict=False))
    return f"no fixing version passed ({kinds})"


def _fix_ladders(run: _Run, ladders: list[Ladder]) -> tuple[dict[str, Step], dict[str, Deferral], _Stop | None]:
    accepted: dict[str, Step] = {}
    deferred: dict[str, Deferral] = {}
    stopped: _Stop | None = None
    for ladder in ladders:
        if stopped is not None:
            deferred[ladder.key] = Deferral(run.budget.untried(stopped.reason, stopped.detail), ())
            continue
        if not ladder.steps and not ladder.fallback:
            deferred[ladder.key] = Deferral(ladder.note or "no usable step", ())
            continue
        tried: list[tuple[int, str]] = []
        try:
            step = _attempt(run, ladder, accepted, tried) if ladder.steps else None
            if step is None and ladder.fallback:
                # Steps that were expected to fail (an API break the project uses) are built after all when nothing else passed.
                fallback = dataclasses.replace(ladder, steps=ladder.fallback, fallback=())
                step = _attempt(run, fallback, accepted, tried)
                ladder = ladder if step is not None else fallback
            if step is not None:
                accepted[ladder.key] = step
            else:
                deferred[ladder.key] = Deferral(_no_step_reason(ladder, tried), tuple(tried))
        except _Stop as stop:
            stopped = stop
            reason = f"could not be judged: {stop.detail}" if stop.reason == "inconclusive" else run.budget.untried(stop.reason)
            deferred[ladder.key] = Deferral(reason, tuple(tried))
    return accepted, deferred, stopped


def _combine(run: _Run, accepted: dict[str, Step], ladders: dict[str, Ladder], deferred: dict[str, Deferral]) -> _Stop | None:
    """Verify the accepted steps together; on failure isolate the culprits and advance or defer them.

    With a strict verification even a single accepted step is checked, and every check here uses it.
    """
    while len(accepted) >= (1 if run.strict else 2):
        try:
            if run.check(accepted, "combine", True).passed:
                return None
            culprits = ddmin(list(accepted), lambda subset: not run.check({k: accepted[k] for k in subset}, "isolate", True).passed)
            culprits = sorted(culprits, key=lambda key: ladders[key].order, reverse=True)  # the least important first
            if not _advance(run, accepted, ladders, culprits):
                last = culprits[0]
                others = [k for k in culprits if k != last]
                names = ", ".join(f"{k} ({accepted[k].label})" for k in others) or "the others"
                deferred[last] = Deferral(f"interacts with {names}; no further step passed", (), interaction=True)
                del accepted[last]
        except _Stop as stop:
            _keep_first_verified(accepted, ladders, deferred, run.budget.untried(stop.reason, stop.detail))
            return stop
    return None


def _repromote(run: _Run, accepted: dict[str, Step], ladders: dict[str, Ladder], deferred: dict[str, Deferral]) -> _Stop | None:
    """Offer each ladder deferred for an interaction another try in the context of what finally stayed, until nothing changes.

    What was deferred was blamed on a combination that has since changed (other culprits backed off or advanced),
    so its steps may pass now. Every attempt is verified strictly against the full accepted set.
    """
    progress = True
    while progress:
        progress = False
        for key in [k for k, d in deferred.items() if d.interaction]:
            ladder = ladders[key]
            try:
                steps = [ladder.steps[found]] if ladder.highest and (found := _bisect(run, ladder, accepted, [])) is not None \
                    else [] if ladder.highest else ladder.steps  # fmt: skip
                for step in steps:
                    if run.check({**accepted, key: step}, "repromote", True).passed:
                        accepted[key] = step
                        del deferred[key]
                        progress = True
                        break
            except _Stop as stop:
                return stop
    return None


def _advance(run: _Run, accepted: dict[str, Step], ladders: dict[str, Ladder], culprits: list[str]) -> bool:
    """Offer each culprit its next ladder steps in the context of the others; True when the whole set passes."""
    for key in culprits:
        ladder, rank = ladders[key], accepted[key].rank
        for step in ([s for s in reversed(ladder.steps) if s.rank < rank] if ladder.highest else [s for s in ladder.steps if s.rank > rank]):
            candidate = {**accepted, key: step}
            if run.check(candidate, "advance", True).passed:
                accepted[key] = step
                return True
    return False


def _keep_first_verified(accepted: dict[str, Step], ladders: dict[str, Ladder], deferred: dict[str, Deferral], why: str) -> None:
    """Without a verified combination only the most important step, verified alone, can be kept."""
    keep = min(accepted, key=lambda key: ladders[key].order)
    for key in [k for k in accepted if k != keep]:
        deferred[key] = Deferral(f"the combination was not verified: {why.removeprefix('not tried: ')}", ())
        del accepted[key]


def search(ladders: Sequence[Ladder], verify: Verify, budget: Budget, strict: Verify | None = None) -> Outcome:
    ordered = sorted(ladders, key=lambda ladder: ladder.order)
    by_key = {ladder.key: ladder for ladder in ordered}
    run = _Run(verify, budget, strict=strict)
    accepted, deferred, stopped = _fix_ladders(run, ordered)
    if stopped is None or stopped.reason != "inconclusive":
        combine_stop = _combine(run, accepted, by_key, deferred)
        stopped = stopped or combine_stop
        if stopped is None:
            stopped = _repromote(run, accepted, by_key, deferred)
    ordered_accepted = {ladder.key: accepted[ladder.key] for ladder in ordered if ladder.key in accepted}
    return Outcome(ordered_accepted, deferred, run.builds, stopped.reason if stopped else "complete",
                   stopped.detail if stopped else "", tuple(run.trace))  # fmt: skip
