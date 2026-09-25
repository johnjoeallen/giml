import datetime
import itertools

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from giml.plan.search import Budget, Ladder, Step, Verdict, ddmin, search

T0 = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.UTC)


def ladder(key, *levels, order=0, note=""):
    steps = tuple(Step(key, f"{key} step{rank}", kind, rank) for rank, kind in enumerate(levels))
    return Ladder(key, steps, order, note)


class Fake:
    """A verifier with a hidden truth: steps that fail alone and combinations that fail together."""

    def __init__(self, bad=(), interactions=(), inconclusive=(), cost=1):
        self.bad, self.interactions, self.inconclusive, self.cost = set(bad), [set(i) for i in interactions], set(inconclusive), cost
        self.calls: list[frozenset] = []

    def __call__(self, chosen):
        present = {(key, step.rank) for key, step in chosen.items()}
        self.calls.append(frozenset(present))
        if present & self.inconclusive:
            return Verdict(False, True, "network down", self.cost)
        for item in sorted(present & self.bad):
            return Verdict(False, False, f"{item[0]} step{item[1]} failed compile", self.cost)
        for group in self.interactions:
            if group <= present:
                return Verdict(False, False, "unit_test failed in combination", self.cost)
        return Verdict(True, False, "", self.cost)


def budget(builds=100, minutes=None, clock=None):
    return Budget(builds, minutes, clock or (lambda: T0), T0)


def run(ladders, fake, **kw):
    return search(ladders, fake, kw.get("budget", budget()))


PATCH_MINOR = ("cve_patch", "cve_minor", "cve_major")


def test_every_first_step_passes_so_each_is_built_once_and_the_combination_once():
    fake = Fake()
    outcome = run([ladder("a", *PATCH_MINOR, order=0), ladder("b", *PATCH_MINOR, order=1), ladder("c", *PATCH_MINOR, order=2)], fake)
    assert {k: s.rank for k, s in outcome.accepted.items()} == {"a": 0, "b": 0, "c": 0}
    assert outcome.deferred == {} and outcome.stop_reason == "complete" and outcome.builds == 4
    assert [len(c) for c in fake.calls] == [1, 1, 1, 3]


def test_a_single_dependency_needs_no_combination_build():
    fake = Fake()
    outcome = run([ladder("a", *PATCH_MINOR)], fake)
    assert outcome.builds == 1 and list(outcome.accepted) == ["a"] and len(fake.calls) == 1


def test_a_failing_step_falls_through_to_the_next_level_and_stops_at_the_first_pass():
    fake = Fake(bad=[("a", 0)])
    outcome = run([ladder("a", *PATCH_MINOR)], fake)
    assert outcome.accepted["a"].rank == 1 and outcome.builds == 2  # the major step was never built
    assert [(t.key, t.step_rank, t.passed) for t in outcome.trace] == [("a", 0, False), ("a", 1, True)]
    assert outcome.trace[0].reason == "a step0 failed compile"


def test_a_dependency_whose_every_step_fails_is_deferred_with_what_was_tried():
    outcome = run([ladder("a", *PATCH_MINOR)], Fake(bad=[("a", 0), ("a", 1), ("a", 2)]))
    assert outcome.accepted == {} and outcome.builds == 3
    deferral = outcome.deferred["a"]
    assert [(rank, reason) for rank, reason in deferral.tried] == [(0, "a step0 failed compile"), (1, "a step1 failed compile"), (2, "a step2 failed compile")]
    assert "no fixing version passed" in deferral.reason and "cve_patch" in deferral.reason and "cve_major" in deferral.reason


def test_a_ladder_with_no_usable_step_is_deferred_with_its_note_and_costs_nothing():
    fake = Fake()
    outcome = run([ladder("a", note="the only fix is a major update and major_updates is disallowed")], fake)
    assert outcome.deferred["a"].reason == "the only fix is a major update and major_updates is disallowed" and fake.calls == [] and outcome.builds == 0


