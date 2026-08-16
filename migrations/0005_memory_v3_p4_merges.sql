-- 0005: Memory V3 P4 — note merges (consolidation with a consumer).
-- House semantics: invalidate-not-delete. Absorbed notes stay in archival_notes;
-- retrieval paths filter them via this table. Reversible by DELETE from note_merges.
CREATE TABLE IF NOT EXISTS note_merges (
    absorbed_id TEXT PRIMARY KEY,
    keep_id     TEXT NOT NULL,
    sim         REAL,
    method      TEXT,           -- exact-auto | manual
    ts          TEXT
);
CREATE INDEX IF NOT EXISTS idx_note_merges_keep ON note_merges(keep_id);
