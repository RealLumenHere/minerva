#!/usr/bin/env python3
"""
lumen_salience.py - wq#576 [mv4-P4]: usage-aware salience + forgetting proposals.

Design of record: /home/lumen/minerva/v4/research/salience_forgetting.md (wq#571),
sections 2 (schema), 3 (Memory Worth), 4 (boost + floors), 5 (stationarity).

Everything here is either (a) additive logging that must never take recall down, or
(b) PROPOSAL-ONLY analysis. Nothing in this module deletes a note, edits
claude_memory.md, or changes retrieval ranking unless MEMORY_V4_SALIENCE=1 is set.

Layers:
  log_hits()          fail-open tap, called at lumen_recall2.search_v3 return
  rollup() / prune()  nightly, called from lumen_dream.run()
  label_outcome()     called from lumen_episode.commit() (outcome joiner)
  boost_map()         read by search_v3 ONLY when MEMORY_V4_SALIENCE=1
  archive_candidates() / write_archive_plan()   DARK forgetting proposer
"""
import hashlib
import math
import os
import sqlite3
import sys
import contextvars
from datetime import datetime, timezone, timedelta

DB = os.environ.get('MINERVA_DB', '/home/lumen/minerva/minerva.db')
QUEUE_DB = os.environ.get('WQ_DB', '/home/lumen/agents/work-queue/queue.db')
DREAM_DIR = os.environ.get('LUMEN_DREAM_DIR', '/home/lumen/minerva/dream')

# ---- flags (all default to CURRENT behaviour) ---------------------------
LOG_ON = os.environ.get('MEMORY_V4_HITLOG', '1') != '0'
SAL_ON = os.environ.get('MEMORY_V4_SALIENCE', '0') == '1'
W_USE = float(os.environ.get('MEMORY_V4_W_USE', '0.0'))
W_WORTH = float(os.environ.get('MEMORY_V4_W_WORTH', '0.0'))
HITS_NORM = float(os.environ.get('MEMORY_V4_HITS_NORM', '20'))
EPOCH = int(os.environ.get('MEMORY_V4_RETRIEVER_EPOCH', '2'))
RETENTION_DAYS = int(os.environ.get('MEMORY_V4_HIT_RETENTION_DAYS', '90'))

# Synthetic callers. LOGGED (so the exclusion is auditable rather than invisible)
# and filtered AT ROLLUP - research §2: they are the majority of would-be volume.
EXCLUDED_CALLERS = tuple(c for c in os.environ.get(
    'MEMORY_V4_EXCLUDE_CALLERS', 'bench,guard,selftest').split(',') if c)

# ---- floors (research §4c) - in code, not convention --------------------
FLOOR_AGE_DAYS = int(os.environ.get('MEMORY_V4_FLOOR_AGE_DAYS', '30'))
FORGET_AGE_DAYS = int(os.environ.get('MEMORY_V4_FORGET_AGE_DAYS', '180'))
FORGET_LCB = float(os.environ.get('MEMORY_V4_FORGET_LCB', '0.35'))

_NOISE_FAIL = {'', 'none', 'nothing', 'n/a', 'na', '-', 'nil', 'no failures'}

_CALLER_MAP = (
    ('recall_bench.py', 'bench'),
    ('/bench_', 'bench'),                 # any eval/bench_*.py harness
    ('memory_embed_guard.py', 'guard'),
    ('v4/memoryd/', 'memoryd'),
    ('minerva_proposal.py', 'proposal'),
    ('lumen_agent_v4.py', 'agent'),
    ('lumen_dream.py', 'dream'),
)

# Our own frames and the recall module's are transparent: the question is who
# asked for the recall, not which function physically wrote the row.
_SKIP_FILES = ('lumen_salience.py', 'lumen_recall2.py')

# Per-request session id for long-running SERVERS (lumen-memoryd), which have no
# WQ_AGENT of their own and serve many lanes from one process. A ContextVar, not
# an env var: env is process-global and would race across concurrent requests.
# Without this every daemon-served hit is unattributable, and the outcome join in
# §3 -- which keys on session_id -- would be blind to exactly the traffic the
# lanes generate.
_SESSION = contextvars.ContextVar('lumen_salience_session', default=None)


def set_session(session_id):
    """Called by a server at the top of a request. Fail-open, returns a token."""
    try:
        return _SESSION.set((session_id or '')[:64])
    except Exception:
        return None


