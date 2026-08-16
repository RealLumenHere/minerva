-- Memory V3 P2: lightweight provenance/index graph over memory objects.
-- Deterministic extraction only (no LLM at ingest). Node types are strings:
--   note (archival_notes.id), subject (state_facts.subject), ticket (wq#/aq#),
--   file (absolute path), tag (archival tag).
CREATE TABLE IF NOT EXISTS memory_edges (
    src_type TEXT NOT NULL,
    src_id   TEXT NOT NULL,
    rel      TEXT NOT NULL,
    dst_type TEXT NOT NULL,
    dst_id   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (src_type, src_id, rel, dst_type, dst_id)
);
CREATE INDEX IF NOT EXISTS idx_me_src ON memory_edges(src_type, src_id);
CREATE INDEX IF NOT EXISTS idx_me_dst ON memory_edges(dst_type, dst_id);
