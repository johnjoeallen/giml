CREATE TABLE snapshot (
    id           TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,  -- ISO 8601, UTC
    content_hash TEXT NOT NULL,  -- sha256 hex
    path         TEXT NOT NULL
);

CREATE INDEX snapshot_source_fetched ON snapshot (source, fetched_at);