def caller_tag():
    """Who asked. Explicit env wins; otherwise inferred from the call stack.

    Stack inference is deliberate: it tags memoryd traffic (daemon proxies
    search_v3 byte-for-byte) without editing the daemon, and it cannot be
    forgotten by a new caller the way an opt-in argument can.
    """
    t = os.environ.get('MEMORY_V4_CALLER')
    if t:
        return t[:40]
    try:
        import traceback
        frames = traceback.extract_stack()[:-1]
        for fr in reversed(frames):
            fn = fr.filename or ''
            if any(fn.endswith(s) for s in _SKIP_FILES):
                continue
            for needle, tag in _CALLER_MAP:
                if needle in fn:
                    return tag
            if fn.endswith('lumen_episode.py'):
                names = {f.name for f in frames
                         if (f.filename or '').endswith('lumen_episode.py')}
                return 'precheck' if 'precheck' in names else 'recall-cli'
            return (os.path.basename(fn) or 'direct')[:40]
    except Exception:
        pass
    return 'direct'


def _now():
    return datetime.now(timezone.utc).isoformat()


def _connect(db=None, timeout=5.0):
    con = sqlite3.connect(db or DB, timeout=timeout)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA busy_timeout=5000')
    return con


class _rowmode(object):
    """Borrow a caller's connection with sqlite3.Row set, then hand it back
    exactly as it was found. lumen_dream passes a plain tuple-factory connection
    and keeps using it afterwards, so mutating it permanently would be a
    side-effect on someone else's code. (Caught by running the real dream job,
    not by reading it.)"""

    def __init__(self, con):
        self.con = con
        self.prev = None

    def __enter__(self):
        self.prev = self.con.row_factory
        self.con.row_factory = sqlite3.Row
        return self.con

    def __exit__(self, *a):
        self.con.row_factory = self.prev
        return False


# ======================= 1. THE TAP (fail-open) =========================

def log_hits(query, hits, caller=None, db=None):
    """Write <=top_k rows at search_v3 return. NEVER raises. Returns rows written.

    Fail-open is not a nicety here: wq#465 showed one exception on the recall
    path silently degrades the whole fleet. Logging is strictly less important
    than recall, so every failure mode ends in `return 0`.
    """
    if not LOG_ON or not hits:
        return 0
    try:
        tag = (caller or caller_tag())[:40]
        qh = hashlib.sha1((query or '').encode('utf-8', 'replace')).hexdigest()[:12]
        sess = (_SESSION.get() or os.environ.get('WQ_AGENT')
                or os.environ.get('LUMEN_SESSION') or '')[:64]
        ts = _now()
        rows = [(str(h.get('id') or ''), ts, i, tag, qh, sess, None, EPOCH)
                for i, h in enumerate(hits, 1) if h.get('id')]
        if not rows:
            return 0
        con = sqlite3.connect(db or DB, timeout=2.0)
        try:
            con.execute('PRAGMA busy_timeout=2000')
            # synchronous=NORMAL on THIS connection only. In WAL mode that means
            # the commit does not fsync the WAL every time; the worst case is
            # losing the last few hit rows on a power cut. This table is an
            # explicitly droppable 90-day forensic log -- paying a full fsync per
            # recall for it is the wrong trade, and measured (576_logcost.json) it
            # is the whole cost: 8.5ms p50 per write at FULL.
            con.execute('PRAGMA synchronous=NORMAL')
            con.executemany(
                'INSERT INTO note_hits(note_id,ts,rank,caller,query_hash,'
                'session_id,episode_id,retriever_epoch) VALUES (?,?,?,?,?,?,?,?)',
                rows)
            con.commit()
        finally:
            con.close()
        return len(rows)
    except Exception:
        return 0


# ==================== 2. MEMORY WORTH (Beta-Bernoulli) ==================
# Stdlib-only regularized incomplete beta + quantile by bisection. No scipy
# dependency on a box whose python env is shared with the agent lanes.

