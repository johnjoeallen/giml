-- Outcome logging (spec sections 11 and 15). A state is the tree a run builds (the baseline, later each
-- candidate); an attempt is one stage run on it; an example is a labelled, de-duplicated training row.

CREATE TABLE candidate_state (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES run (id),
    parent_id    TEXT REFERENCES candidate_state (id),
    changes_json TEXT NOT NULL,     -- the dependency changes relative to the parent; [] for the baseline
    status       TEXT NOT NULL
);

CREATE INDEX candidate_state_run ON candidate_state (run_id);

CREATE TABLE build_attempt (
    id              TEXT PRIMARY KEY,
    state_id        TEXT NOT NULL REFERENCES candidate_state (id),
    stage           TEXT NOT NULL,
    outcome         TEXT NOT NULL,  -- pass or fail
    failure_class   TEXT,
    error_signature TEXT,
    cache_key       TEXT,
    cache_hit       INTEGER NOT NULL,
    duration_ms     INTEGER NOT NULL,
    log_path        TEXT
);

CREATE INDEX build_attempt_state ON build_attempt (state_id);

CREATE TABLE example (
    id            TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES run (id),
    features_json TEXT NOT NULL,
    label_json    TEXT NOT NULL,
    split_group   TEXT NOT NULL,    -- the project: no project appears on both sides of a train/test split
    dedup_hash    TEXT NOT NULL UNIQUE  -- exact duplicates are dropped
);

CREATE INDEX example_group ON example (split_group);
