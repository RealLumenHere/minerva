-- 0008 wq#575 [mv4-P3] memoryd governance surface.
--
-- Additive only. No table rebuild, no rewrite, no drop. Every current reader
-- (search_v3 / minerva_memory / lumen_dream / memory_embed_guard) selects named
-- columns, never *, so appended columns are invisible to them.
--
-- Source of truth for this DDL: minerva/v4/research/memory_service_design.md
--   §2.1 provenance columns · §2.2 immutability trigger · §3.1 memory_audit
--   §3.2 memory_quarantine.
--
-- PRE-FLIGHT PERFORMED (2026-08-15, session mv4-w575-05322f):
--   * sqlite3 .backup -> /home/lumen/backups/wq575/minerva.db.pre575_20260815_025626
--     (integrity_check ok, 1197 archival_notes, 906 state_facts)
--   * quiet window: applied between minerva-eval (03:00Z) and minerva-factory
--     (03:35Z); classifier 06:30Z, proposal 06:45Z, dream/reflector Sun.
--   * live UPDATE callers on archival_notes verified to touch `embedding` ONLY
--     (memory_embed_guard.py:96, lumen_dream.py:236), so the provenance trigger
--     below cannot break the embedding repair path. Zero DELETE FROM callers.
--
-- DELIBERATE DEVIATION FROM THE DESIGN — schema_version has NO DEFAULT.
--   §1.1 sketches `schema_version INTEGER DEFAULT 1`, but ADD COLUMN with a
--   default backfills all 1,197 legacy rows with 1, i.e. asserts they were
--   written under a kernel that did not exist. §2.1 states the opposite intent:
--   pre-cutover rows are `schema_version=0 / lane='legacy'` and "we do not
--   backfill a trust level we cannot evidence". Nullable wins: legacy rows read
--   NULL (treated as 0), the kernel stamps 1 explicitly on every write it makes.
--
-- KNOWN RESIDUAL, NOT CLOSED HERE — minerva_memory.py:180 still issues
--   INSERT OR REPLACE, which SQLite implements as DELETE+INSERT and which
--   therefore does NOT fire the BEFORE UPDATE trigger below. Closing it needs a
--   BEFORE DELETE trigger, which would break lumen_episode.py commit for every
--   lane. Kernel writes are safe (I-3 bans the statement); the legacy library
--   path is not. Tracked as a follow-up ticket, deliberately not wedged in here.

BEGIN TRANSACTION;

-- §2.1 -----------------------------------------------------------------------
ALTER TABLE archival_notes ADD COLUMN agent_id       TEXT;
ALTER TABLE archival_notes ADD COLUMN lane           TEXT;
ALTER TABLE archival_notes ADD COLUMN content_sha256 TEXT;
ALTER TABLE archival_notes ADD COLUMN schema_version INTEGER;
ALTER TABLE archival_notes ADD COLUMN supersedes     TEXT;
ALTER TABLE archival_notes ADD COLUMN superseded_by  TEXT;
ALTER TABLE archival_notes ADD COLUMN revoked_at     TEXT;
ALTER TABLE archival_notes ADD COLUMN revoked_reason TEXT;

CREATE INDEX IF NOT EXISTS idx_archival_live ON archival_notes(revoked_at, superseded_by);
CREATE INDEX IF NOT EXISTS idx_archival_sha  ON archival_notes(content_sha256);
CREATE INDEX IF NOT EXISTS idx_archival_lane ON archival_notes(lane, created_at);

-- §2.2 provenance is write-once. Mutable after write, by design:
-- superseded_by / revoked_at / revoked_reason / embedding (repair path).
CREATE TRIGGER IF NOT EXISTS trg_archival_provenance_immutable
BEFORE UPDATE OF origin, trust, agent_id, lane, created_at, content_sha256, supersedes
ON archival_notes
BEGIN
  SELECT RAISE(ABORT, 'provenance is immutable; supersede instead (wq#575 §2.2)');
END;

-- §3.1 append-only write/read audit. GAP-4: nothing records who wrote what.
-- Read granularity = ONE ROW PER QUERY, hit ids in `detail` (§3.1 recommendation,
-- Frank Q4). Per-hit counting for #576 lands in that ticket's own note_hits /
-- note_salience tables (#571 design), so this schema does not have to change twice.
CREATE TABLE IF NOT EXISTS memory_audit (
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           TEXT NOT NULL,
  action       TEXT NOT NULL,   -- write|supersede|revoke|read|reject|quarantine|propose
  table_name   TEXT NOT NULL,
  row_id       TEXT,
  lane         TEXT,
  agent_id     TEXT,
  trust        TEXT,
  source_ref   TEXT,
  before_sha   TEXT,
  after_sha    TEXT,
  detail       TEXT,
  invariant    TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_row  ON memory_audit(row_id, ts);
CREATE INDEX IF NOT EXISTS idx_audit_lane ON memory_audit(lane, ts);
CREATE INDEX IF NOT EXISTS idx_audit_act  ON memory_audit(action, ts);

CREATE TRIGGER IF NOT EXISTS trg_audit_no_update BEFORE UPDATE ON memory_audit
BEGIN SELECT RAISE(ABORT, 'memory_audit is append-only (wq#575 §3.1)'); END;
CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete BEFORE DELETE ON memory_audit
BEGIN SELECT RAISE(ABORT, 'memory_audit is append-only (wq#575 §3.1)'); END;

-- §3.2 quarantine. Payload is held OUT of the live tables and is never
-- retrievable by any lane, never embedded. Release re-runs the full kernel.
CREATE TABLE IF NOT EXISTS memory_quarantine (
  id           TEXT PRIMARY KEY,
  ts           TEXT NOT NULL,
  table_name   TEXT NOT NULL,
  payload      TEXT NOT NULL,
  lane         TEXT,
  agent_id     TEXT,
  trust        TEXT,
  source_ref   TEXT,
  reason       TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'held',   -- held|released|discarded
  reviewed_by  TEXT,
  reviewed_at  TEXT,
  review_note  TEXT
);
CREATE INDEX IF NOT EXISTS idx_quar_status ON memory_quarantine(status, ts);

COMMIT;
