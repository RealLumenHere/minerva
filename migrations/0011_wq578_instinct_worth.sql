-- 0011_wq578_instinct_worth.sql
-- wq#578 [mv4-P6] instinct effectiveness loop (GOVERNED).
-- Design: research/salience_forgetting.md section 6 ("SAME MACHINERY -> INSTINCT
-- EFFECTIVENESS") -- reuses wq#576's Memory Worth two-counter machinery
-- (lumen_salience.worth_stats) and its note_hit_outcomes labels; does NOT
-- reimplement either. ADDITIVE ONLY. No column added to any existing table.
--
-- Rollup, recomputed by dream; never hand-edited (same convention as
-- note_salience). instinct_id is sha1(rule_text)[:8] -- instincts.yaml has no
-- stable id of its own, and rule text IS the identity (editing the wording is
-- editing the instinct; the id should change with it rather than silently
-- keep scoring the old wording under a stale key).
CREATE TABLE IF NOT EXISTS instinct_worth (
  instinct_id   TEXT PRIMARY KEY,
  rule_text     TEXT NOT NULL,
  n_adhere      INTEGER DEFAULT 0,
  n_violate     INTEGER DEFAULT 0,
  succ_adhere   INTEGER DEFAULT 0,
  fail_adhere   INTEGER DEFAULT 0,
  succ_violate  INTEGER DEFAULT 0,
  fail_violate  INTEGER DEFAULT 0,
  worth_adhere  REAL,                 -- Beta-Bernoulli posterior mean, Pr[success|adhered]
  worth_violate REAL,                 -- Pr[success|violated]
  delta         REAL,                 -- worth_adhere - worth_violate; THE signal, not the level
  computed_at   TEXT
);

-- Evidence trail: which episode contributed to which instinct's adhere/violate
-- count, and how it was detected. Exists so a confidence-update proposal can be
-- checked by a human against its actual support (GovMem lesson: correlated
-- traces mislead counting if you cannot see what was counted).
CREATE TABLE IF NOT EXISTS instinct_worth_evidence (
  instinct_id TEXT NOT NULL,
  episode_id  TEXT NOT NULL,
  class       TEXT NOT NULL,          -- adhere | violate
  method      TEXT NOT NULL,          -- fq-prevented-by | episode-relevance
  score       REAL,                   -- match score (jaccard / containment) that earned this row
  outcome     TEXT,                   -- success | failure, from note_hit_outcomes at compute time
  computed_at TEXT NOT NULL,
  PRIMARY KEY (instinct_id, episode_id, class)
);
