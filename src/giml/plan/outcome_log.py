"""Log every stage attempt as a labelled example (spec sections 11 and 15).

A run's baseline is logged here; the planner logs each candidate the same way from M5. Per stage that
ran there is an attempt row (with the cache key, so hits are visible) and one example: features that
describe the situation, a label that says how the stage ended, the project as its split group, and a
hash that drops exact repeats. Stages that did not run are not logged, and neither is an infrastructure
failure that a retry replaced: it says nothing about the project.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from giml.core.interfaces import StateStore
from giml.core.model import BuildAttemptRecord, CandidateStateRecord, ExampleRecord, GateResultRecord
from giml.plan.baseline import NOT_RUN, Baseline, StageBaseline
from giml.store.result_cache import canonical_json

_ORACLE_KEYS = ("unit_line_coverage", "unit_branch_coverage", "pit_test_strength", "pit_mutation_coverage")


@dataclass(frozen=True)
class RewindFacts:
    commit: str
    commit_date: str


@dataclass(frozen=True)
class LoggedBaseline:
    state_id: str
    attempts: int
    examples_new: int  # examples that were not exact repeats of one already logged


def oracle_strength(record: GateResultRecord | None) -> dict[str, float] | None:
    """The measured test-quality figures of a stored assessment, the strength of the oracle (spec 15)."""
    if record is None:
        return None
    measured = json.loads(record.json).get("measured")
    if not measured:
        return None
    return {key: measured[key] for key in _ORACLE_KEYS if key in measured}


def _features(stage: StageBaseline, tier: str | None, oracle: dict[str, float] | None, rewind: RewindFacts | None,
              jdk: str | None) -> dict[str, Any]:  # fmt: skip
    outcome = stage.outcome
    return {"kind": "baseline", "stage": stage.stage, "failure_class": outcome.failure_class, "signature": outcome.signature,
            "key_lines": list(outcome.failure.key_lines) if outcome.failure else [],
            "log_path": str(outcome.log_path), "duration_seconds": outcome.duration_seconds, "cache_hit": outcome.cache_hit,
            "retries": stage.attempts - 1, "tier": tier, "oracle": oracle,
            "rewind": {"is_rewind": rewind is not None, "commit": rewind.commit if rewind else None,
                       "commit_date": rewind.commit_date if rewind else None},
            "jdk": jdk, "violations": [v.identity for v in outcome.violations]}  # fmt: skip


def log_baseline(
    store: StateStore,
    run_id: str,
    project_id: str,
    baseline: Baseline,
    *,
    tier: str | None,
    oracle: dict[str, float] | None,
    rewind: RewindFacts | None,
    jdk: str | None,
) -> LoggedBaseline:
    state_id = f"{run_id}:baseline"
    store.save_state(CandidateStateRecord(state_id, run_id, None, "[]", "baseline_verified" if baseline.upgradeable else "baseline_failed"))
    attempts = new_examples = 0
    for stage in baseline.stages:
        outcome = stage.outcome
        if stage.status == NOT_RUN or outcome is None:
            continue
        label = "pass" if outcome.passed else "fail"
        store.save_attempt(BuildAttemptRecord(f"{state_id}:{stage.stage}:1", state_id, stage.stage, label, outcome.failure_class,
                                              outcome.signature, outcome.cache_key, outcome.cache_hit,
                                              round(outcome.duration_seconds * 1000), str(outcome.log_path)))  # fmt: skip
        attempts += 1
        dedup = hashlib.sha256(canonical_json({"scope": "baseline", "project": project_id, "stage": stage.stage, "outcome": label,
                                               "signature": outcome.signature, "rewind_commit": rewind.commit if rewind else None}).encode()).hexdigest()  # fmt: skip
        new_examples += store.save_example(ExampleRecord(
            f"{run_id}:{stage.stage}", run_id, canonical_json(_features(stage, tier, oracle, rewind, jdk)),
            canonical_json({"stage": stage.stage, "outcome": label, "failure_class": outcome.failure_class}), project_id, dedup))  # fmt: skip
    return LoggedBaseline(state_id, attempts, new_examples)


def log_trial(store: StateStore, run_id: str, project_id: str, number: int, changes: list[dict[str, Any]], result,
              *, tier: str | None, oracle: dict[str, float] | None, jdk: str | None, now) -> list[dict[str, Any]]:  # fmt: skip
    """Log one candidate trial like the baseline (state, attempt and example per stage that ran) and remember a failure.

    ``changes`` describe what the trial applied (key, members, kind, from, to). A failed single-step trial adds to
    the knowledge of failed transitions; the returned entries say how many times each had failed before. A
    trial with several steps cannot say which one broke it, so it only teaches the examples.
    """
    state_id = f"{run_id}:trial-{number:03d}"
    status = "inconclusive" if result.inconclusive else "passed" if result.passed else "failed"
    store.save_state(CandidateStateRecord(state_id, run_id, f"{run_id}:baseline", canonical_json(changes), status))
    for outcome in result.outcomes:
        if outcome.retryable:
            continue  # the environment failed; it says nothing about the candidate
        label = "pass" if outcome.passed else "fail"
        store.save_attempt(BuildAttemptRecord(f"{state_id}:{outcome.stage}:1", state_id, outcome.stage, label, outcome.failure_class,
                                              outcome.signature, outcome.cache_key, outcome.cache_hit,
                                              round(outcome.duration_seconds * 1000), str(outcome.log_path)))  # fmt: skip
        features = {"kind": "candidate", "stage": outcome.stage, "changes": changes, "failure_class": outcome.failure_class,
                    "signature": outcome.signature, "key_lines": list(outcome.failure.key_lines) if outcome.failure else [],
                    "log_path": str(outcome.log_path), "duration_seconds": outcome.duration_seconds, "cache_hit": outcome.cache_hit,
                    "tier": tier, "oracle": oracle,
                    "jdk": jdk, "new_violations": [v.identity for v in result.new_violations]}  # fmt: skip
        dedup = hashlib.sha256(canonical_json({"scope": "candidate", "project": project_id, "stage": outcome.stage, "outcome": label,
                                               "signature": outcome.signature, "changes": changes}).encode()).hexdigest()  # fmt: skip
        store.save_example(ExampleRecord(f"{state_id}:{outcome.stage}", run_id, canonical_json(features),
                                         canonical_json({"stage": outcome.stage, "outcome": label, "failure_class": outcome.failure_class}),
                                         project_id, dedup))  # fmt: skip
    if result.passed or result.inconclusive or result.failure is None or len(changes) != 1 or changes[0]["from"] is None:
        return []
    change = changes[0]
    return [{"coordinate": member, "from": change["from"], "to": change["to"], "failure_class": result.failure.failure_class,
             "signature": result.failure.signature,
             "prior_failures": store.record_transition(member, change["from"], change["to"], result.failure.failure_class,
                                                       result.failure.signature, now)}
            for member in change["members"]]  # fmt: skip
