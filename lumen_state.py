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
                        [--stated-by-frank] [--source-kind KIND]
  lumen_state.py current [subject] [predicate]   # + an evidence column per fact
  lumen_state.py asof <ISO_DATE> [subject] [predicate]
  lumen_state.py history <subject> <predicate>
  lumen_state.py check                           # wq#1700 invariants, exit 1 on violation

ONE OWNER PER FACT (wq#1700, STATE_SINGLE_OWNER_DESIGN.md): the (subject,predicate)
key only works if the predicate names the fact. ingest() therefore canonicalises
synonym predicates to their owner (state_registry.json aliases), refuses predicates
the store has never seen unless the writer gives a reason (new_predicate), and
treats `lifecycle` as a closed enum whose decommissioned/retired values close the
subject's operational facts and refuse later operational writes.

AUDITED WRITE PATH (wq#1634, MINERVA_PIPELINE_MAP.md §4.5): every accepted ingest
appends a `memory_audit` row (table_name='state_facts', action=write|supersede|
reconfirm, agent_id=$WQ_AGENT, lane read from the ticket in source_ref) INSIDE the
same transaction as the fact, and stamps `source_kind` on the fact itself. "Who
wrote this fact and from which ticket" is now a SELECT, not an archaeology project.
"""
import sqlite3, sys, uuid, argparse, os, json, fnmatch, difflib
from datetime import datetime, timezone

import minerva_audit

DB = '/home/lumen/minerva/minerva.db'
REGISTRY_PATH = os.environ.get(
    'LUMEN_STATE_REGISTRY',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'state_registry.json'))

class StateGateError(ValueError):
    """A write refused by the wq#1700 gate. Nothing was written."""

class PredicateNotRegistered(StateGateError):
    pass

class LifecycleValueError(StateGateError):
    pass

class LifecycleClosedError(StateGateError):
    pass

_REGISTRY = {}

def registry(path=None):
    """state_registry.json, loaded once per path. Missing file is a hard error:
    a gate that silently opens when its policy file is gone is not a gate."""
    path = path or REGISTRY_PATH
    if path not in _REGISTRY:
        with open(path) as f:
            doc = json.load(f)
        lc = doc.get('lifecycle', {})
        _REGISTRY[path] = {
            'lifecycle': lc.get('predicate', 'lifecycle'),
            'enum': tuple(lc.get('enum', ())),
            'closing': tuple(lc.get('closing', ())),
            'aliases': dict(doc.get('aliases', {})),
            'namespaces': dict(doc.get('open_namespaces', {})),
            'core': set(doc.get('core_predicates', ())),
            'operational': set(doc.get('operational_predicates', ())),
            'op_patterns': tuple(doc.get('operational_patterns', ())),
        }
    return _REGISTRY[path]

def is_operational(predicate, reg=None):
    reg = reg or registry()
    return predicate in reg['operational'] or any(
        fnmatch.fnmatchcase(predicate, p) for p in reg['op_patterns'])

def canonical_predicate(predicate, reg=None):
    reg = reg or registry()
    return reg['aliases'].get(predicate, predicate)

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

def _cols(conn):
    return {r[1] for r in conn.execute("PRAGMA table_info(state_facts)").fetchall()}

def _migrate(conn):
    """Add nullable columns if missing (SQLite has no ADD COLUMN IF NOT EXISTS):
    half_life_days (decay rate) and last_confirmed_at (evidence-refresh anchor).
    Self-healing: safe to call from init/ingest/current."""
    cols = _cols(conn)
    if not cols:
        return  # table not created yet; init_schema's CREATE handles a fresh DB
    if 'half_life_days' not in cols:
        conn.execute("ALTER TABLE state_facts ADD COLUMN half_life_days REAL")
        conn.commit()
    if 'last_confirmed_at' not in cols:
        conn.execute("ALTER TABLE state_facts ADD COLUMN last_confirmed_at TEXT")
        conn.commit()
    # V3 P1 added these by hand-run backfill, so a DB built by init_schema alone
    # was missing them and could not ingest at all (found by wq#1612's tests).
    for col in ('fact_class', 'render_tier'):
        if col not in cols:
            conn.execute(f"ALTER TABLE state_facts ADD COLUMN {col} TEXT")
            conn.commit()
    # wq#1700: lineage for closed rows (the fact id that closed it: a supersession,
    # a lifecycle closure, or an alias owner). source_ref is never rewritten.
    if 'closed_by' not in cols:
        conn.execute("ALTER TABLE state_facts ADD COLUMN closed_by TEXT")
        conn.commit()
    # wq#1700: who introduced a predicate the store had never seen, and why.
    # Checked via sqlite_master first so read-only callers never take a write lock.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='state_predicate_additions'").fetchone():
        conn.execute('''CREATE TABLE IF NOT EXISTS state_predicate_additions (
            predicate TEXT PRIMARY KEY, first_subject TEXT, why TEXT NOT NULL,
            agent TEXT, source_ref TEXT, added_at TEXT NOT NULL)''')
        conn.commit()
    # wq#1634: how this fact came to be known (frank_stated / observed /
    # agent_written / unattributed), stamped at ingest. Additive and nullable,
    # matching wq#1613's own add-if-missing migration for the same column.
    if 'source_kind' not in cols:
        conn.execute("ALTER TABLE state_facts ADD COLUMN source_kind TEXT")
        conn.commit()
    minerva_audit.ensure_table(conn)

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

# ── wq#1612: staleness -- ordinal, visible, never a probability ──────────────
# MINERVA_STALENESS_REVIEW.md §3.6. The calibration caveat above applies here
# too: this orders facts and prompts reconfirmation. It is NEVER rendered as
# "N% true", and there is deliberately no probability behind it.
FRESH, DUE, STALE = 'FRESH', 'DUE', 'STALE'
UNVERIFIABLE, CONTRADICTED = 'UNVERIFIABLE', 'CONTRADICTED'

# Evidence TTL per fact_class when the fact carries no verify_interval_days (§3.3).
# None = no probe owns the class: narrative prose is the prose lifecycle's job,
# so it reads as UNVERIFIABLE (a coverage gap) rather than as decay.
VERIFY_INTERVAL_CLASS_DAYS = {'status': 3.0, 'config': 14.0,
                              'identity': 90.0, 'narrative': None}
VERIFY_INTERVAL_DEFAULT_DAYS = 14.0
DUE_RATIO, STALE_RATIO = 1.0, 3.0

# Columns 3.1 adds and the verifier registry (fix #6) populates. current() selects
# whichever of them exist, so this file works before and after that migration.
STALENESS_COLS = ('verify_recipe', 'verify_interval_days', 'contradicted_at')

def verify_interval_days(row):
    """Evidence TTL for one fact: explicit verify_interval_days, else the
    fact_class default. None => no probe owns this fact."""
    explicit = row.get('verify_interval_days')
    if explicit:
        return float(explicit)
    fc = row.get('fact_class') or ''
    if fc in VERIFY_INTERVAL_CLASS_DAYS:
        return VERIFY_INTERVAL_CLASS_DAYS[fc]
    return VERIFY_INTERVAL_DEFAULT_DAYS

def evidence_age_days(row, now=None):
    """Days since the fact was last observed. last_confirmed_at is the
    reconfirm-on-match anchor; valid_from covers facts never re-observed."""
    return _age_days(row.get('last_confirmed_at') or row['valid_from'], now)

def staleness(row, now=None):
    """Ordinal staleness class per §3.6 -> (cls, evidence_age_days, interval).

    Precedence: CONTRADICTED (hard, overrides all) > UNVERIFIABLE (coverage, not
    decay) > the age ratio. `verify_recipe` is consulted only when the column is
    present: the verifier registry (fix #6) lands after this, and reading its
    absence as "no probe" would label every fact UNVERIFIABLE and drain the
    signal this exists to add. When the column does land, a NULL recipe
    classifies as UNVERIFIABLE with no further change here."""
    age = evidence_age_days(row, now)
    if row.get('contradicted_at'):
        return CONTRADICTED, age, None
    interval = verify_interval_days(row)
    if interval is None or ('verify_recipe' in row and not row['verify_recipe']):
        return UNVERIFIABLE, age, None
    ratio = age / interval
    cls = FRESH if ratio < DUE_RATIO else (DUE if ratio < STALE_RATIO else STALE)
    return cls, age, interval

def _evidence_label(cls, age, row):
    """The evidence column `current` prints. Ordinal wording only."""
    if cls == CONTRADICTED:
        note = row.get('contradicted_by_note')
        return f'[⚠ CONTRADICTED by note {note}]' if note else '[⚠ CONTRADICTED]'
    if cls == UNVERIFIABLE:
        return '[UNVERIFIABLE — no probe]'
    days = int(round(age))
    if cls == FRESH:
        return '[verified today]' if days < 1 else f'[verified {days}d ago]'
    probe = row.get('verify_recipe')
    tail = f', probe {probe}' if probe else ' unverified'
    return f'[{cls} — {days}d{tail}]'

def annotate(row, now=None):
    """Attach staleness class, evidence age and evidence label to a current()
    row, in place. Display only -- nothing here is written back to the store."""
    cls, age, _interval = staleness(row, now)
    row['staleness'] = cls
    row['evidence_age_days'] = age
    row['evidence'] = _evidence_label(cls, age, row)
    return row

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

def _gate_predicate(conn, subject, predicate, new_predicate, source_ref, now, reg):
    """wq#1700 predicate gate. Accepts: open-namespace subject, registry predicate,
    a predicate the store already has rows for, or a recorded addition. A new
    predicate needs a reason, which is recorded. Returns the addition row to write
    (inside the ingest transaction) or None. Raises PredicateNotRegistered."""
    if subject in reg['namespaces']:
        return None
    if (predicate == reg['lifecycle'] or predicate in reg['core']
            or is_operational(predicate, reg)):
        return None
    if conn.execute('SELECT 1 FROM state_facts WHERE predicate=? LIMIT 1',
                    (predicate,)).fetchone():
        return None
    if conn.execute('SELECT 1 FROM state_predicate_additions WHERE predicate=?',
                    (predicate,)).fetchone():
        return None
    if new_predicate and str(new_predicate).strip():
        return (predicate, subject, str(new_predicate).strip(),
                os.environ.get('WQ_AGENT'), source_ref, now)
    known = {r[0] for r in conn.execute('SELECT DISTINCT predicate FROM state_facts')}
    known |= reg['core'] | reg['operational'] | set(reg['aliases'])
    near = difflib.get_close_matches(predicate, sorted(known), n=4, cutoff=0.6)
    hint = f" Nearest existing: {', '.join(near)}." if near else ''
    raise PredicateNotRegistered(
        f"predicate '{predicate}' is new to state_facts.{hint} If this fact is one of "
        f"those, write it there (supersession only works on the owner predicate). "
        f"If it is a genuinely new axis, pass --new-predicate '<why>'.")

def closed_ids(conn, lifecycle_id):
    """(id, predicate) of every row a lifecycle fact closed -- the wq#1700 `closed_by`
    lineage column read back, so wq#1634 can audit each closure by row id."""
    return [(r['id'], r['predicate']) for r in conn.execute(
        'SELECT id, predicate FROM state_facts WHERE closed_by=? ORDER BY predicate',
        (lifecycle_id,)).fetchall()]

def _open_lifecycle(conn, subject, reg):
    return conn.execute(
        'SELECT id, value, valid_from FROM state_facts WHERE subject=? AND predicate=? '
        'AND valid_to IS NULL', (subject, reg['lifecycle'])).fetchone()

def close_operational(conn, subject, lifecycle_id, lifecycle_valid_from, now, reg=None):
    """Close every open operational fact on `subject` (wq#1700 §2). valid_to is the
    lifecycle fact's valid_from, never earlier than the row's own valid_from.
    source_ref untouched; closed_by = the lifecycle fact id. Caller commits."""
    reg = reg or registry()
    closed = []
    for r in conn.execute(
            'SELECT id, predicate, valid_from FROM state_facts WHERE subject=? '
            'AND valid_to IS NULL AND predicate != ?', (subject, reg['lifecycle'])).fetchall():
        if not is_operational(r['predicate'], reg):
            continue
        vt = max(r['valid_from'], lifecycle_valid_from)
        conn.execute('UPDATE state_facts SET valid_to=?, invalidated_at=?, closed_by=? '
                     'WHERE id=?', (vt, now, lifecycle_id, r['id']))
        closed.append(r['predicate'])
    return sorted(closed)

def ingest(subject, predicate, value, valid_from=None, source_ref=None,
           confidence=1.0, half_life_days=None, episode_id=None, conn=None,
           new_predicate=None, stated_by_frank=False, source_kind=None):
    """Deterministic key-based supersession behind the wq#1700 gate.
    Returns dict describing the action; raises StateGateError (nothing written).

    wq#1634: every accepted write also appends a memory_audit row in the same
    transaction, and stamps source_kind on the fact it writes."""
    own = conn is None
    conn = conn or _conn()
    try:
        return _ingest(conn, subject, predicate, value, valid_from, source_ref,
                       confidence, half_life_days, episode_id, new_predicate,
                       stated_by_frank, source_kind)
    except Exception:
        if own: conn.rollback()
        raise
    finally:
        if own: conn.close()

def _ingest(conn, subject, predicate, value, valid_from, source_ref,
            confidence, half_life_days, episode_id, new_predicate,
            stated_by_frank=False, source_kind=None):
    _migrate(conn)
    reg = registry()
    now = _now()
    valid_from = valid_from or now
    subject = subject.strip().lower()
    asked = predicate.strip().lower()
    predicate = canonical_predicate(asked, reg)
    value = str(value).strip()
    # wq#1634: resolved BEFORE the gate so an invalid --source-kind is refused with
    # the other gate errors, i.e. with nothing written.
    kind = minerva_audit.resolve_source_kind(stated_by_frank, source_kind)
    _lane = minerva_audit.ticket_lane(source_ref)

    def _audit(action, row_id, detail=None):
        minerva_audit.audit(conn, action, 'state_facts', row_id,
                            source_ref=source_ref, detail=detail,
                            source_kind=kind, lane=_lane)

    # ── wq#1700 gate: nothing below this block runs for a refused write ─────
    if predicate == reg['lifecycle']:
        value = value.lower()
        if value not in reg['enum']:
            raise LifecycleValueError(
                f"lifecycle is a closed enum {list(reg['enum'])}; got '{value}'. "
                f"Put the story in `status`.")
    addition = _gate_predicate(conn, subject, predicate, new_predicate, source_ref, now, reg)
    lc_closed = None
    if predicate != reg['lifecycle'] and is_operational(predicate, reg):
        lc = _open_lifecycle(conn, subject, reg)
        if lc is not None and lc['value'] in reg['closing']:
            lc_closed = lc
        if lc_closed is not None and valid_from >= lc['valid_from']:
            raise LifecycleClosedError(
                f"{subject} is {lc['value']} since {lc['valid_from'][:10]}; '{predicate}' is an "
                f"operational fact and would contradict it. If the subject is back in service, "
                f"ingest `{subject} lifecycle active` first.")
    if addition is not None:
        conn.execute('INSERT INTO state_predicate_additions '
                     '(predicate, first_subject, why, agent, source_ref, added_at) '
                     'VALUES (?,?,?,?,?,?)', addition)
    alias_note = {'alias_of': predicate, 'asked': asked} if asked != predicate else {}

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

    open_row = conn.execute(
        'SELECT * FROM state_facts WHERE subject=? AND predicate=? AND valid_to IS NULL',
        (subject, predicate)).fetchone()

    if open_row is not None and open_row['value'] == value:
        # EVIDENCE REFRESH (supersedes the old pure-noop): re-observing an identical
        # value IS evidence, so bump the decay anchor (last_confirmed_at). No new row;
        # valid_from untouched (world-truth start stays fixed); stored confidence
        # untouched. history()/as_of() stay bit-for-bit reproducible.
        conn.execute('UPDATE state_facts SET last_confirmed_at=? WHERE id=?',
                     (now, open_row['id']))
        _audit(minerva_audit.RECONFIRM, open_row['id'],
               f'{subject}.{predicate}={value[:120]}')
        conn.commit()
        return {'action': 'reconfirmed', 'refreshed': True, 'subject': subject,
                'predicate': predicate, 'value': value, **alias_note}

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
                last_confirmed_at, episode_id, fact_class, render_tier, closed_by,
                source_kind)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (fid, subject, predicate, value, valid_from, open_row['valid_from'],
             now, now, source_ref, confidence, half_life_days, now, episode_id,
             _fc, _tier, open_row['id'], kind))
        _audit(minerva_audit.WRITE, fid,
               f'{subject}.{predicate}={value[:120]} historical:older-than-current '
               f'closed_by={open_row["id"]}')
        conn.commit()
        return {'action': 'historical_insert', 'subject': subject, 'predicate': predicate,
                'value': value, 'note': 'older-than-current; inserted as closed historical row, open row unchanged',
                'current': open_row['value'], **alias_note}

    if lc_closed is not None:
        # wq#1700: a past operational fact for a subject that has since been
        # decommissioned/retired. It was true then; it must not come back open.
        # Land it closed at the lifecycle boundary (closing a legacy open row it
        # supersedes on the way), so the write cannot recreate the contradiction.
        if open_row is not None:
            conn.execute(
                'UPDATE state_facts SET valid_to=?, invalidated_at=?, closed_by=? WHERE id=?',
                (valid_from, now, fid, open_row['id']))
        conn.execute(
            '''INSERT INTO state_facts
               (id, subject, predicate, value, valid_from, valid_to,
                ingested_at, invalidated_at, source_ref, confidence, half_life_days,
                last_confirmed_at, episode_id, fact_class, render_tier, closed_by,
                source_kind)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (fid, subject, predicate, value, valid_from, lc_closed['valid_from'],
             now, now, source_ref, confidence, half_life_days, now, episode_id,
             _fc, _tier, lc_closed['id'], kind))
        _audit(minerva_audit.WRITE, fid,
               f'{subject}.{predicate}={value[:120]} historical:'
               f'{subject}-is-{lc_closed["value"]} closed_by={lc_closed["id"]}')
        conn.commit()
        return {'action': 'historical_insert', 'subject': subject, 'predicate': predicate,
                'value': value, 'note': f"{subject} is {lc_closed['value']}; inserted closed at "
                f"{lc_closed['valid_from'][:10]}", 'prior': (open_row['value'] if open_row else None),
                **alias_note}

    if open_row is not None:
        # close (invalidate) the superseded fact -- do NOT delete
        conn.execute(
            'UPDATE state_facts SET valid_to=?, invalidated_at=?, closed_by=? WHERE id=?',
            (valid_from, now, fid, open_row['id']))
        action = 'superseded'

    conn.execute(
        '''INSERT INTO state_facts
           (id, subject, predicate, value, valid_from, valid_to,
            ingested_at, invalidated_at, source_ref, confidence, half_life_days,
            last_confirmed_at, episode_id, fact_class, render_tier, source_kind)
           VALUES (?,?,?,?,?,NULL,?,NULL,?,?,?,?,?,?,?,?)''',
        (fid, subject, predicate, value, valid_from, now, source_ref, confidence,
         half_life_days, now, episode_id, _fc, _tier, kind))
    _audit(minerva_audit.SUPERSEDE if open_row is not None else minerva_audit.WRITE,
           fid, f'{subject}.{predicate}={value[:120]}'
                + (f' prior={open_row["id"]}' if open_row is not None else ''))
    closed = []
    if predicate == reg['lifecycle'] and value in reg['closing']:
        closed = close_operational(conn, subject, fid, valid_from, now, reg)
        # One row per closed sibling: a lifecycle closure IS a write to each of
        # those facts, so "which writes touched this row" has to answer for them
        # too, not just for the lifecycle fact that triggered them.
        for cid, cpred in closed_ids(conn, fid):
            _audit(minerva_audit.SUPERSEDE, cid,
                   f'{subject}.{cpred} closed by lifecycle {value} ({fid})')
    conn.commit()
    res = {'action': action, 'subject': subject, 'predicate': predicate, 'value': value,
           'prior': (open_row['value'] if open_row else None), **alias_note}
    if closed:
        res['closed'] = closed
    if addition is not None:
        res['new_predicate'] = addition[2]
    return res

VIOLATION_CLASSES = ('open_alias_row', 'operational_under_closed_lifecycle',
                     'lifecycle_outside_enum', 'multiple_open_rows')

def check(conn=None):
    """wq#1700 invariant scan over open rows. Returns {'violations': [...], 'ok': bool}.
    Each violation: {'class', 'subject', 'predicate', 'id', 'detail'}."""
    own = conn is None
    conn = conn or _conn()
    _migrate(conn)
    reg = registry()
    out = []
    for r in conn.execute('SELECT id, subject, predicate, value FROM state_facts '
                          'WHERE valid_to IS NULL').fetchall():
        if r['predicate'] in reg['aliases']:
            out.append({'class': 'open_alias_row', 'subject': r['subject'],
                        'predicate': r['predicate'], 'id': r['id'],
                        'detail': f"alias of {reg['aliases'][r['predicate']]}"})
        if r['predicate'] == reg['lifecycle'] and r['value'] not in reg['enum']:
            out.append({'class': 'lifecycle_outside_enum', 'subject': r['subject'],
                        'predicate': r['predicate'], 'id': r['id'], 'detail': r['value']})
    for lc in conn.execute('SELECT id, subject, value FROM state_facts WHERE predicate=? '
                           'AND valid_to IS NULL', (reg['lifecycle'],)).fetchall():
        if lc['value'] not in reg['closing']:
            continue
        for r in conn.execute('SELECT id, predicate FROM state_facts WHERE subject=? '
                              'AND valid_to IS NULL AND predicate != ?',
                              (lc['subject'], reg['lifecycle'])).fetchall():
            if is_operational(r['predicate'], reg):
                out.append({'class': 'operational_under_closed_lifecycle',
                            'subject': lc['subject'], 'predicate': r['predicate'],
                            'id': r['id'], 'detail': f"lifecycle={lc['value']}"})
    for r in conn.execute('SELECT subject, predicate, COUNT(*) n FROM state_facts '
                          'WHERE valid_to IS NULL GROUP BY 1,2 HAVING n > 1').fetchall():
        out.append({'class': 'multiple_open_rows', 'subject': r['subject'],
                    'predicate': r['predicate'], 'id': None, 'detail': f"{r['n']} open"})
    if own: conn.close()
    return {'ok': not out, 'violations': out}

def current(subject=None, predicate=None, limit=None, min_confidence=0.0,
            now=None, conn=None):
    """Open facts (valid_to IS NULL). Each row dict also carries `confidence`
    (stored), `eff_confidence` (read-time age-decayed) and the wq#1612 evidence
    annotation (`staleness`, `evidence_age_days`, `evidence`). Rows whose
    eff_confidence < min_confidence are dropped (default 0.0 => keep all;
    soft-expiry is opt-in). Decay is never written back."""
    own = conn is None
    conn = conn or _conn()
    _migrate(conn)
    cols = ['subject', 'predicate', 'value', 'valid_from', 'source_ref', 'confidence',
            'half_life_days', 'last_confirmed_at', 'fact_class', 'render_tier']
    present = _cols(conn)
    cols += [c for c in STALENESS_COLS if c in present]
    q = f'SELECT {",".join(cols)} FROM state_facts WHERE valid_to IS NULL'
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
        rows.append(annotate(d, now=now))
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
    pi.add_argument('--new-predicate', default=None, metavar='WHY',
                    help='wq#1700: reason for introducing a predicate state_facts has never seen')
    pi.add_argument('--stated-by-frank', action='store_true',
                    help='wq#1634: Frank asserted this himself -> source_kind=frank_stated, '
                         'which wq#1613 never supersedes on a probe mismatch')
    pi.add_argument('--source-kind', default=None, choices=minerva_audit.SOURCE_KINDS,
                    help='override the stamped source_kind (probes pass "observed")')
    sub.add_parser('check')
    pc = sub.add_parser('current'); pc.add_argument('subject', nargs='?'); pc.add_argument('predicate', nargs='?')
    pc.add_argument('--limit', type=int, default=None); pc.add_argument('--min-confidence', type=float, default=0.0)
    pa = sub.add_parser('asof'); pa.add_argument('when'); pa.add_argument('subject', nargs='?'); pa.add_argument('predicate', nargs='?')
    ph = sub.add_parser('history'); ph.add_argument('subject'); ph.add_argument('predicate')
    a = ap.parse_args()

    if a.cmd == 'init':
        init_schema(); print('schema ready'); return
    if a.cmd == 'ingest':
        try:
            print(ingest(a.subject, a.predicate, a.value, valid_from=a.valid_from, source_ref=a.source,
                         confidence=a.confidence, half_life_days=a.half_life,
                         new_predicate=a.new_predicate,
                         stated_by_frank=a.stated_by_frank,
                         source_kind=a.source_kind))
        except StateGateError as e:
            print(f'REFUSED ({type(e).__name__}): {e}', file=sys.stderr)
            sys.exit(2)
        return
    if a.cmd == 'check':
        res = check()
        for v in res['violations']:
            print(f"VIOLATION {v['class']}: {v['subject']}.{v['predicate']}  {v['detail']}  [{v['id']}]")
        print(f"check: {'OK' if res['ok'] else 'FAIL'} ({len(res['violations'])} violation(s))")
        sys.exit(0 if res['ok'] else 1)
    if a.cmd == 'current':
        for r in current(a.subject, a.predicate, limit=a.limit, min_confidence=a.min_confidence):
            line = f"{r['subject']:>10}.{r['predicate']:<14} = {r['value']}  (since {r['valid_from'][:10]})"
            line += f"  {r['evidence']}"
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
