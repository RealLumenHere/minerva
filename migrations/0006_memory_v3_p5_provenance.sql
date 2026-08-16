-- 0006: Memory V3 P5 — write-path provenance/trust on episodic notes.
-- origin: claude-session | convo-learn | timer | backfill | state-mirror
-- trust:  operator (Frank/Claude session) | derived (machine-generated from owned data)
--         | external (distilled from third-party/messenger input — quarantine tier)
ALTER TABLE archival_notes ADD COLUMN origin TEXT;
ALTER TABLE archival_notes ADD COLUMN trust  TEXT;
-- Backfill by source_ref convention (best-effort; unknowns default operator-session).
UPDATE archival_notes SET origin='backfill',     trust='derived'  WHERE source_ref LIKE 'backfill:%';
UPDATE archival_notes SET origin='state-mirror', trust='derived'  WHERE source_ref LIKE 'state:%';
UPDATE archival_notes SET origin='convo-learn',  trust='external' WHERE source_ref LIKE 'convo%' OR title LIKE '[convo-learn]%';
UPDATE archival_notes SET origin='claude-session', trust='operator' WHERE origin IS NULL;
CREATE INDEX IF NOT EXISTS idx_archival_origin ON archival_notes(origin);
