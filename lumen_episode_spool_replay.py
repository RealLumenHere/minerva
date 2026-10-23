#!/usr/bin/env python3
"""
lumen_episode_spool_replay.py - wq#3173: drain EPISODE_SPOOL_DIR.

lumen_episode.py commit spools a payload here when memoryd's identity gate rejects
it (agent not registered / no live claim on source_ref / missing source_ref -- see
lumen_episode.py's module docstring). This script re-POSTs each spooled entry through
lumen_episode._memoryd_commit() -- the SAME code path and SAME gate the CLI used, so
an accept here means the gate's own current verdict changed, not a second copy of its
logic drifting out of sync with daemon.py.

Each entry replays under its ORIGINAL agent identity (WQ_AGENT is swapped in for the
call and restored after) -- a later run of the SAME rejected agent id is the only
thing "holds a live claim now" can mean once the CLI process that made the commit is
long gone.

Runs unattended off lumen-episode-spool-replay.timer (every 15 min, staging/). Safe to
run by hand or overlap: each entry is an independent file, a landed one is deleted, and
a still-rejected one is rewritten in place with an updated reason/attempt count.

wq#3309 TERMINAL STATE: an entry whose originating identity can never authenticate
again (purged/retired session, missing source_ref, re-claimed fworker receipt -- see
design/wq3309_spool_terminal_state.md in foreman -- or, wq#4538, a does-not-hold
reject that survived 3 replays of the identical payload) is classified BEFORE the POST,
its content verified in the local archival store (written there via the existing
local fallback writer when the spool holds the only copy), and the file moved to
spool/recovered/ -- instead of burning MAX_ATTEMPTS futile POSTs into expired/.
This is NOT a second copy of the accept gate: an accept still only ever happens
through le._memoryd_commit() -> daemon; the pre-check only decides whether a future
accept is POSSIBLE, and every failure mode degrades to "keep retrying".

CLI: lumen_episode_spool_replay.py [--dry-run]
"""
import argparse
import glob
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lumen_episode as le
import minerva_audit
import minerva_memory as mm

MAX_AGE_DAYS = 14
# wq#3309: with terminal classes exiting eagerly, this no longer doubles as the
# dead-identity backstop -- 96 = ~24 h at 15-min cadence for genuinely transient
# rejects. expired/ now means only "transient failure that never cleared".
MAX_ATTEMPTS = 96
GRACE_HOURS = 1          # dodge any registration race on fresh entries

QUEUE_DB = os.environ.get('WQ_QUEUE_DB', '/home/lumen/agents/work-queue/queue.db')
# Mirrors kernel.FWORKER_IDENTITY_RE (v4/memoryd/kernel.py:36), same env override.
FWORKER_IDENTITY_RE = re.compile(
    os.environ.get('MEMORYD_FWORKER_IDENTITY_RE', r'^fworker-wq(\d+)$'))


def _age_days(entry):
    try:
        spooled = datetime.fromisoformat(entry['spooled_at'])
    except Exception:
        return 0.0
    return (datetime.now(timezone.utc) - spooled).total_seconds() / 86400.0


def _ticket_claimant(tid):
    """Read-only mirror of kernel.ticket_claimant() (v4/memoryd/kernel.py:327):
    claimed_by is the claim RECEIPT -- survives close, written only by `wq claim`.
    None on any error (fail-open to retry)."""
    try:
        q = sqlite3.connect('file:%s?mode=ro' % QUEUE_DB, uri=True, timeout=3.0)
        try:
            r = q.execute('SELECT claimed_by FROM tasks WHERE id=?', (tid,)).fetchone()
            return r[0] if r else None
        finally:
            q.close()
    except Exception:
        return None


def _session_retired(agent):
    """True only when queue.db was READABLE and the id is absent from
    agent_sessions (purged -- bin/wq's stale sweep DELETEs the row) or has an
    explicit retired_sessions row (the stronger marker, bin/wq:508). Same SELECT
    kernel.agent_registered() runs (v4/memoryd/kernel.py:303), read-only.
    Fail-open to False: misclassifying toward retry is the status quo."""
    try:
        q = sqlite3.connect('file:%s?mode=ro' % QUEUE_DB, uri=True, timeout=3.0)
        try:
            try:
                if q.execute('SELECT 1 FROM retired_sessions WHERE session_id=?',
                             (agent,)).fetchone():
                    return True
            except sqlite3.Error:
                pass  # schema predates retired_sessions; the registry row decides
            return q.execute('SELECT 1 FROM agent_sessions WHERE session_id=?',
                             (agent,)).fetchone() is None
        finally:
            q.close()
    except Exception:
        return False


