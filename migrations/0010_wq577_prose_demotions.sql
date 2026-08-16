-- 0010_wq577_prose_demotions.sql
-- wq#577 [mv4-P5] prose lifecycle: claude_memory.md SECTION-level demotion pipeline.
-- Design: reuses wq#576's archive-not-erase ledger shape (note_archives) for prose
-- sections instead of archival_notes rows. ADDITIVE ONLY -- no column added to any
-- existing table, no trigger touched (wq#575 migration 0008 owns archival_notes'
-- provenance triggers).
--
-- Physical asymmetry vs note_archives, stated up front: a note_archives row is a
-- pure DB flag flip (the note text never moves). A prose demotion is a real file
-- move (section text leaves claude_memory.md, is appended to
-- context/claude_memory_archive.md). This table is the audit ledger for that move,
-- not the reversibility mechanism itself -- reversibility is the paired restore
-- (lumen_prose_lifecycle.py restore), which is why plan_ref and both file shas are
-- recorded: enough to prove which apply moved which bytes.

CREATE TABLE IF NOT EXISTS prose_demotions (
  section_id     TEXT NOT NULL,             -- slug id, matches federated_chunks.section_id
  title          TEXT,
  demoted_at     TEXT NOT NULL,
  reason         TEXT,                      -- signal tags, e.g. "age=142d,zero_hits,stale_store"
  age_days       INTEGER,
  hits           INTEGER,
  note_cov       REAL,                      -- max cosine overlap vs an archival note, 0 if none
  live_sha_pre   TEXT,                      -- sha256 of claude_memory.md before this apply
  archive_sha_post TEXT,                    -- sha256 of claude_memory_archive.md after this apply
  plan_ref       TEXT,
  restored_at    TEXT,
  PRIMARY KEY (section_id, demoted_at)
);
CREATE INDEX IF NOT EXISTS idx_prose_demotions_active
  ON prose_demotions(section_id) WHERE restored_at IS NULL;