def _betacf(a, b, x, itmax=200, eps=3e-12):
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < 1e-300:
        d = 1e-300
    d = 1.0 / d
    h = d
    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-300:
            d = 1e-300
        c = 1.0 + aa / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-300:
            d = 1e-300
        c = 1.0 + aa / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def betainc(a, b, x):
    """Regularized incomplete beta I_x(a,b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
             + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(lbeta) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lbeta) * _betacf(b, a, 1.0 - x) / b


def beta_ppf(a, b, q, tol=1e-7):
    """Quantile of Beta(a,b) by bisection on the CDF. Monotone -> safe."""
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if betainc(a, b, mid) < q:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2.0


def worth_stats(succ, fail, alpha=1.0, beta_prior=1.0, q=0.05):
    """(posterior mean, lower confidence bound).

    Laplace prior alpha=beta=1: a note nobody ever retrieved sits at 0.5, not 0
    -- it must not be punished for the retriever's blind spots (research §3).
    Forgetting ranks on the LCB so that 1 failure / 0 successes cannot outrank
    40 failures / 5 successes for demotion. Confidence has to be earned.
    """
    a = float(succ) + alpha
    b = float(fail) + beta_prior
    return a / (a + b), beta_ppf(a, b, q)


# ======================= 3. FLOORS (research §4c) =======================

def _age_days(created_at, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        d = datetime.strptime((created_at or '')[:10], '%Y-%m-%d').replace(
            tzinfo=timezone.utc)
        return (now - d).total_seconds() / 86400.0
    except Exception:
        return 0.0        # unparseable date -> treat as NEW, i.e. protected


def floor_reason(row, now=None):
    """Return why this note may never be demoted, or None if it is eligible.

    A floor-protected note may still be BOOSTED; it may never be DEMOTED.
    Not tunable downward by a counter -- that is the whole point of the
    'I told you yesterday' failure mode.
    """
    trust = (row['trust'] if 'trust' in row.keys() else None) or ''
    if trust.lower() == 'operator':
        return 'trust=operator'
    origin = (row['origin'] if 'origin' in row.keys() else None) or ''
    if origin.lower() == 'state-mirror':
        return 'origin=state-mirror (mirrors core-tier facts)'
    age = _age_days(row['created_at'], now)
    if age <= FLOOR_AGE_DAYS:
        return 'age<=%dd' % FLOOR_AGE_DAYS
    return None


# ==================== 4. OUTCOME JOINER (research §3) ===================

def label_outcome(episode_id, failed_str='', session_id=None, ref='', db=None):
    """Attribute this session's logged hits to its terminal episode.

    Called from lumen_episode.commit(). Fail-open: an episode commit must never
    die because the salience joiner did.
    """
    try:
        sess = (session_id or os.environ.get('WQ_AGENT')
                or os.environ.get('LUMEN_SESSION') or '')[:64]
        f = (failed_str or '').strip().lower()
        if f and f not in _NOISE_FAIL:
            outcome, reason = 'failure', 'episode --failed non-empty'
        else:
            # Not success yet: the wq ticket usually reaches done AFTER commit.
            outcome, reason = 'pending', 'awaiting wq/fq resolution'
        con = _connect(db)
        try:
            con.execute(
                'INSERT OR REPLACE INTO note_hit_outcomes'
                '(episode_id,session_id,outcome,reason,labeled_at,resolved_at)'
                ' VALUES (?,?,?,?,?,?)',
                (str(episode_id), sess, outcome, (reason + ' ' + ref)[:200],
                 _now(), _now() if outcome == 'failure' else None))
            n = 0
            if sess:
                cur = con.execute(
                    'UPDATE note_hits SET episode_id=? WHERE session_id=? '
                    'AND episode_id IS NULL', (str(episode_id), sess))
                n = cur.rowcount
            con.commit()
        finally:
            con.close()
        return outcome, n
    except Exception:
        return None, 0


def _wq_verdict(session_id):
    """success/failure/None from the work queue for a session's tickets."""
    try:
        if not session_id or not os.path.exists(QUEUE_DB):
            return None
        con = sqlite3.connect('file:%s?mode=ro' % QUEUE_DB, uri=True, timeout=5)
        rows = con.execute(
            'SELECT status, count(*) FROM tasks WHERE claimed_by=? GROUP BY status',
            (session_id,)).fetchall()
        con.close()
        st = {r[0]: r[1] for r in rows}
        if not st:
            return None
        if st.get('blocked'):
            return 'failure'
        if st.get('done'):
            return 'success'
        return None
    except Exception:
        return None