def _identity_terminal(entry):
    """(terminal: bool, why: str) -- wq#3309 reject taxonomy. Decides only whether
    a future accept is POSSIBLE; fail-open to 'retry' on any DB error."""
    agent = (entry.get('agent') or '').strip()
    src = (entry.get('source_ref') or '').strip()
    fw = FWORKER_IDENTITY_RE.match(agent)
    if fw:
        # R4: the claim receipt has been overwritten by a re-claim; it will never
        # name this fworker again. While claimed_by still matches (or is
        # unreadable), keep retrying.
        claimant = _ticket_claimant(fw.group(1))
        if claimant is not None and claimant != agent:
            return True, 'claim receipt on wq#%s now names %s (R4)' % (fw.group(1), claimant)
        return False, 'claim receipt intact or unreadable'
    if not src.startswith('wq#'):
        # R3: replay re-POSTs the identical payload; nothing about a retry adds
        # a source_ref. Terminal immediately, attempts=0.
        return True, 'no source_ref; a retry can never add one (R3)'
    # R2→terminal (wq#4538): post-wq#3352 the daemon already consults the claim
    # receipt before emitting "does not hold", so a does-not-hold that survives 3
    # replays (~45 min, outlasting any claim/registration race) means the receipt
    # names someone else or nobody — a state no future replay of the identical
    # payload can change. Covers both the session branch ("a live claim on") and
    # the fworker branch ("the claim on") message forms.
    if (int(entry.get('attempts', 0)) >= 3
            and str(entry.get('last_reason', '')).startswith('reject: agent does not hold')):
        return True, 'does-not-hold ×%d; receipt will never name this agent (R2-terminal)' % entry['attempts']
    if _age_days(entry) * 24.0 < GRACE_HOURS:
        return False, 'inside registration grace window'
    if _session_retired(agent):
        # R1: session ids carry a random per-session hex suffix (bin/wq:1321,
        # loops until unused), so a purged id never re-registers.
        return True, 'session id absent from agent_sessions (purged/retired) (R1)'
    return False, 'agent registered or registry unreadable'


def _audit_recovery(nid, entry, why):
    """One memory_audit row per recovery (invariant=wq3309-recovered-local,
    mirroring the wq#2901 breadcrumb pattern) so one SELECT counts recoveries.
    Raises on failure: a recovery that cannot leave its audit row falls back to
    the retry path instead of exiting silently."""
    conn = mm._connect()
    try:
        minerva_audit.audit(
            conn, minerva_audit.RECONFIRM, 'archival_notes', nid,
            source_ref=entry.get('source_ref') or None,
            detail='spool-replay recovery (local copy verified); original agent '
                   '%s unreplayable: %s' % (entry.get('agent'), why),
            invariant='wq3309-recovered-local')
        conn.commit()
    finally:
        conn.close()


