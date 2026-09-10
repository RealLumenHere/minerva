#!/usr/bin/env python3
"""wq#1700 one-time data migration: one owner predicate per fact + lifecycle backfill.

STATE_SINGLE_OWNER_DESIGN.md §Migration. Runs the shipped lumen_state.ingest()
(so lifecycle closure happens on the one code path) against:

  default (dry-run) : a throwaway copy of the DB; prints what --apply would do
  --apply           : a sqlite .backup copy is written first, then the real DB

Idempotent: a second --apply reports zero changes. Exit 0 iff `check` is clean after.

  lumen_state_single_owner_migrate.py [--db PATH] [--apply] [--backup-dir DIR] [--report FILE]
"""
import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lumen_state as ls  # noqa: E402

# Reviewed 2026-09-10 against live rows. Each entry is pinned to the prefix the
# subject's open `status` value had at review; if the prose has changed since,
# the subject is skipped and reported rather than guessed.
LIFECYCLE_PLAN = (
    ('nephthys', 'decommissioned', 'DECOMMISSIONED'),
    ('pbox', 'decommissioned', 'DECOMMISSIONED'),
    ('tapo-p12', 'decommissioned', 'DECOMMISSIONED'),
    ('leantime', 'retired', 'RETIRED'),
    ('leantime_research_bridge', 'retired', 'RETIRED'),
    ('mem0', 'retired', 'RETIRED'),
    ('llama-gemma4-mtp', 'retired', 'RETIRED'),
    ('llama33-local', 'retired', 'TORN DOWN'),
    ('lumen-agent', 'retired', 'MASKED'),
)
NOT_BACKFILLED = {
    'mirror': "store says decommissioned 2026-08-28, Frank's current preferences list it in use "
              "for RTD ISP flashing; Frank's statement outranks the store",
    'wadjet': 'retired from the vault-standby ROLE; box kept with WoL armed, node lifecycle never stated',
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def backup(src, dest_dir):
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, 'minerva.db.%s.pre_wq1700' %
                        datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    s = sqlite3.connect(src)
    d = sqlite3.connect(dest)
    s.backup(d)
    d.close(); s.close()
    return dest


def migrate_aliases(conn, reg):
    out = []
    aliases = reg['aliases']
    q = 'SELECT * FROM state_facts WHERE valid_to IS NULL AND predicate IN (%s)' % \
        ','.join('?' * len(aliases))
    for a in conn.execute(q, tuple(aliases)).fetchall():
        owner_pred = aliases[a['predicate']]
        owner = conn.execute('SELECT id, value FROM state_facts WHERE subject=? AND predicate=? '
                             'AND valid_to IS NULL', (a['subject'], owner_pred)).fetchone()
        now = _now()
        if owner is not None:
            owner_id, how = owner['id'], ('owner_kept_same_value' if owner['value'] == a['value']
                                          else 'owner_kept_alias_value_dropped')
        else:
            ls.ingest(a['subject'], owner_pred, a['value'], valid_from=a['valid_from'],
                      source_ref=a['source_ref'], confidence=a['confidence'] or 1.0, conn=conn)
            owner_id = conn.execute('SELECT id FROM state_facts WHERE subject=? AND predicate=? '
                                    'AND valid_to IS NULL', (a['subject'], owner_pred)).fetchone()['id']
            # carry the evidence age across: re-keying is not a new observation
            conn.execute('UPDATE state_facts SET last_confirmed_at=? WHERE id=?',
                         (a['last_confirmed_at'] or a['valid_from'], owner_id))
            how = 'value_moved_to_owner'
        conn.execute('UPDATE state_facts SET valid_to=?, invalidated_at=?, closed_by=? WHERE id=?',
                     (max(now, a['valid_from']), now, owner_id, a['id']))
        conn.commit()
        out.append({'subject': a['subject'], 'alias': a['predicate'], 'owner': owner_pred,
                    'how': how, 'alias_value': a['value'][:80],
                    'owner_value': (owner['value'][:80] if owner is not None else None)})
    return out


def migrate_lifecycle(conn, reg):
    done, skipped = [], []
    for subject, state, prefix in LIFECYCLE_PLAN:
        lc = conn.execute('SELECT value FROM state_facts WHERE subject=? AND predicate=? '
                          'AND valid_to IS NULL', (subject, reg['lifecycle'])).fetchone()
        if lc is not None:
            skipped.append({'subject': subject, 'why': f"lifecycle already open = {lc['value']}"})
            continue
        st = conn.execute("SELECT * FROM state_facts WHERE subject=? AND predicate='status' "
                          "AND valid_to IS NULL", (subject,)).fetchone()
        if st is None:
            skipped.append({'subject': subject, 'why': 'no open status fact'})
            continue
        if not st['value'].strip().upper().startswith(prefix):
            skipped.append({'subject': subject,
                            'why': f"status no longer starts with {prefix!r}: {st['value'][:60]!r}"})
            continue
        res = ls.ingest(subject, reg['lifecycle'], state, valid_from=st['valid_from'],
                        source_ref=f"wq#1700 backfill from status fact {st['id']}",
                        confidence=0.9, conn=conn)
        conn.execute("UPDATE state_facts SET last_confirmed_at=? WHERE subject=? AND predicate=? "
                     "AND valid_to IS NULL", (st['last_confirmed_at'] or st['valid_from'],
                                              subject, reg['lifecycle']))
        conn.commit()
        done.append({'subject': subject, 'lifecycle': state, 'since': st['valid_from'][:10],
                     'closed': res.get('closed', [])})
    return done, skipped


def run(db):
    # lumen_state resolves DB at call time; restore it so a dry-run against a
    # throwaway copy never leaves the module pointed at a deleted file.
    prior = ls.DB
    ls.DB = db
    try:
        reg = ls.registry()
        conn = ls._conn()
        ls._migrate(conn)
        before = ls.check(conn)
        aliases = migrate_aliases(conn, reg)
        lifecycle, skipped = migrate_lifecycle(conn, reg)
        after = ls.check(conn)
        conn.close()
    finally:
        ls.DB = prior
    changes = len(aliases) + len(lifecycle)
    return {'db': db, 'changes': changes, 'aliases': aliases, 'lifecycle': lifecycle,
            'lifecycle_skipped': skipped, 'not_backfilled': NOT_BACKFILLED,
            'check_before': {'ok': before['ok'], 'n': len(before['violations']),
                             'violations': before['violations']},
            'check_after': {'ok': after['ok'], 'n': len(after['violations']),
                            'violations': after['violations']}}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', default=ls.DB)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--backup-dir', default='/home/lumen/backups/wq1700')
    ap.add_argument('--report')
    a = ap.parse_args(argv)
    if a.apply:
        bak = backup(a.db, a.backup_dir)
        rep = run(a.db)
        rep['mode'], rep['backup'] = 'apply', bak
    else:
        with tempfile.TemporaryDirectory(prefix='wq1700-dry-') as td:
            copy = backup(a.db, td)
            rep = run(copy)
            rep['mode'], rep['db'] = 'dry-run (throwaway copy)', a.db
    text = json.dumps(rep, indent=2)
    if a.report:
        with open(a.report, 'w') as f:
            f.write(text + '\n')
    print(text)
    return 0 if rep['check_after']['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
