#!/usr/bin/env python3
"""
minerva_audit.py - wq#1634: the audit chokepoint for direct writes to minerva.db.

MINERVA_PIPELINE_MAP.md §4.5 ("one audited write path"). memoryd's kernel already
writes a `memory_audit` row for every `archival_notes` write that arrives through
the daemon (§1.6). The two write paths that DON'T go through the daemon --
`lumen_state.ingest()` (§1.1) and `lumen_episode.commit` from a CLI session
(§1.4) -- had no trail beyond a free-text `source_ref`. This module gives them
the same trail, against the same append-only table, with no new schema.

Contract for callers:
  * `audit()` NEVER commits. It writes on the connection you hand it, so the
    audit row lands in the SAME transaction as the row it describes. An accepted
    write with no audit row is then impossible by construction, which is the
    property the kernel already has (v4/memoryd/kernel.py:277).
  * `source_kind` is stamped HERE, at write time, from $WQ_AGENT plus an explicit
    --stated-by-frank flag (wq#1607 §3.4). It is never inferred after the fact:
    wq#1613's verifier refuses to supersede a `frank_stated` fact, so a value
    guessed later would silently disarm that guard.
  * `lane` is READ from the ticket named by `source_ref`, never taken from the
    writer -- the same rule as memoryd §2.4, for the same reason: a lane must not
    be able to spell its own name.
"""
import os
import sqlite3
from datetime import datetime, timezone

QUEUE_DB = os.environ.get('WQ_QUEUE_DB', '/home/lumen/agents/work-queue/queue.db')

# ── source_kind (wq#1607 §3.4) ───────────────────────────────────────────────
# Closed set. `frank_stated` and `observed` are wq#1613's anchors (lumen_verify.py
# treats frank_stated as never-supersedable and stamps observed on probe writes);
# the other two split "a session wrote this" from "a bare shell wrote this", which
# is exactly the distinction $WQ_AGENT can answer and prose cannot.
FRANK_STATED = 'frank_stated'
OBSERVED = 'observed'
AGENT_WRITTEN = 'agent_written'
UNATTRIBUTED = 'unattributed'
SOURCE_KINDS = (FRANK_STATED, OBSERVED, AGENT_WRITTEN, UNATTRIBUTED)

# ── actions (migration 0008 §3.1 vocabulary) ─────────────────────────────────
WRITE, SUPERSEDE, RECONFIRM = 'write', 'supersede', 'reconfirm'

# Mirrors migrations/0008_wq575_memoryd_governance.sql verbatim so a fresh or test
# DB gets the identical table -- including the append-only triggers, without which
# "append-only" is a comment rather than a guarantee.
AUDIT_DDL = '''
CREATE TABLE IF NOT EXISTS memory_audit (
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           TEXT NOT NULL,
  action       TEXT NOT NULL,
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
'''


def _now():
    return datetime.now(timezone.utc).isoformat()


def ensure_table(conn):
    """Create memory_audit if this DB predates wq#575. Checked via sqlite_master
    first so the common (live) case takes no write lock."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='memory_audit'").fetchone():
        return
    conn.executescript(AUDIT_DDL)


def agent_id():
    return (os.environ.get('WQ_AGENT') or '').strip() or None


def ticket_lane(source_ref):
    """The lane of the ticket named by `source_ref` ('wq#1634' -> 'build').

    Read-only, fail-open to None: an unattributed audit row is worth far more
    than an ingest that dies because the queue DB was busy."""
    ref = (source_ref or '').strip()
    if not ref.startswith('wq#'):
        return None
    tid = ref[3:].split()[0].strip()
    if not tid:
        return None
    try:
        q = sqlite3.connect('file:%s?mode=ro' % QUEUE_DB, uri=True, timeout=3.0)
        try:
            r = q.execute('SELECT lane FROM tasks WHERE id=?', (tid,)).fetchone()
            return r[0] if r else None
        finally:
            q.close()
    except Exception:
        return None


def resolve_source_kind(stated_by_frank=False, explicit=None):
    """Stamp source_kind at the chokepoint (wq#1607 §3.4).

    Precedence: the human flag wins, then an explicit caller value (lumen_verify
    passes `observed`), then presence of $WQ_AGENT. Only Frank can assert
    frank_stated, which is why it is a flag and not a heuristic."""
    if stated_by_frank:
        return FRANK_STATED
    if explicit:
        explicit = str(explicit).strip()
        if explicit not in SOURCE_KINDS:
            raise ValueError('source_kind %r not in %s' % (explicit, list(SOURCE_KINDS)))
        return explicit
    return AGENT_WRITTEN if agent_id() else UNATTRIBUTED


def audit(conn, action, table_name, row_id, source_ref=None, detail=None,
          invariant=None, source_kind=None, lane=None):
    """Append one memory_audit row on `conn`. Caller commits (see module docstring).

    `source_kind` rides in `detail` rather than in a column of its own: memory_audit
    is append-only and trigger-guarded, so widening it is a migration with a live
    daemon writing to it, and the audit row's job here is provenance, not query
    surface. The state_facts row carries source_kind in its own column."""
    ensure_table(conn)
    bits = []
    if source_kind:
        bits.append('source_kind=%s' % source_kind)
    if detail:
        bits.append(str(detail))
    conn.execute(
        'INSERT INTO memory_audit(ts,action,table_name,row_id,lane,agent_id,trust,'
        'source_ref,before_sha,after_sha,detail,invariant) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        (_now(), action, table_name, row_id,
         lane if lane is not None else ticket_lane(source_ref), agent_id(), None,
         source_ref, None, None, (' '.join(bits) or None), invariant))
    return conn.execute('SELECT last_insert_rowid()').fetchone()[0]
