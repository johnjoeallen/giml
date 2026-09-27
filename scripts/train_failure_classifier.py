#!/usr/bin/env python3
"""Train and evaluate the ML failure classifier against the rule-based one (spec section 16, layer 1).

Corpus: giml's own real Maven failure logs, `tests/fixtures/logs`, each already used to test the rule-based
classifier (`tests/maven/test_failures.py`); the mapping here must be kept in step with that file's. Evaluation
is leave-one-out: each log is held out, a model trained on the rest predicts it, and that is compared with what
the rule-based classifier says on the very same log (which needs no training, so it is simply run). The report
says which one wins; per spec 16 the trained model is not used anywhere until it wins clearly.

The corpus is tiny (five classes, two near-duplicate copies each), so leave-one-out overstates real accuracy:
each held-out log's own near-duplicate is in the training set every time. This is reported honestly; growing
the corpus with real, distinct failures (via `giml export-examples`, once it carries raw log text) is the way
to make the number trustworthy.

Usage: `.venv/bin/python scripts/train_failure_classifier.py [--save PATH]`. Needs the `ml` extra installed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from giml.maven.failures import classify  # noqa: E402
from giml.ml.failure_classifier import FailureClassifier  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "logs"

# Kept in step with tests/maven/test_failures.py's SCENARIOS; the enforcer-*.log fixtures are excluded (they
# test the enforcer's own violation parser, not the failure classifier, and have no single agreed class here).
SCENARIOS = {
    "compile-error": "compile", "test-failure": "unit_test", "unresolvable": "resolution",
    "convergence": "enforcer_convergence", "duplicate-classes": "duplicate_classes",
}  # fmt: skip


def corpus() -> tuple[list[str], list[str]]:
    logs, labels = [], []
    for name, label in SCENARIOS.items():
        for copy in ("a", "b"):
            logs.append((FIXTURES / f"{name}-{copy}.log").read_text(encoding="utf-8"))
            labels.append(label)
    return logs, labels


def leave_one_out(logs: list[str], labels: list[str]) -> dict:
    trained_correct = rule_correct = 0
    trained_by_class: Counter = Counter()
    rule_by_class: Counter = Counter()
    total_by_class = Counter(labels)
    mistakes = []
    for held_out in range(len(logs)):
        train_logs = logs[:held_out] + logs[held_out + 1 :]
        train_labels = labels[:held_out] + labels[held_out + 1 :]
        model = FailureClassifier.train(train_logs, train_labels)
        trained = model.predict(logs[held_out])
        rule = classify(logs[held_out]).failure_class
        true = labels[held_out]
        trained_correct += trained.label == true
        rule_correct += rule == true
        trained_by_class[true] += trained.label == true
        rule_by_class[true] += rule == true
        if trained.label != true or rule != true:
            mistakes.append({"true": true, "trained": trained.label, "trained_confidence": round(trained.confidence, 3), "rule_based": rule})
    return {
        "examples": len(logs), "classes": sorted(total_by_class),
        "trained_accuracy": trained_correct / len(logs), "rule_based_accuracy": rule_correct / len(logs),
        "trained_by_class": {c: f"{trained_by_class[c]}/{total_by_class[c]}" for c in sorted(total_by_class)},
        "rule_based_by_class": {c: f"{rule_by_class[c]}/{total_by_class[c]}" for c in sorted(total_by_class)},
        "mistakes": mistakes,
    }  # fmt: skip


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--save", type=Path, help="also train on the whole corpus and save the model here (plus a .sha256)")
    args = parser.parse_args()
    logs, labels = corpus()
    report = leave_one_out(logs, labels)
    print(json.dumps(report, indent=2))
    verdict = "beats" if report["trained_accuracy"] > report["rule_based_accuracy"] else \
        "ties with" if report["trained_accuracy"] == report["rule_based_accuracy"] else "loses to"  # fmt: skip
    print(f"\nthe trained classifier {verdict} the rule-based one on this corpus "
          f"({report['trained_accuracy']:.0%} vs {report['rule_based_accuracy']:.0%})")  # fmt: skip
    if args.save:
        FailureClassifier.train(logs, labels).save(args.save)
        print(f"model saved to {args.save} (with {args.save.name}.sha256)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