def test_an_interaction_is_isolated_and_the_riskier_later_step_moves_on():
    # a0 and b0 pass alone but fail together; b1 works with a0.
    fake = Fake(interactions=[{("a", 0), ("b", 0)}])
    outcome = run([ladder("a", *PATCH_MINOR, order=0), ladder("b", *PATCH_MINOR, order=1), ladder("c", *PATCH_MINOR, order=2)], fake)
    assert {k: s.rank for k, s in outcome.accepted.items()} == {"a": 0, "b": 1, "c": 0} and outcome.deferred == {}
    assert any(t.phase == "isolate" for t in outcome.trace) and outcome.stop_reason == "complete"


def test_when_no_step_of_the_later_culprit_helps_it_is_deferred_and_the_earlier_fix_stays():
    fake = Fake(interactions=[{("a", i), ("b", j)} for i in range(3) for j in range(3)])  # nothing fits together
    outcome = run([ladder("a", *PATCH_MINOR, order=0), ladder("b", *PATCH_MINOR, order=1)], fake)
    assert list(outcome.accepted) == ["a"] and "b" in outcome.deferred
    assert "interacts with a" in outcome.deferred["b"].reason and "a step0" in outcome.deferred["b"].reason


def test_only_the_interacting_pair_is_touched_in_a_larger_set():
    fake = Fake(interactions=[{("a", 0), ("d", 0)}])
    ladders = [ladder(k, *PATCH_MINOR, order=i) for i, k in enumerate("abcde")]
    outcome = run(ladders, fake)
    assert {k: s.rank for k, s in outcome.accepted.items()} == {"a": 0, "b": 0, "c": 0, "d": 1, "e": 0}


def test_an_inconclusive_verdict_stops_the_search_and_says_so():
    outcome = run([ladder("a", *PATCH_MINOR, order=0), ladder("b", *PATCH_MINOR, order=1)], Fake(inconclusive=[("a", 0)]))
    assert outcome.stop_reason == "inconclusive" and outcome.accepted == {}
    assert "network down" in outcome.stop_detail and {"a", "b"} == set(outcome.deferred)
    assert "not tried" in outcome.deferred["b"].reason


def test_the_build_budget_stops_the_search_and_untried_ladders_are_deferred():
    fake = Fake(bad=[("a", 0)])
    outcome = run([ladder("a", *PATCH_MINOR, order=0), ladder("b", *PATCH_MINOR, order=1)], fake, budget=budget(builds=2))
    assert outcome.stop_reason == "budget_builds" and outcome.builds == 2
    assert outcome.deferred["b"].reason == "not tried: the build budget (2) was used up" and outcome.accepted["a"].rank == 1


def test_a_build_that_was_answered_from_the_cache_costs_nothing():
    outcome = run([ladder(k, *PATCH_MINOR, order=i) for i, k in enumerate("abc")], Fake(cost=0), budget=budget(builds=1))
    assert outcome.stop_reason == "complete" and outcome.builds == 0 and len(outcome.accepted) == 3


def test_the_time_budget_stops_the_search():
    ticks = itertools.count()
    clock = lambda: T0 + datetime.timedelta(minutes=next(ticks) * 30)  # noqa: E731
    outcome = run([ladder(k, *PATCH_MINOR, order=i) for i, k in enumerate("abc")], Fake(), budget=Budget(100, 60, clock, T0))
    assert outcome.stop_reason == "budget_time" and len(outcome.accepted) < 3
    assert any("time budget (60 minutes)" in d.reason for d in outcome.deferred.values())


def test_ladders_are_processed_in_their_given_order_whatever_the_list_order():
    fake = Fake()
    run([ladder("b", *PATCH_MINOR, order=1), ladder("a", *PATCH_MINOR, order=0)], fake)
    assert [next(iter(c))[0] for c in fake.calls[:2]] == ["a", "b"]


