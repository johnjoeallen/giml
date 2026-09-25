"""Export the logged examples as JSONL for the ML layers (spec section 15)."""

from __future__ import annotations

import json
from collections.abc import Iterator

from giml.core.interfaces import StateStore
from giml.store.result_cache import canonical_json


def example_lines(store: StateStore) -> Iterator[str]:
    """One canonical JSON object per example, ordered by id, so the same examples give the same file."""
    for example in store.list_examples():
        yield canonical_json({"id": example.id, "run_id": example.run_id, "split_group": example.split_group,
                              "dedup_hash": example.dedup_hash, "features": json.loads(example.features_json),
                              "label": json.loads(example.label_json)})  # fmt: skip