def resolve_pending(con):
    """Turn 'pending' labels into success/failure once wq/fq have spoken."""
    resolved = 0
    with _rowmode(con):
        rows = con.execute("SELECT episode_id, session_id FROM note_hit_outcomes "
                           "WHERE outcome='pending'").fetchall()
    for r in rows:
        v = _wq_verdict(r['session_id'])
        if v:
            con.execute('UPDATE note_hit_outcomes SET outcome=?, resolved_at=?, '
                        'reason=? WHERE episode_id=?',
                        (v, _now(), 'resolved from wq ticket status', r['episode_id']))
            resolved += 1
    con.commit()
    return resolved


# ================= 5. ROLLUP + PRUNE (research §2, §5) ==================

def _excl_sql(prefix='caller'):
    if not EXCLUDED_CALLERS:
        return '1=1', []
    ph = ','.join('?' * len(EXCLUDED_CALLERS))
    return '%s NOT IN (%s)' % (prefix, ph), list(EXCLUDED_CALLERS)


def rollup(con=None, epoch=None, window_days=28):
    """Recompute note_salience from the raw log. Idempotent; cheap; nightly.

    Excludes: synthetic callers (filtered here, not at the tap, so the exclusion
    is auditable), revoked notes (wq#610 fixture notes must never accrue worth),
    and any epoch other than the current retriever epoch (research §5 - counters
    earned under a different retriever measure a different function).
    """
    own = con is None
    con = con or _connect()
    epoch = EPOCH if epoch is None else epoch
    try:
      with _rowmode(con):
        resolve_pending(con)
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=window_days)).isoformat()
        excl, params = _excl_sql('h.caller')
        sql = ('SELECT h.note_id AS nid, COUNT(*) AS hits, MAX(h.ts) AS last_hit, '
               "SUM(CASE WHEN o.outcome='success' THEN 1 ELSE 0 END) AS succ, "
               "SUM(CASE WHEN o.outcome='failure' THEN 1 ELSE 0 END) AS fail, "
               'SUM(CASE WHEN h.ts>? THEN 1 ELSE 0 END) AS hits_w, '
               "SUM(CASE WHEN h.ts>? AND o.outcome='success' THEN 1 ELSE 0 END) AS succ_w, "
               "SUM(CASE WHEN h.ts>? AND o.outcome='failure' THEN 1 ELSE 0 END) AS fail_w "
               'FROM note_hits h '
               'JOIN archival_notes n ON n.id=h.note_id AND n.revoked_at IS NULL '
               'LEFT JOIN note_hit_outcomes o ON o.episode_id=h.episode_id '
               'WHERE h.retriever_epoch=? AND ' + excl + ' GROUP BY h.note_id')
        rows = con.execute(sql, [cutoff, cutoff, cutoff, epoch] + params).fetchall()
        now = _now()
        con.execute('DELETE FROM note_salience')
        for r in rows:
            w, lcb = worth_stats(r['succ'], r['fail'])
            w28, _ = worth_stats(r['succ_w'], r['fail_w'])
            con.execute(
                'INSERT INTO note_salience(note_id,hits,last_hit_at,succ,fail,'
                'worth,worth_lcb,hits_28d,worth_28d,epoch,computed_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (r['nid'], r['hits'], r['last_hit'], r['succ'], r['fail'],
                 round(w, 4), round(lcb, 4), r['hits_w'], round(w28, 4), epoch, now))
        con.execute("INSERT OR REPLACE INTO salience_meta(key,value,updated_at) "
                    "VALUES ('last_rollup_at',?,?)", (now, now))
        con.commit()
        return len(rows)
    finally:
        if own:
            con.close()


def prune(con=None, days=None):
    """Drop raw hits older than the retention window. The ROLLUP is durable;
    the raw log is a 90-day forensic window (research §2)."""
    own = con is None
    con = con or _connect()
    days = RETENTION_DAYS if days is None else days
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = con.execute('DELETE FROM note_hits WHERE ts < ?', (cutoff,))
        con.commit()
        return cur.rowcount
    finally:
        if own:
            con.close()


# ============ 6. BOOST (read path; OFF unless MEMORY_V4_SALIENCE=1) =====