def _recover(path, entry, why):
    """wq#3309 verify-then-move, never assume: prove the content is in the local
    archival store -- local note ids are deterministic over exactly the fields the
    entry carries (mm.note_id) -- write it there via the existing local fallback
    writer when the spool holds the only copy, then move the file to
    spool/recovered/ with a recovery record. Returns (outcome, detail), or None on
    any failure so the entry falls through to today's retry path unchanged.
    NOT service-identity re-attribution: that is rejected by design (the service
    identity is read-only by contract, daemon.py I-7)."""
    try:
        kind = entry.get('kind') or 'session'
        title = entry.get('title') or ''
        content = entry.get('content') or ''
        # Trust-but-reverify the commit-time local_nid stamp; legacy entries
        # (and a stale stamp) recompute via note_id().
        nid = entry.get('local_nid') or mm.note_id(kind, title, content)
        if not mm.note_exists(nid):
            nid = mm.note_id(kind, title, content)
        if mm.note_exists(nid):
            recovery = 'local-only'
            _audit_recovery(nid, entry, why)
        else:
            # Possible: _spool_rejected runs BEFORE the fallback write, which can
            # itself die (lumen_episode.py commit) -- the spool holds the only
            # copy. Write it locally with the original agent in the audit detail;
            # the spool is never the only copy lost.
            recovery = 'local-write'
            nid, _ = mm.write_note_verified(
                kind, title, content, tags=entry.get('tags') or [],
                source_ref=entry.get('source_ref') or '',
                audit={'action': minerva_audit.WRITE,
                       'source_ref': entry.get('source_ref') or None,
                       'detail': 'spool-replay local recovery; original agent '
                                 '%s unreplayable: %s' % (entry.get('agent'), why),
                       'invariant': 'wq3309-recovered-local'},
                embed_now=False)
        entry = dict(entry)
        entry.update({'recovered_at': datetime.now(timezone.utc).isoformat(),
                      'recovery': recovery, 'local_nid': nid, 'final_reason': why,
                      'attempts': int(entry.get('attempts', 0))})
        recovered_dir = os.path.join(os.path.dirname(path), 'recovered')
        os.makedirs(recovered_dir, mode=0o700, exist_ok=True)
        with open(path, 'w') as f:
            json.dump(entry, f)
        os.replace(path, os.path.join(recovered_dir, os.path.basename(path)))
        return 'recovered-local', '%s (local_nid=%s)' % (why, nid)
    except Exception:
        return None  # fall through to retry; losing eagerness loses nothing


def replay_one(path, dry_run=False):
    """(outcome, detail) for one spool file. Never raises -- a bad entry is reported,
    not fatal to the rest of the drain."""
    try:
        with open(path) as f:
            entry = json.load(f)
    except Exception as e:
        return 'unreadable', str(e)
    agent = (entry.get('agent') or '').strip()
    if not agent:
        return 'no-agent', 'nothing a replay could authenticate as'
    if dry_run:
        return 'dry-run', 'would retry as %s' % agent

    # wq#3309: terminality pre-check BEFORE the POST. A recovery failure returns
    # None and falls through to the ordinary retry below.
    terminal, why = _identity_terminal(entry)
    if terminal:
        recovered = _recover(path, entry, why)
        if recovered is not None:
            return recovered

    prev_agent = os.environ.get('WQ_AGENT')
    os.environ['WQ_AGENT'] = agent
    try:
        nid, reason = le._memoryd_commit(
            entry.get('kind') or 'session', entry.get('title') or '',
            entry.get('content') or '', entry.get('tags') or [],
            entry.get('source_ref') or '')
    finally:
        if prev_agent is None:
            os.environ.pop('WQ_AGENT', None)
        else:
            os.environ['WQ_AGENT'] = prev_agent

    if nid:
        os.remove(path)
        return 'landed', str(nid)

    entry['attempts'] = int(entry.get('attempts', 0)) + 1
    entry['last_reason'] = str(reason)[:300]
    entry['last_tried'] = datetime.now(timezone.utc).isoformat()
    if entry['attempts'] >= MAX_ATTEMPTS or _age_days(entry) >= MAX_AGE_DAYS:
        expired_dir = os.path.join(os.path.dirname(path), 'expired')
        os.makedirs(expired_dir, mode=0o700, exist_ok=True)
        os.replace(path, os.path.join(expired_dir, os.path.basename(path)))
        return 'expired', reason
    with open(path, 'w') as f:
        json.dump(entry, f)
    return 'still-rejected', reason


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help='list what would be retried without POSTing or mutating spool files')
    args = ap.parse_args()

    spool_dir = le.EPISODE_SPOOL_DIR
    os.makedirs(spool_dir, mode=0o700, exist_ok=True)
    paths = sorted(glob.glob(os.path.join(spool_dir, '*.json')))
    if not paths:
        print('spool empty (%s)' % spool_dir)
        return 0
    counts = {}
    for path in paths:
        outcome, detail = replay_one(path, dry_run=args.dry_run)
        counts[outcome] = counts.get(outcome, 0) + 1
        print('%s: %s (%s)' % (os.path.basename(path), outcome, detail))
    print('summary: ' + ', '.join('%s=%d' % kv for kv in sorted(counts.items())))
    return 0


if __name__ == '__main__':
    sys.exit(main())
