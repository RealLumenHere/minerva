#!/usr/bin/env python3
"""
lumen_state.py - Memory v2 Phase 1: bi-temporal fleet-state layer (the staleness fix).

Stores structured fleet facts as (subject, predicate, value) with a bi-temporal
model identical in spirit to Graphiti/Zep, but with DETERMINISTIC key-based
supersession instead of an LLM contradiction judge (reliable + zero-cost for
structured facts like IPs / hostnames / service state):

  valid_from / valid_to        : world-truth window  (valid_to IS NULL => true NOW)
  ingested_at / invalidated_at : system-knowledge window (invalidated_at NULL => active)

INGEST RULE: a new value for an existing (subject,predicate) whose value differs
closes the prior open row (sets valid_to = new fact's valid_from, invalidated_at = now)
and inserts the new row. Same value = idempotent no-op. Facts are INVALIDATED, never
deleted -> history + point-in-time stay queryable. This is what auto-expires
"PBox active" the moment "PBox retired" lands, with zero hand-maintained overrides.

CLI:
  lumen_state.py init
  lumen_state.py ingest <subject> <predicate> <value> [--valid-from ISO] [--source REF]
  lumen_state.py current [subject] [predicate]
  lumen_state.py asof <ISO_DATE> [subject] [predicate]
  lumen_state.py history <subject> <predicate>
"""
import sqlite3, sys, uuid, argparse
from datetime import datetime, timezone

DB = '/home/lumen/minerva/minerva.db'

def _now():
    return datetime.now(timezone.utc).isoformat()

def _conn():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c

# ── Change 3: confidence-decay support (2026-07-08) ──────────────────────────
# Predicate -> default half-life (days). Predicates NOT listed => NULL => never
# decay (stable facts: IPs, MACs, hostnames, status=retired stay authoritative).
VOLATILE_HALFLIFE = {
    'price': 30, 'deal_quality': 30, 'market_price': 30, 'cost': 30,
    'listing': 7, 'auction_state': 3, 'auction_end': 3, 'stock': 7,
}

# ── Memory V3 P1: fact classification (write-time) ──────────────────────────
import re as _re
IDENTITY_PREDS = {'hardware', 'role', 'status', 'lan_ip', 'tailscale_ip',
                  'tailscale_hostname', 'location', 'ssh_user', 'os', 'user',
                  'mac', 'cpu', 'ram', 'storage', 'gpu', 'model', 'port', 'url'}
_STATUSISH = _re.compile(r'(status|state|result|assessment|next_steps|blocker|'
                         r'calibration|recovery|attempt|incident)')

def _classify_fact(predicate, value):
    """Return (fact_class, render_tier). Mirrors the 0003 backfill heuristic:
    core = short identity facts (<=280) or any short fact (<=140); everything
    else is topic (on-demand via `current <subject>` / FULL render)."""
    vlen = len(value)
    if vlen > 500:
        fc = 'narrative'
    elif predicate in IDENTITY_PREDS:
        fc = 'identity'
    elif _STATUSISH.search(predicate):
        fc = 'status'
    else:
        fc = 'config'
    # thresholds tuned 2026-08-07 against live corpus: identity<=240 / other<=120
    # renders ~3.8K tok (soft cap 4K) with growth headroom. See eval sweep in P1 episode.
    tier = 'core' if ((predicate in IDENTITY_PREDS and vlen <= 240) or vlen <= 120) else 'topic'
    return fc, tier

