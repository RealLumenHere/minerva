-- fleet_memory schema (wq#3647) — canonical fleet memory DB
-- Target: lumen-pgvector pg16 @127.0.0.1:5433, database fleet_memory
-- Built by audit-mem-335f7c 2026-10-08. Idempotent (safe to re-run).

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 1. fleet_facts — atomic subject/attribute/value facts
CREATE TABLE IF NOT EXISTS fleet_facts (
  id            SERIAL PRIMARY KEY,
  subject       TEXT NOT NULL,
  attribute     TEXT NOT NULL,
  value         TEXT,
  value_type    TEXT,
  source        TEXT,
  confidence    REAL DEFAULT 1.0,
  status        TEXT DEFAULT 'verified',
  first_seen    TIMESTAMPTZ DEFAULT now(),
  last_verified TIMESTAMPTZ DEFAULT now(),
  expires_at    TIMESTAMPTZ,
  created_by    TEXT,
  updated_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_facts_subject      ON fleet_facts (lower(subject));
CREATE INDEX IF NOT EXISTS idx_facts_subject_attr ON fleet_facts (lower(subject), lower(attribute));
CREATE INDEX IF NOT EXISTS idx_facts_status       ON fleet_facts (status);
CREATE INDEX IF NOT EXISTS idx_facts_expires      ON fleet_facts (expires_at);
CREATE INDEX IF NOT EXISTS idx_facts_trgm ON fleet_facts
  USING gin ((coalesce(subject,'')||' '||coalesce(attribute,'')||' '||coalesce(value,'')) gin_trgm_ops);

-- 2. fleet_devices — node roster
CREATE TABLE IF NOT EXISTS fleet_devices (
  id            SERIAL PRIMARY KEY,
  hostname      TEXT UNIQUE,
  role          TEXT,
  architecture  TEXT,
  ip_tailscale  TEXT,
  ip_lan        TEXT,
  os            TEXT,
  status        TEXT,
  location      TEXT,
  power_profile TEXT,
  notes         TEXT
);
CREATE INDEX IF NOT EXISTS idx_devices_status ON fleet_devices (status);

-- 3. fleet_decisions — standing rules / never-always directives
CREATE TABLE IF NOT EXISTS fleet_decisions (
  id         SERIAL PRIMARY KEY,
  decision   TEXT NOT NULL,
  rationale  TEXT NOT NULL,
  scope      TEXT DEFAULT 'fleet',
  status     TEXT DEFAULT 'active',
  created_at TIMESTAMPTZ DEFAULT now(),
  supersedes INTEGER REFERENCES fleet_decisions(id)
);
CREATE INDEX IF NOT EXISTS idx_decisions_status ON fleet_decisions (status);
CREATE INDEX IF NOT EXISTS idx_decisions_trgm ON fleet_decisions
  USING gin ((coalesce(decision,'')||' '||coalesce(rationale,'')) gin_trgm_ops);

-- 4. agent_episodes — per-ticket work episodes
CREATE TABLE IF NOT EXISTS agent_episodes (
  id             SERIAL PRIMARY KEY,
  ticket_id      TEXT UNIQUE,
  agent          TEXT,
  started_at     TIMESTAMPTZ,
  finished_at    TIMESTAMPTZ,
  result         TEXT,
  summary        TEXT,
  files_changed  TEXT,
  commit_sha     TEXT,
  tests          TEXT,
  failure_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_episodes_agent ON agent_episodes (agent);
CREATE INDEX IF NOT EXISTS idx_episodes_trgm ON agent_episodes
  USING gin ((coalesce(summary,'')||' '||coalesce(ticket_id,'')) gin_trgm_ops);

-- 5. memory_sources — provenance of ingested material
CREATE TABLE IF NOT EXISTS memory_sources (
  id           SERIAL PRIMARY KEY,
  source_type  TEXT,
  source_uri   TEXT,
  content_hash TEXT,
  captured_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sources_hash ON memory_sources (content_hash);

-- 6. memory_events — append-only audit log of every mutation
CREATE TABLE IF NOT EXISTS memory_events (
  id          SERIAL PRIMARY KEY,
  "timestamp" TIMESTAMPTZ DEFAULT now(),
  actor       TEXT,
  operation   TEXT,
  object_type TEXT,
  object_id   INTEGER,
  before      JSONB,
  after       JSONB,
  reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_object ON memory_events (object_type, object_id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON memory_events ("timestamp");

-- updated_at maintenance on fleet_facts
CREATE OR REPLACE FUNCTION fm_touch_updated_at() RETURNS trigger AS $FM$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$FM$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_facts_touch ON fleet_facts;
CREATE TRIGGER trg_facts_touch BEFORE UPDATE ON fleet_facts
  FOR EACH ROW EXECUTE FUNCTION fm_touch_updated_at();

-- convenience view: active (non-expired, verified) facts
CREATE OR REPLACE VIEW fleet_facts_active AS
  SELECT * FROM fleet_facts
  WHERE status = 'verified'
    AND (expires_at IS NULL OR expires_at > now());
