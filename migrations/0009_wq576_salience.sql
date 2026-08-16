-- 0009_wq576_salience.sql
-- wq#576 [mv4-P4] usage-aware salience + forgetting proposals.
-- Design: minerva/v4/research/salience_forgetting.md sections 2/3/4/5.
-- ADDITIVE ONLY. No column is added to archival_notes and no trigger is added or
-- altered on it -- wq#575 migration 0008 owns that table s provenance triggers and
-- this migration must leave them exactly as it found them.

-- 1. Raw hit log. No FK to archival_notes on purpose: the log has to survive a
--    merge/absorb that removes the note id from the live set (research 2).
CREATE TABLE IF NOT EXISTS note_hits (
  note_id         TEXT    NOT NULL,
  ts              TEXT    NOT NULL,          -- UTC ISO
  rank            INTEGER NOT NULL,          -- 1..top_k position in the returned list
  caller          TEXT    NOT NULL,          -- precheck|recall-cli|proposal|memoryd|agent|bench|guard|...
  query_hash      TEXT,                      -- sha1(query)[:12]; never the query text
  session_id      TEXT,                      -- WQ_AGENT when present
  episode_id      TEXT,                      -- NULL at write; back-filled by the outcome joiner
  retriever_epoch INTEGER NOT NULL DEFAULT 2 -- counters earned under a different retriever measure a different function
);
CREATE INDEX IF NOT EXISTS idx_note_hits_note ON note_hits(note_id, ts);
CREATE INDEX IF NOT EXISTS idx_note_hits_ep   ON note_hits(episode_id);
CREATE INDEX IF NOT EXISTS idx_note_hits_sess ON note_hits(session_id, episode_id);
CREATE INDEX IF NOT EXISTS idx_note_hits_ts   ON note_hits(ts);

-- 2. Rollup. Recomputed nightly by lumen_dream; never hand-edited.
CREATE TABLE IF NOT EXISTS note_salience (
  note_id     TEXT PRIMARY KEY,
  hits        INTEGER DEFAULT 0,
  last_hit_at TEXT,
  succ        INTEGER DEFAULT 0,
  fail        INTEGER DEFAULT 0,
  worth       REAL,                          -- Beta-Bernoulli posterior mean, Laplace prior
  worth_lcb   REAL,                          -- Beta 5th percentile: forgetting ranks on THIS, never the mean
  hits_28d    INTEGER DEFAULT 0,
  worth_28d   REAL,
  epoch       INTEGER,
  computed_at TEXT
);

-- 3. Outcome labels, one row per episode. The label lives here, not on note_hits,
--    so a session s verdict can be revised once without rewriting N hit rows.
CREATE TABLE IF NOT EXISTS note_hit_outcomes (
  episode_id  TEXT PRIMARY KEY,
  session_id  TEXT,
  outcome     TEXT NOT NULL,                 -- success | failure | pending
  reason      TEXT,
  labeled_at  TEXT NOT NULL,
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_hit_outcomes_sess ON note_hit_outcomes(session_id);

-- 4. Archive-not-erase ledger for the (dark) forgetting proposer. Mirrors the
--    note_merges precedent: the note row is never deleted, only marked.
CREATE TABLE IF NOT EXISTS note_archives (
  note_id     TEXT PRIMARY KEY,
  archived_at TEXT NOT NULL,
  reason      TEXT,
  worth_lcb   REAL,
  hits        INTEGER,
  plan_ref    TEXT,
  restored_at TEXT
);

CREATE TABLE IF NOT EXISTS salience_meta (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT
);
INSERT OR IGNORE INTO salience_meta(key, value, updated_at)
  VALUES ('retriever_epoch', '2', datetime('now'));
INSERT OR IGNORE INTO salience_meta(key, value, updated_at)
  VALUES ('hit_retention_days', '90', datetime('now'));
