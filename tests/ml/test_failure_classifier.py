import pytest

sklearn = pytest.importorskip("sklearn")

from giml.ml.failure_classifier import FailureClassifier, ModelError

COMPILE_A = "[ERROR] COMPILATION ERROR :\n[ERROR] App.java:[3,20] cannot find symbol\n"
COMPILE_B = "[ERROR] COMPILATION ERROR :\n[ERROR] Other.java:[9,4] cannot find symbol\n"
UNIT_A = "[ERROR] Tests run: 3, Failures: 1\n[ERROR] AppTest.testX:12 expected:<1> but was:<2>\n"
UNIT_B = "[ERROR] Tests run: 5, Failures: 2\n[ERROR] OtherTest.testY:30 expected:<a> but was:<b>\n"
LOGS = [COMPILE_A, COMPILE_B, UNIT_A, UNIT_B]
LABELS = ["compile", "compile", "unit_test", "unit_test"]


def test_a_trained_classifier_separates_two_clear_classes():
    model = FailureClassifier.train(LOGS, LABELS)
    assert model.predict(COMPILE_A).label == "compile" and model.predict(UNIT_A).label == "unit_test"
    prediction = model.predict(UNIT_B)
    assert 0.0 <= prediction.confidence <= 1.0


def test_train_needs_matching_lengths_and_at_least_two_classes():
    with pytest.raises(ValueError, match="one label per log"):
        FailureClassifier.train(LOGS, ["compile"])
    with pytest.raises(ValueError, match="at least two different"):
        FailureClassifier.train([COMPILE_A, COMPILE_B], ["compile", "compile"])


def test_a_log_with_no_error_lines_still_gets_a_prediction():
    model = FailureClassifier.train(LOGS, LABELS)
    prediction = model.predict("[INFO] BUILD SUCCESS\n")
    assert prediction.label in {"compile", "unit_test"}


def test_save_and_load_round_trips_and_writes_a_checksum(tmp_path):
    model = FailureClassifier.train(LOGS, LABELS)
    path = tmp_path / "model.joblib"
    model.save(path)
    assert path.with_name("model.joblib.sha256").is_file()
    loaded = FailureClassifier.load(path)
    assert loaded.predict(COMPILE_A).label == model.predict(COMPILE_A).label


def test_loading_without_a_checksum_file_is_refused(tmp_path):
    model = FailureClassifier.train(LOGS, LABELS)
    path = tmp_path / "model.joblib"
    model.save(path)
    path.with_name("model.joblib.sha256").unlink()
    with pytest.raises(ModelError, match="no checksum file"):
        FailureClassifier.load(path)


def test_a_tampered_model_file_is_refused(tmp_path):
    model = FailureClassifier.train(LOGS, LABELS)
    path = tmp_path / "model.joblib"
    model.save(path)
    path.write_bytes(path.read_bytes() + b"\x00")
    with pytest.raises(ModelError, match="does not match"):
        FailureClassifier.load(path)


def test_a_model_from_a_different_feature_version_is_refused(tmp_path, monkeypatch):
    import giml.ml.failure_classifier as module

    model = FailureClassifier.train(LOGS, LABELS)
    path = tmp_path / "model.joblib"
    model.save(path)
    monkeypatch.setattr(module, "FEATURE_VERSION", module.FEATURE_VERSION + 1)
    with pytest.raises(ModelError, match="feature version"):
        FailureClassifier.load(path)
