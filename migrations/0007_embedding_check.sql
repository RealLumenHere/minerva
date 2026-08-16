-- 0007 (STAGED — NOT APPLIED): wq#465 defence-in-depth CHECK constraint on
-- archival_notes.embedding, so a malformed vector cannot be persisted at all.
--
-- STATUS: staged only. wq#465 said "STAGE only for any schema change".
-- Frank gate required before running this. The wq#465 fix does NOT depend on it:
--   L1 read path  — minerva_memory.decode_embedding() rejects malformed blobs
--   L2 write path — write_note() stores NULL, never b''
--   L3 guard      — lumen-embed-guard.timer repairs hourly + alarms
-- This migration is the fourth layer: make the invalid state unrepresentable.
--
-- WHY A TABLE REBUILD: SQLite cannot ADD a CHECK constraint to an existing table.
-- The only route is the documented 12-step ALTER procedure (new table, copy,
-- drop, rename). archival_notes is ~1006 rows / ~20MB — a sub-second rebuild.
--
-- PRE-FLIGHT (mandatory):
--   1. sqlite3 /home/lumen/minerva/minerva.db ".backup '/home/lumen/backups/wq465/pre_0007.db'"
--   2. Confirm zero malformed rows first, or the INSERT..SELECT will abort:
--      python3 /home/lumen/minerva/memory_embed_guard.py --check   # must exit 0
--   3. Stop writers (lumen-agent, minerva timers) — a concurrent write_note()
--      during the rebuild would be lost.
--
-- THE CONSTRAINT: embedding may be NULL (unknown, repair queue picks it up) but
-- if present must be a BLOB whose length is a positive multiple of 4 (float32).
-- Deliberately NOT pinning length to 3072: that would hard-block a future
-- embedding-model swap. decode_embedding(expect_dim=) handles dim agreement at
-- read time, which is where the query vector's dimensionality is actually known.

PRAGMA foreign_keys=OFF;
BEGIN TRANSACTION;

CREATE TABLE archival_notes_new (
    id          TEXT PRIMARY KEY,
    kind        TEXT,
    title       TEXT,
    content     TEXT,
    embedding   BLOB,
    tags        TEXT,
    source_ref  TEXT,
    created_at  TEXT,
    origin      TEXT,
    trust       TEXT,
    CHECK (embedding IS NULL
           OR (typeof(embedding) = 'blob'
               AND length(embedding) > 0
               AND length(embedding) % 4 = 0))
);

INSERT INTO archival_notes_new
    (id, kind, title, content, embedding, tags, source_ref, created_at, origin, trust)
SELECT id, kind, title, content, embedding, tags, source_ref, created_at, origin, trust
FROM archival_notes;

DROP TABLE archival_notes;
ALTER TABLE archival_notes_new RENAME TO archival_notes;

-- DROP TABLE takes the indexes with it. Recreate them or every recall query and
-- the origin/trust provenance scan degrade to full table scans. (Caught during
-- dry-run validation on a DB copy, 2026-08-10 — the first draft omitted these.)
CREATE INDEX idx_archival_kind   ON archival_notes(kind, created_at);
CREATE INDEX idx_archival_origin ON archival_notes(origin);

COMMIT;
PRAGMA foreign_keys=ON;
PRAGMA integrity_check;

-- POST-CHECK:
--   sqlite3 minerva.db "SELECT COUNT(*) FROM archival_notes;"          -- expect 1006+
--   sqlite3 minerva.db "INSERT INTO archival_notes(id,embedding) VALUES('x',x'');"
--     -- MUST fail with: CHECK constraint failed
--   sqlite3 minerva.db "DELETE FROM archival_notes WHERE id='x';"      -- cleanup if it did insert
--   python3 /home/lumen/minerva/memory_embed_guard.py                  -- must exit 0
--   python3 /home/lumen/minerva/eval/recall_bench.py --label post_0007 -- R@1 must hold at 0.911
-- ROLLBACK: restore /home/lumen/backups/wq465/pre_0007.db over minerva.db.
