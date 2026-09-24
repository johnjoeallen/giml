CREATE TABLE project (
    id              TEXT PRIMARY KEY,  -- project key: <repo name>-<hash of repo path and subdir>
    path            TEXT NOT NULL,     -- project directory
    remote_url_hash TEXT,              -- sha256 of the origin URL; the URL itself may hold credentials
    declared_tier   TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE run (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES project (id),
    base_sha        TEXT NOT NULL,
    branch          TEXT NOT NULL,
    worktree_path   TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,              -- NULL while running, or if the run crashed
    stop_reason     TEXT,
    rewind_from_sha TEXT               -- NULL unless the run used rewind mode (spec 5.3)
);

CREATE INDEX run_project ON run (project_id, started_at);
