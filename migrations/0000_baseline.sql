-- 0000_baseline.sql
-- Baseline marker for the pre-migration schema (all tables created by the
-- per-module CREATE TABLE IF NOT EXISTS bootstrap: init_db.py, lumen_state.py,
-- minerva_memory.py, lumen_scout_expanded.py). This file intentionally does
-- nothing structural — it exists so the migration ledger has a 0000 anchor and
-- so a FRESH db (which runs the bootstrap first) records the baseline too.
SELECT 1;