def _migrate(conn):
    """Add nullable columns if missing (SQLite has no ADD COLUMN IF NOT EXISTS):
    half_life_days (decay rate) and last_confirmed_at (evidence-refresh anchor).
    Self-healing: safe to call from init/ingest/current."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(state_facts)").fetchall()}
    if not cols:
        return  # table not created yet; init_schema's CREATE handles a fresh DB
    if 'half_life_days' not in cols:
        conn.execute("ALTER TABLE state_facts ADD COLUMN half_life_days REAL")
        conn.commit()
    if 'last_confirmed_at' not in cols:
        conn.execute("ALTER TABLE state_facts ADD COLUMN last_confirmed_at TEXT")
        conn.commit()

def _age_days(valid_from, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        vf = datetime.fromisoformat(valid_from)
        if vf.tzinfo is None:
            vf = vf.replace(tzinfo=timezone.utc)
    except Exception:
        return 0.0
    return max(0.0, (now - vf).total_seconds() / 86400.0)

# CALIBRATION CAVEAT: eff_confidence is an UNCALIBRATED heuristic -- it has never
# been validated against real staleness rates. Use it ONLY for ordering and
# reconfirm-prompting; NEVER present it as a literal probability of truth
# (not "41% true" / "41% likely"). Render it in reconfirm-framed wording only.
# Deliberately NO Brier scores / calibration tooling -- out of scope for a homelab.
def effective_confidence(stored, half_life_days, valid_from, now=None, last_confirmed_at=None):
    """Read-time age decay; never mutates stored state. NULL/0 half-life =>
    never decays (returns stored confidence unchanged). Decay is measured from
    last_confirmed_at when present (evidence-refresh anchor: repeated observation
    resets the clock) else from valid_from. valid_from (world-truth) is untouched."""
    if not half_life_days:
        return float(stored)
    anchor = last_confirmed_at or valid_from
    age = _age_days(anchor, now)
    return float(stored) * (0.5 ** (age / float(half_life_days)))

def init_schema(conn=None):
    own = conn is None
    conn = conn or _conn()
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS state_episodes (
        id TEXT PRIMARY KEY,
        source TEXT,
        raw TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS state_facts (
        id TEXT PRIMARY KEY,
        subject TEXT NOT NULL,
        predicate TEXT NOT NULL,
        value TEXT NOT NULL,
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        ingested_at TEXT NOT NULL,
        invalidated_at TEXT,
        source_ref TEXT,
        confidence REAL DEFAULT 1.0,
        episode_id TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_sf_sp   ON state_facts(subject, predicate);
    CREATE INDEX IF NOT EXISTS idx_sf_open ON state_facts(subject, predicate, valid_to);
    CREATE INDEX IF NOT EXISTS idx_sf_valid ON state_facts(valid_from, valid_to);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_sf_one_open ON state_facts(subject, predicate) WHERE valid_to IS NULL;
    ''')
    conn.commit()
    _migrate(conn)
    if own: conn.close()

def ingest(subject, predicate, value, valid_from=None, source_ref=None,
           confidence=1.0, half_life_days=None, episode_id=None, conn=None):
    """Deterministic key-based supersession. Returns dict describing the action."""
    own = conn is None
    conn = conn or _conn()
    _migrate(conn)
    now = _now()
    valid_from = valid_from or now
    subject = subject.strip().lower()
    predicate = predicate.strip().lower()
    value = str(value).strip()
    # half-life resolution: explicit arg > volatile-predicate default > NULL (stable)
    if half_life_days is None:
        half_life_days = VOLATILE_HALFLIFE.get(predicate)

    # ── Memory V3 P1: fact classification + render tier (write-time) ─────────
    # core -> injected LIVE_FLEET_STATE block; topic -> on-demand (`current <subj>`,
    # LIVE_FLEET_STATE_FULL.md). Values >280 chars get a stderr nudge: long prose
    # belongs in an episode (lumen_episode.py commit) with a short fact pointing at it.
    _fc, _tier = _classify_fact(predicate, value)
    if len(value) > 280:
        print(f'[lumen_state] NOTE: value is {len(value)} chars (>280) -> render_tier=topic. '
              f'Prefer a short fact + full story as an episode.', file=sys.stderr)

    cur = conn.execute(
        'SELECT * FROM state_facts WHERE subject=? AND predicate=? AND valid_to IS NULL',
        (subject, predicate))
    open_row = cur.fetchone()

    if open_row is not None and open_row['value'] == value:
        # EVIDENCE REFRESH (supersedes the old pure-noop): re-observing an identical
        # value IS evidence, so bump the decay anchor (last_confirmed_at). No new row;
        # valid_from untouched (world-truth start stays fixed); stored confidence
        # untouched. history()/as_of() stay bit-for-bit reproducible.
        conn.execute('UPDATE state_facts SET last_confirmed_at=? WHERE id=?',
                     (now, open_row['id']))
        conn.commit()
        if own: conn.close()
        return {'action': 'reconfirmed', 'refreshed': True, 'subject': subject,
                'predicate': predicate, 'value': value}

    action = 'insert'
    fid = uuid.uuid4().hex

    # ── Out-of-order guard (Fable5 F-MEM-2, 2026-07-01) ──────────────────────
    # If a late/backfilled/clock-skewed fact arrives with a valid_from EARLIER
    # than the current open row, it must NOT supersede the open row (doing so
    # made an OLDER value 'current' and truncated the real current fact's window
    # to negative length). Instead insert it as a historical CLOSED row ending
    # where the open row began, leaving the open (current) row untouched.
    if open_row is not None and valid_from < open_row['valid_from']:
        conn.execute(
            '''INSERT INTO state_facts
               (id, subject, predicate, value, valid_from, valid_to,
                ingested_at, invalidated_at, source_ref, confidence, half_life_days,
                last_confirmed_at, episode_id, fact_class, render_tier)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (fid, subject, predicate, value, valid_from, open_row['valid_from'],
             now, now, source_ref, confidence, half_life_days, now, episode_id,
             _fc, _tier))
        conn.commit()
        res = {'action': 'historical_insert', 'subject': subject, 'predicate': predicate,
               'value': value, 'note': 'older-than-current; inserted as closed historical row, open row unchanged',
               'current': open_row['value']}
        if own: conn.close()
        return res

    if open_row is not None:
        # close (invalidate) the superseded fact -- do NOT delete
        conn.execute(
            'UPDATE state_facts SET valid_to=?, invalidated_at=? WHERE id=?',
            (valid_from, now, open_row['id']))
        action = 'superseded'

    conn.execute(
        '''INSERT INTO state_facts
           (id, subject, predicate, value, valid_from, valid_to,
            ingested_at, invalidated_at, source_ref, confidence, half_life_days,
            last_confirmed_at, episode_id, fact_class, render_tier)
           VALUES (?,?,?,?,?,NULL,?,NULL,?,?,?,?,?,?,?)''',
        (fid, subject, predicate, value, valid_from, now, source_ref, confidence,
         half_life_days, now, episode_id, _fc, _tier))
    conn.commit()
    res = {'action': action, 'subject': subject, 'predicate': predicate, 'value': value,
           'prior': (open_row['value'] if open_row else None)}
    if own: conn.close()
    return res

def current(subject=None, predicate=None, limit=None, min_confidence=0.0,
            now=None, conn=None):
    """Open facts (valid_to IS NULL). Each row dict also carries `confidence`
    (stored) and `eff_confidence` (read-time age-decayed). Rows whose
    eff_confidence < min_confidence are dropped (default 0.0 => keep all;
    soft-expiry is opt-in). Decay is never written back."""
    own = conn is None
    conn = conn or _conn()
    _migrate(conn)
    q = ('SELECT subject,predicate,value,valid_from,source_ref,confidence,half_life_days,last_confirmed_at,'
         'fact_class,render_tier '
         'FROM state_facts WHERE valid_to IS NULL')
    p = []
    if subject:   q += ' AND subject=?';   p.append(subject.strip().lower())
    if predicate: q += ' AND predicate=?'; p.append(predicate.strip().lower())
    q += ' ORDER BY subject, predicate'
    if limit is not None:
        q += ' LIMIT ?'; p.append(int(limit))
    rows = []
    for r in conn.execute(q, p).fetchall():
        d = dict(r)
        stored = d.get('confidence')
        stored = 1.0 if stored is None else float(stored)
        d['confidence'] = stored
        d['eff_confidence'] = effective_confidence(
            stored, d.get('half_life_days'), d['valid_from'], now=now,
            last_confirmed_at=d.get('last_confirmed_at'))
        if d['eff_confidence'] < min_confidence:
            continue
        rows.append(d)
    if own: conn.close()
    return rows

def as_of(when, subject=None, predicate=None, conn=None):
    """World-truth point-in-time: facts whose valid window contains `when`."""
    own = conn is None
    conn = conn or _conn()
    q = '''SELECT subject,predicate,value,valid_from,valid_to FROM state_facts
           WHERE valid_from<=? AND (valid_to IS NULL OR valid_to>?)'''
    p = [when, when]
    if subject:   q += ' AND subject=?';   p.append(subject.strip().lower())
    if predicate: q += ' AND predicate=?'; p.append(predicate.strip().lower())
    q += ' ORDER BY subject, predicate'
    rows = [dict(r) for r in conn.execute(q, p).fetchall()]
    if own: conn.close()
    return rows

def history(subject, predicate, conn=None):
    own = conn is None
    conn = conn or _conn()
    rows = [dict(r) for r in conn.execute(
        '''SELECT value,valid_from,valid_to,ingested_at,invalidated_at,source_ref
           FROM state_facts WHERE subject=? AND predicate=? ORDER BY valid_from''',
        (subject.strip().lower(), predicate.strip().lower())).fetchall()]
    if own: conn.close()
    return rows

def _main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('init')
    pi = sub.add_parser('ingest')
    pi.add_argument('subject'); pi.add_argument('predicate'); pi.add_argument('value')
    pi.add_argument('--valid-from', default=None); pi.add_argument('--source', default=None)
    pi.add_argument('--confidence', type=float, default=1.0); pi.add_argument('--half-life', type=float, default=None)
    pc = sub.add_parser('current'); pc.add_argument('subject', nargs='?'); pc.add_argument('predicate', nargs='?')
    pc.add_argument('--limit', type=int, default=None); pc.add_argument('--min-confidence', type=float, default=0.0)
    pa = sub.add_parser('asof'); pa.add_argument('when'); pa.add_argument('subject', nargs='?'); pa.add_argument('predicate', nargs='?')
    ph = sub.add_parser('history'); ph.add_argument('subject'); ph.add_argument('predicate')
    a = ap.parse_args()

    if a.cmd == 'init':
        init_schema(); print('schema ready'); return
    if a.cmd == 'ingest':
        print(ingest(a.subject, a.predicate, a.value, valid_from=a.valid_from, source_ref=a.source,
                     confidence=a.confidence, half_life_days=a.half_life)); return
    if a.cmd == 'current':
        for r in current(a.subject, a.predicate, limit=a.limit, min_confidence=a.min_confidence):
            line = f"{r['subject']:>10}.{r['predicate']:<14} = {r['value']}  (since {r['valid_from'][:10]})"
            if r.get('eff_confidence', 1.0) < 0.99:
                line += f"  [conf {r['eff_confidence']:.2f}]"
            print(line)
        return
    if a.cmd == 'asof':
        for r in as_of(a.when, a.subject, a.predicate):
            print(f"{r['subject']:>10}.{r['predicate']:<14} = {r['value']}")
        return
    if a.cmd == 'history':
        for r in history(a.subject, a.predicate):
            end = r['valid_to'][:10] if r['valid_to'] else 'NOW'
            print(f"{r['valid_from'][:10]} -> {end:<10} {r['value']}  [{r['source_ref'] or ''}]")
        return

if __name__ == '__main__':
    _main()
