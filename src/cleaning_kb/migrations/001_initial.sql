CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    title TEXT NOT NULL CHECK(length(trim(title)) > 0),
    text TEXT NOT NULL CHECK(length(trim(text)) > 0),
    content_hash TEXT NOT NULL,
    indexing_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(indexing_status IN ('pending', 'indexing', 'ready', 'failed')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
    error TEXT
);
CREATE TABLE snippets (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK(position >= 0),
    snippet_text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    embedding_json TEXT,
    embedding_model TEXT,
    embedding_dimension INTEGER,
    UNIQUE(document_id, position)
);
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    title TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0)
);
CREATE TABLE qa_runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
    position INTEGER,
    kind TEXT NOT NULL DEFAULT 'answer' CHECK(kind IN ('answer', 'summary')),
    state TEXT NOT NULL DEFAULT 'running'
        CHECK(state IN ('running', 'completed', 'failed', 'interrupted')),
    question TEXT NOT NULL,
    search_question TEXT,
    answer TEXT,
    result_json TEXT,
    sources_json TEXT NOT NULL DEFAULT '[]',
    needs_review INTEGER NOT NULL DEFAULT 0 CHECK(needs_review IN (0, 1)),
    error TEXT,
    reviewed_at TEXT,
    source_revision INTEGER,
    model TEXT,
    usage_json TEXT,
    UNIQUE(conversation_id, position)
);
CREATE UNIQUE INDEX one_running_operation_per_conversation
    ON qa_runs(conversation_id) WHERE state = 'running' AND conversation_id IS NOT NULL;
CREATE TABLE audit_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    request_id TEXT,
    conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    input TEXT,
    output TEXT,
    status TEXT NOT NULL CHECK(status IN ('ok', 'error')),
    error TEXT,
    duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0)
);
CREATE INDEX audit_runs_request_id ON audit_runs(request_id);
CREATE TABLE auth_sessions (
    token_hash TEXT PRIMARY KEY,
    csrf_token TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX auth_sessions_expires_at ON auth_sessions(expires_at);