def boost_map(con, ids):
    """note_id -> additive score delta. {} when the flag is off.

    ADDITIVE and NON-FILTERING by construction: a multiplicative or filtering
    usage term makes a cold-start note permanently unreachable (never retrieved
    -> never used -> never boosted -> never retrieved). The worth term is SIGNED
    and centred on the prior, so an unproven note contributes exactly 0.
    """
    if not SAL_ON or not ids or (W_USE == 0.0 and W_WORTH == 0.0):
        return {}
    try:
      with _rowmode(con):
        ids = list(ids)
        out = {}
        denom = math.log1p(HITS_NORM) or 1.0
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            ph = ','.join('?' * len(chunk))
            for r in con.execute(
                    'SELECT note_id,hits,worth FROM note_salience '
                    'WHERE note_id IN (%s)' % ph, chunk):
                g = min(1.0, math.log1p(r['hits'] or 0) / denom)
                w = r['worth'] if r['worth'] is not None else 0.5
                out[r['note_id']] = W_USE * g + W_WORTH * (w - 0.5)
        return out
    except Exception:
        return {}


def archived_ids(con):
    """Ids demoted by an operator-applied archive plan. Consulted by recall only
    when MEMORY_V4_ARCHIVE_FILTER=1 (default 0 - demotion is inert until a later
    ticket adopts it on a measured win)."""
    try:
        return {r[0] for r in con.execute(
            'SELECT note_id FROM note_archives WHERE restored_at IS NULL')}
    except Exception:
        return set()


# ========== 7. FORGETTING PROPOSER - DARK (research §4b) ================

def archive_candidates(con=None, age_days=None, lcb_max=None):
    """Notes that COULD be proposed for archival. Never acts. Returns list of dict.

    Rule (research §4b): age > 180d AND hits == 0 AND worth_lcb < 0.35 AND not
    floor-protected. On today's corpus this matches ~nothing by design: 3 notes
    are over 90 days old. A proposer with nothing to propose is untestable
    against reality, which is why this stays dark and is proved on fixtures.
    """
    own = con is None
    con = con or _connect()
    age_days = FORGET_AGE_DAYS if age_days is None else age_days
    lcb_max = FORGET_LCB if lcb_max is None else lcb_max
    try:
      with _rowmode(con):
        now = datetime.now(timezone.utc)
        arch = archived_ids(con)
        out = []
        for r in con.execute(
                'SELECT n.id,n.title,n.kind,n.created_at,n.trust,n.origin,'
                'COALESCE(s.hits,0) AS hits, s.succ, s.fail, s.worth_lcb '
                'FROM archival_notes n LEFT JOIN note_salience s ON s.note_id=n.id '
                'WHERE n.revoked_at IS NULL').fetchall():
            if r['id'] in arch:
                continue
            fr = floor_reason(r, now)
            if fr:
                continue
            if _age_days(r['created_at'], now) <= age_days:
                continue
            if (r['hits'] or 0) != 0:
                continue
            lcb = r['worth_lcb']
            if lcb is None:
                lcb = worth_stats(r['succ'] or 0, r['fail'] or 0)[1]
            if lcb >= lcb_max:
                continue
            # worth_ucb is carried for the operator but NOT gated on. See
            # reports/576_salience.md: research §3's own worked example (0succ/1fail
            # vs 5succ/40fail) requires the UPPER bound to express "confidence must
            # earn the demotion"; the LOWER bound ranks them the other way round.
            # And under `hits == 0` the worth clause is vacuous either way (a
            # never-retrieved note sits at the prior). Reported, not silently changed.
            ucb = worth_stats(r['succ'] or 0, r['fail'] or 0, q=0.95)[1]
            out.append({'id': r['id'], 'title': (r['title'] or '')[:90],
                        'kind': r['kind'], 'created_at': r['created_at'],
                        'trust': r['trust'], 'origin': r['origin'],
                        'hits': r['hits'] or 0, 'worth_lcb': round(lcb, 4),
                        'worth_ucb': round(ucb, 4)})
        return out
    finally:
        if own:
            con.close()


def write_archive_plan(cands, out_dir=None):
    """PROPOSAL ONLY. Mirrors lumen_dream.write_merge_plan: a JSON file an
    operator applies by hand. The timer NEVER calls apply_archive_plan()."""
    import json
    from pathlib import Path
    d = Path(out_dir or DREAM_DIR)
    d.mkdir(parents=True, exist_ok=True)
    p = d / ('archive_plan_%s.json' % datetime.now(timezone.utc).strftime('%Y-%m-%d'))
    p.write_text(json.dumps({
        'generated': _now(),
        'rule': 'age>%dd AND hits==0 AND worth_lcb<%s AND not floor-protected'
                % (FORGET_AGE_DAYS, FORGET_LCB),
        'action': 'ARCHIVE (demote) - never DELETE. Reversible: '
                  'UPDATE note_archives SET restored_at=... WHERE note_id=?',
        'apply': 'python3 lumen_salience.py apply --plan <file> --confirm',
        'count': len(cands), 'candidates': cands}, indent=1))
    return p


