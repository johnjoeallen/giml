"""A trained alternative to the rule-based failure classifier (spec section 16, layer 1).

Turns a build or startup log's own ``[ERROR]`` lines into a failure class and a confidence: the same job
``maven.failures.classify`` does with hand-written patterns. It is trained locally from giml's own labelled
logs; nothing is downloaded from a hosted model and no example ever leaves the machine (spec 16 constraints).
Needs the ``ml`` extra (``pip install '.[ml]'``); importing this module without it raises scikit-learn's own
``ImportError``.

It must beat the rule-based classifier before anything trusts it (spec 16); ``scripts/train_failure_classifier.py``
measures that by leave-one-out cross-validation and reports the result honestly, win or not. Until it wins
clearly on real (not near-duplicate) failures, it stays a reporting signal: nothing in the engine reads its
prediction, and it never chooses a fallback, skip or verdict on its own (hard rule 8).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

from giml.maven.failures import error_text

FEATURE_VERSION = 1  # bump when what predict() feeds the model changes; a saved model records the version it was trained under


class ModelError(ValueError):
    """A saved model cannot be trusted: missing or mismatched checksum, or from an incompatible feature version."""


@dataclass(frozen=True)
class Prediction:
    label: str
    confidence: float  # the model's own probability of its top class (spec 16 layer 4 calibrates this further)


def _pipeline() -> Pipeline:
    return Pipeline([("tfidf", TfidfVectorizer(ngram_range=(1, 2), min_df=1, sublinear_tf=True)),
                     ("clf", LogisticRegression(max_iter=2000, class_weight="balanced"))])  # fmt: skip


def _feature_text(log_text: str) -> str:
    """What the model reads: the log's own [ERROR] lines, or the raw text when it has none (still something to read)."""
    return error_text(log_text) or log_text


class FailureClassifier:
    """A fitted TF-IDF + logistic-regression classifier over a log's error lines."""

    def __init__(self, pipeline: Pipeline) -> None:
        self._pipeline = pipeline

    @classmethod
    def train(cls, logs: Sequence[str], labels: Sequence[str]) -> FailureClassifier:
        """``logs`` are raw log text, matched in length and order with their true ``labels``."""
        if len(logs) != len(labels):
            raise ValueError(f"{len(logs)} logs but {len(labels)} labels: train needs one label per log")
        if len(set(labels)) < 2:
            raise ValueError("train needs examples of at least two different failure classes")
        pipeline = _pipeline()
        pipeline.fit([_feature_text(log) for log in logs], list(labels))
        return cls(pipeline)

    def predict(self, log_text: str) -> Prediction:
        probabilities = self._pipeline.predict_proba([_feature_text(log_text)])[0]
        classes = self._pipeline.named_steps["clf"].classes_
        top = probabilities.argmax()
        return Prediction(str(classes[top]), float(probabilities[top]))

    def save(self, path: Path) -> None:
        """Writes the model and, beside it, a checksum file that ``load`` refuses to skip."""
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"version": FEATURE_VERSION, "pipeline": self._pipeline}, path)
        _checksum_path(path).write_text(hashlib.sha256(path.read_bytes()).hexdigest() + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> FailureClassifier:
        checksum_path = _checksum_path(path)
        if not checksum_path.is_file():
            raise ModelError(f"{path}: no checksum file ({checksum_path.name}); refusing to load a model that cannot be verified")
        expected = checksum_path.read_text(encoding="utf-8").strip()
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ModelError(f"{path}: checksum does not match {checksum_path.name}; the file may be corrupt or tampered with")
        payload: dict[str, Any] = joblib.load(path)
        if payload.get("version") != FEATURE_VERSION:
            raise ModelError(f"{path}: trained with feature version {payload.get('version')}, this code expects {FEATURE_VERSION}; retrain")
        return cls(payload["pipeline"])


def _checksum_path(path: Path) -> Path:
    return path.with_name(path.name + ".sha256")
