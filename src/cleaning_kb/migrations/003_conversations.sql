ALTER TABLE conversations ADD COLUMN auto_title INTEGER NOT NULL DEFAULT 1 CHECK(auto_title IN (0,1));
CREATE INDEX qa_runs_conversation_order ON qa_runs(conversation_id, position);
CREATE INDEX qa_runs_review_queue ON qa_runs(needs_review, reviewed_at, created_at);
CREATE INDEX audit_runs_conversation ON audit_runs(conversation_id);