def apply_archive_plan(path, confirm=False, db=None):
    """Operator-gated. Archive-not-erase: writes note_archives, never deletes."""
    import json
    from pathlib import Path
    plan = json.loads(Path(path).read_text())
    cands = plan.get('candidates') or []
    if not confirm:
        print('DRY: %d archive candidates in plan. Re-run with --confirm.'
              % len(cands))
        return 0
    con = _connect(db)
    n = 0
    try:
        for c in cands:
            con.execute(
                'INSERT OR IGNORE INTO note_archives'
                '(note_id,archived_at,reason,worth_lcb,hits,plan_ref) '
                'VALUES (?,?,?,?,?,?)',
                (c['id'], _now(), plan.get('rule', '')[:200],
                 c.get('worth_lcb'), c.get('hits'), str(path)))
            n += 1
        con.commit()
    finally:
        con.close()
    print('archived %d note(s) (demoted, not deleted)' % n)
    return n


# ============================== 8. CLI =================================

def stats(db=None):
    con = _connect(db)
    try:
        def one(q, p=()):
            r = con.execute(q, p).fetchone()
            return r[0] if r else 0
        print('note_hits rows      : %d' % one('SELECT count(*) FROM note_hits'))
        print('  distinct notes    : %d' % one('SELECT count(DISTINCT note_id) FROM note_hits'))
        print('  by caller         : ' + ', '.join(
            '%s=%d' % (r[0], r[1]) for r in con.execute(
                'SELECT caller,count(*) FROM note_hits GROUP BY caller ORDER BY 2 DESC')))
        print('  epochs            : ' + ', '.join(
            '%s=%d' % (r[0], r[1]) for r in con.execute(
                'SELECT retriever_epoch,count(*) FROM note_hits GROUP BY 1')))
        print('note_salience rows  : %d' % one('SELECT count(*) FROM note_salience'))
        print('outcomes            : ' + ', '.join(
            '%s=%d' % (r[0], r[1]) for r in con.execute(
                'SELECT outcome,count(*) FROM note_hit_outcomes GROUP BY 1')))
        print('archived            : %d' % one('SELECT count(*) FROM note_archives'))
        print('flags               : HITLOG=%s SALIENCE=%s W_USE=%s W_WORTH=%s '
              'epoch=%s excl=%s' % (LOG_ON, SAL_ON, W_USE, W_WORTH, EPOCH,
                                    ','.join(EXCLUDED_CALLERS)))
    finally:
        con.close()


def _main():
    import argparse
    ap = argparse.ArgumentParser(
        description='wq#576 usage-aware salience (logging + rollup + DARK forgetting proposer)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('stats')
    r = sub.add_parser('rollup'); r.add_argument('--epoch', type=int, default=None)
    p = sub.add_parser('prune'); p.add_argument('--days', type=int, default=None)
    pr = sub.add_parser('propose')
    pr.add_argument('--age-days', type=int, default=None)
    pr.add_argument('--lcb-max', type=float, default=None)
    pr.add_argument('--out-dir', default=None)
    ap_ = sub.add_parser('apply')
    ap_.add_argument('--plan', required=True)
    ap_.add_argument('--confirm', action='store_true')
    a = ap.parse_args()
    if a.cmd == 'stats':
        stats()
    elif a.cmd == 'rollup':
        print('rolled up %d note(s)' % rollup(epoch=a.epoch))
    elif a.cmd == 'prune':
        print('pruned %d hit row(s) older than %sd'
              % (prune(days=a.days), a.days or RETENTION_DAYS))
    elif a.cmd == 'propose':
        c = archive_candidates(age_days=a.age_days, lcb_max=a.lcb_max)
        path = write_archive_plan(c, out_dir=a.out_dir)
        print('%d archive candidate(s) -> %s  (PROPOSAL ONLY, nothing applied)'
              % (len(c), path))
        for x in c[:20]:
            print('  %s  lcb=%.3f hits=%d  %s' % (x['id'][:12], x['worth_lcb'],
                                                  x['hits'], x['title'][:60]))
    elif a.cmd == 'apply':
        apply_archive_plan(a.plan, confirm=a.confirm)


if __name__ == '__main__':
    _main()
