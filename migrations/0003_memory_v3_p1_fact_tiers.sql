-- Memory V3 P1: semantic-tier diet.
-- fact_class: identity|config|status|narrative (coarse classification)
-- render_tier: core|topic  (core -> LIVE_FLEET_STATE injected block; topic -> on-demand)
-- Also drops the dead state_episodes table (0 rows since creation; episodes live in
-- recall_fts/archival_notes via minerva_memory.py).
ALTER TABLE state_facts ADD COLUMN fact_class TEXT;
ALTER TABLE state_facts ADD COLUMN render_tier TEXT;
DROP TABLE IF EXISTS state_episodes;
