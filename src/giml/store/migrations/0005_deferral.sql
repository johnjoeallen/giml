-- Deferrals (spec sections 8.4 and 11): a dependency the planner could not move, with why, and what
-- would make it worth trying again. Open until a later run resolves it.

CREATE TABLE deferral (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES project (id),
    coordinate      TEXT NOT NULL,
    held_at_version TEXT NOT NULL,
    reason          TEXT NOT NULL,
    trigger_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    resolved_at     TEXT
);

CREATE INDEX deferral_project ON deferral (project_id);
