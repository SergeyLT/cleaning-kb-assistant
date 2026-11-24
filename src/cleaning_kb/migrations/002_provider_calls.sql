CREATE TABLE provider_calls (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('embedding', 'generation')),
    model TEXT NOT NULL,
    reserved_tokens INTEGER NOT NULL CHECK(reserved_tokens > 0),
    state TEXT NOT NULL CHECK(state IN ('started', 'completed', 'failed', 'interrupted')),
    usage_json TEXT,
    error TEXT
);
CREATE INDEX provider_calls_created_at ON provider_calls(created_at);
