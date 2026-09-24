ALTER TABLE run ADD COLUMN kind TEXT NOT NULL DEFAULT 'plan';  -- 'plan' or 'assess'

CREATE TABLE gate_result (
    run_id         TEXT PRIMARY KEY REFERENCES run (id),
    project_id     TEXT NOT NULL REFERENCES project (id),
    base_sha       TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    earned_tier    TEXT,              -- NULL when no tier was earned
    json           TEXT NOT NULL,     -- the full assessment result (spec 6.4)
    measured_at    TEXT NOT NULL,
    expires_at     TEXT NOT NULL
);

CREATE INDEX gate_result_project ON gate_result (project_id, measured_at);