def test_identical_subsets_are_never_built_twice():
    fake = Fake(interactions=[{("a", 0), ("b", 0)}])
    run([ladder("a", *PATCH_MINOR, order=0), ladder("b", *PATCH_MINOR, order=1)], fake)
    assert len(fake.calls) == len(set(fake.calls))


def test_the_search_is_deterministic():
    def once():
        fake = Fake(interactions=[{("a", 0), ("c", 0)}], bad=[("b", 0)])
        outcome = run([ladder(k, *PATCH_MINOR, order=i) for i, k in enumerate("abc")], fake)
        return outcome.accepted, outcome.deferred, [(t.key, t.step_rank, t.phase, t.passed) for t in outcome.trace], fake.calls

    assert once() == once()


# ddmin --------------------------------------------------------------------------------------------------------------


def test_ddmin_finds_a_pair_among_many():
    calls = []

    def fails(subset):
        calls.append(tuple(subset))
        return {3, 7} <= set(subset)

    assert sorted(ddmin(list(range(10)), fails)) == [3, 7] and len(calls) <= 30


def test_ddmin_of_a_single_failing_item():
    assert ddmin(["x"], lambda s: True) == ["x"] and ddmin(["x", "y"], lambda s: "y" in s) == ["y"]


@settings(max_examples=120, deadline=None)
@given(st.integers(2, 24), st.integers(2, 4), st.randoms(use_true_random=False))
def test_ddmin_finds_the_hidden_minimal_set_within_a_bound(n, k, rng):
    k = min(k, n)
    hidden = set(rng.sample(range(n), k))
    calls = []

    def fails(subset):
        calls.append(len(subset))
        return hidden <= set(subset)

    found = ddmin(list(range(n)), fails)
    assert set(found) == hidden
    assert len(calls) <= 2 * n * max(1, n.bit_length()) + 8  # a generous polynomial bound: it never degenerates into trying everything


def highest(key, count, order=0):
    steps = tuple(Step(key, f"{key} v{rank}", "latest_in_major", rank) for rank in range(count))
    return Ladder(key, steps, order, highest=True)


def threshold(limit):
    """Steps above ``limit`` fail: a monotone hidden truth."""
    return Fake(bad={("a", rank) for rank in range(limit + 1, 64)})


def test_the_newest_step_is_taken_when_it_passes_in_one_build():
    fake = Fake()
    outcome = run([highest("a", 8)], fake)
    assert outcome.accepted["a"].rank == 7 and outcome.builds == 1


@pytest.mark.parametrize("limit", range(8))
def test_a_failing_newest_step_is_chopped_back_to_the_newest_that_passes(limit):
    outcome = run([highest("a", 8)], threshold(limit))
    assert outcome.accepted["a"].rank == limit and outcome.stop_reason == "complete"
    assert outcome.builds <= 1 + 3 + 1  # the newest, log2(8) narrowing builds, at most one combination


def test_when_even_the_oldest_step_fails_the_ladder_stops_and_says_so():
    outcome = run([highest("a", 6)], Fake(bad={("a", rank) for rank in range(6)}))
    assert outcome.accepted == {} and "not even the oldest (a v0)" in outcome.deferred["a"].reason
    assert outcome.deferred["a"].tried[-1][0] == 0


def test_a_highest_ladder_is_verified_in_the_context_of_what_is_already_accepted():
    fake = Fake(interactions=[{("a", 3), ("b", 3)}])
    outcome = run([highest("a", 4, order=0), highest("b", 4, order=1)], fake)
    assert outcome.accepted["a"].rank == 3 and outcome.accepted["b"].rank == 2  # b backs off from the step that clashes with a
    assert all(len(call) <= 2 for call in fake.calls)


def test_an_interaction_found_at_the_combination_backs_a_highest_ladder_off_not_forward():
    fake = Fake(interactions=[{("a", 3), ("c", 0)}])
    outcome = run([highest("a", 4, order=1), ladder("c", "cve_patch", "cve_minor", order=0)], fake)
    assert outcome.accepted["a"].rank < 3 and outcome.accepted["c"].rank == 0
