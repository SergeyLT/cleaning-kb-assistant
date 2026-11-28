CREATE TABLE provider_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    calls_per_minute INTEGER,
    calls_per_day INTEGER,
    token_budget_per_day INTEGER,
    reset_at TEXT,
    reset_actor_type TEXT,
    reset_actor_id TEXT
);
INSERT INTO provider_settings (id) VALUES (1);
ALTER TABLE audit_runs ADD COLUMN actor_type TEXT;
ALTER TABLE audit_runs ADD COLUMN actor_id TEXT;
ALTER TABLE audit_runs ADD COLUMN details_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE auth_sessions ADD COLUMN actor_id TEXT;
