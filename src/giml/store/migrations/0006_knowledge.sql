-- Knowledge (spec sections 10 and 11): failures of a dependency transition, kept across runs and projects.
-- It informs ordering and reports, never a verdict: a transition that failed once is still built when the
-- search reaches it.

CREATE TABLE knowledge_transition (
    id              TEXT PRIMARY KEY,   -- coordinate|from|to|failure_class|error_signature
    coordinate      TEXT NOT NULL,
    from_version    TEXT NOT NULL,
    to_version      TEXT NOT NULL,
    failure_class   TEXT NOT NULL,
    error_signature TEXT NOT NULL,
    count           INTEGER NOT NULL,
    last_seen       TEXT NOT NULL
);

CREATE INDEX knowledge_transition_coordinate ON knowledge_transition (coordinate);
