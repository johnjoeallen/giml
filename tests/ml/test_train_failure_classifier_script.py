import sys
from pathlib import Path

import pytest

pytest.importorskip("sklearn")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import train_failure_classifier as script  # noqa: E402

from tests.maven.test_failures import SCENARIOS  # noqa: E402


def test_the_scripts_corpus_matches_the_rule_based_classifiers_test_fixtures():
    """If tests/maven/test_failures.py's SCENARIOS changes, this catches the script falling out of step."""
    assert script.SCENARIOS == SCENARIOS


def test_the_corpus_has_two_logs_per_class_and_reads_real_files():
    logs, labels = script.corpus()
    assert len(logs) == len(labels) == 10 and set(labels) == set(SCENARIOS.values())
    assert all(log.strip() for log in logs)


def test_leave_one_out_reports_real_numbers_for_both_classifiers():
    logs, labels = script.corpus()
    report = script.leave_one_out(logs, labels)
    assert report["examples"] == 10 and set(report["classes"]) == set(SCENARIOS.values())
    assert 0.0 <= report["trained_accuracy"] <= 1.0 and 0.0 <= report["rule_based_accuracy"] <= 1.0
    assert all(v.endswith("/2") for v in report["trained_by_class"].values())


def test_main_saves_a_model_when_asked(tmp_path, capsys):
    model_path = tmp_path / "m.joblib"
    sys.argv = ["train_failure_classifier.py", "--save", str(model_path)]
    assert script.main() == 0
    assert model_path.is_file() and model_path.with_name("m.joblib.sha256").is_file()
    out = capsys.readouterr().out
    assert "the trained classifier" in out and "model saved to" in out
