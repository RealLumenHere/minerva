#!/usr/bin/env python3
"""
lumen_coverage.py - one shape for "what did we actually check?" (wq#1611).

MINERVA_STALENESS_REVIEW.md §2.3: `fleet-inv drift` reports 0 DRIFT / 34 NO-FACT
and exits 0. `state_reconcile` prints "no tailscale_ip drift; store matches the
wire." The dream report prints "None - prose IPs match the store." All three say
*success* when the honest reading is "nothing was checked". Unverifiable is a
reported, alarmed quantity, separate from verified-clean.

Two vocabularies live here, deliberately kept apart:

  Coverage  - per-run reconciliation coverage. checked / matched / drifted /
              unverifiable. Every reconciler and audit emits this line. The
              format is a cross-repo contract: fleet-inv (a different checkout,
              /home/lumen/fleet/inventory/fleet-inv) renders the same string
              from its own counters - change one, change both.

  classify()- per-fact staleness, §3.6. fresh / due / stale / unverifiable /
              contradicted, delegated to lumen_state.staleness() (wq#1612) so
              this line and the LIVE_FLEET_STATE block can never disagree about
              the same fact. UNVERIFIABLE is a coverage verdict, not a decay
              verdict: it means nothing declares when this fact should be
              re-checked, so no amount of elapsed time can make it "stale".

CLI:
  lumen_coverage.py line              # the handoff verify one-liner
  lumen_coverage.py facts --json      # the same numbers as JSON
  lumen_coverage.py check [--aq]      # week-over-week; exit 3 if stale+unverifiable rose
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import lumen_state

DB = os.environ.get('MINERVA_DB', '/home/lumen/minerva/minerva.db')
AQ = '/home/lumen/bin/aq'

BUCKETS = ('fresh', 'due', 'stale', 'unverifiable', 'contradicted')

# Week-over-week comparison looks for the newest snapshot at least this old, so a
# stray same-day re-run can never become the "last week" it compares against.
WEEK_DAYS = 6.0


def _now():
    return datetime.now(timezone.utc)


def _conn(db=None):
    c = sqlite3.connect(db or DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


# ── per-run reconciliation coverage ──────────────────────────────────────────

@dataclass
class Coverage:
    """checked/matched/drifted/unverifiable for one reconciler run.

    `unverifiable` is the count the review is about: fields nobody could compare,
    for any reason. `detail` names the reasons (no-fact, not-seen, ...) so the
    line stays one line while keeping the breakdown addressable.
    """
    scope: str
    checked: int = 0
    matched: int = 0
    drifted: int = 0
    unverifiable: int = 0
    detail: dict = field(default_factory=dict)

    def count(self, verdict, reason=None):
        """Record one comparison. verdict: matched | drifted | unverifiable."""
        if verdict not in ('matched', 'drifted', 'unverifiable'):
            raise ValueError(f'unknown verdict {verdict!r}')
        self.checked += 1
        setattr(self, verdict, getattr(self, verdict) + 1)
        if reason:
            self.detail[reason] = self.detail.get(reason, 0) + 1

    def line(self):
        s = (f'coverage[{self.scope}]: checked {self.checked} · matched {self.matched}'
             f' · drifted {self.drifted} · unverifiable {self.unverifiable}')
        if self.detail:
            s += ' (' + ', '.join(f'{k} {self.detail[k]}' for k in sorted(self.detail)) + ')'
        return s

    def clean(self):
        """True only when everything checked matched. Nothing-checked is not clean."""
        return self.checked > 0 and self.drifted == 0 and self.unverifiable == 0

    def as_dict(self):
        return {'scope': self.scope, 'checked': self.checked, 'matched': self.matched,
                'drifted': self.drifted, 'unverifiable': self.unverifiable,
                'detail': dict(self.detail)}


# ── per-fact staleness (§3.6) ────────────────────────────────────────────────

def classify(row, now=None):
    """One of BUCKETS for an open fact. Delegates to the §3.6 classifier that
    `lumen_state current` and LIVE_FLEET_STATE already render (wq#1612) - one
    definition of DUE, or the block and this line disagree about the same fact."""
    return lumen_state.staleness(dict(row), now)[0].lower()


def fact_coverage(conn=None, now=None, db=None):
    """Bucket every open fact. Returns {'facts': N, 'fresh': N, ...}."""
    own = conn is None
    conn = conn or _conn(db)
    try:
        rows = conn.execute(
            'SELECT * FROM state_facts WHERE valid_to IS NULL').fetchall()
    finally:
        if own:
            conn.close()
    counts = {b: 0 for b in BUCKETS}
    for r in rows:
        counts[classify(r, now)] += 1
    counts['facts'] = len(rows)
    return counts


def fact_line(counts):
    return ('facts {facts} · fresh {fresh} · due {due} · stale {stale}'
            ' · unverifiable {unverifiable} · contradicted {contradicted}').format(**counts)


# ── week-over-week regression ────────────────────────────────────────────────

def init_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS coverage_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        taken_at TEXT NOT NULL,
        scope TEXT NOT NULL,
        payload TEXT NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_cov_scope ON coverage_snapshots(scope, taken_at)')
    conn.commit()


def record(conn, counts, scope='facts', now=None):
    init_schema(conn)
    conn.execute('INSERT INTO coverage_snapshots (taken_at, scope, payload) VALUES (?,?,?)',
                 ((now or _now()).isoformat(), scope, json.dumps(counts, sort_keys=True)))
    conn.commit()


def previous(conn, scope='facts', now=None, week_days=WEEK_DAYS):
    """Newest snapshot at least week_days old, or None on the first week."""
    init_schema(conn)
    cutoff = ((now or _now()) - timedelta(days=week_days)).isoformat()
    r = conn.execute('SELECT payload FROM coverage_snapshots WHERE scope=? AND taken_at<=?'
                     ' ORDER BY taken_at DESC LIMIT 1', (scope, cutoff)).fetchone()
    return json.loads(r['payload']) if r else None


def unverified(counts):
    """The alarmed quantity: facts nobody has confirmed and nobody can."""
    return counts.get('stale', 0) + counts.get('unverifiable', 0)


def check(conn=None, now=None, raise_aq=False, scope='facts', db=None):
    """Snapshot today's coverage and compare with a week ago.

    Returns (counts, prev, delta). delta is None on the first run - there is
    nothing to compare against, which is not a regression.
    """
    own = conn is None
    conn = conn or _conn(db)
    try:
        counts = fact_coverage(conn, now)
        prev = previous(conn, scope, now)
        delta = None if prev is None else unverified(counts) - unverified(prev)
        record(conn, counts, scope, now)
    finally:
        if own:
            conn.close()
    if raise_aq and delta and delta > 0:
        _aq(counts, prev, delta, now)
    return counts, prev, delta


def _aq(counts, prev, delta, now=None):
    """One aq row per ISO week; aq dedups on (source, key)."""
    week = (now or _now()).strftime('%G-W%V')
    key = f'fact-coverage-{week}'
    title = f'fact coverage regressed: stale+unverifiable +{delta} week over week'
    msg = (f'now: {fact_line(counts)}\nweek ago: {fact_line(prev)}\n'
           'Unverifiable means no fact declares when it should be re-checked '
           '(MINERVA_STALENESS_REVIEW.md §2.3, §3.6).')
    try:
        subprocess.run([AQ, 'add', 'minerva', key, title, msg],
                       capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        print(f'# aq add failed: {e}', file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='lumen_coverage.py', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('line', help='one-line fact coverage (handoff verify)')
    p = sub.add_parser('facts', help='fact coverage counts')
    p.add_argument('--json', action='store_true')
    p = sub.add_parser('check', help='snapshot + week-over-week; exit 3 on regression')
    p.add_argument('--aq', action='store_true', help='raise an aq row on regression')
    args = ap.parse_args(argv)

    if args.cmd == 'line':
        print(fact_line(fact_coverage()))
        return 0
    if args.cmd == 'facts':
        counts = fact_coverage()
        print(json.dumps(counts, sort_keys=True) if args.json else fact_line(counts))
        return 0
    counts, prev, delta = check(raise_aq=args.aq)
    print(fact_line(counts))
    if delta is None:
        print('# no snapshot from a week ago yet; baseline recorded')
        return 0
    print(f'# stale+unverifiable {unverified(prev)} -> {unverified(counts)} ({delta:+d}) week over week')
    return 3 if delta > 0 else 0


if __name__ == '__main__':
    sys.exit(main())
