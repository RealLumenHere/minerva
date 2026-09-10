"""wq#1700 migration: legacy rows written before the gate existed (raw inserts),
then the migration, then idempotency + invariants."""
import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import lumen_state as ls  # noqa: E402
import lumen_state_single_owner_migrate as mig  # noqa: E402

D1, D2 = '2026-06-28T00:00:00+00:00', '2026-08-22T00:00:00+00:00'


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(ls, 'DB', str(tmp_path / 'legacy.db'))
    ls.init_schema()
    c = sqlite3.connect(ls.DB)
    rows = [
        ('n1', 'nephthys', 'serving_model', 'qwen3:14b', D1, 'orig-serving'),
        ('n2', 'nephthys', 'embed_model', 'nomic', D1, 'orig-embed'),
        ('n3', 'nephthys', 'dc_handle_store', 'LIVE v1', D1, 'orig-dc'),
        ('n4', 'nephthys', 'ram', '64GB', D1, 'dmidecode'),
        ('n5', 'nephthys', 'status', 'DECOMMISSIONED 2026-08-22 (wq#589)', D2, 'wq#589'),
        ('n6', 'nephthys', 'ts_ip', '100.98.74.87', D1, 'a'),
        ('n7', 'nephthys', 'tailscale_ip', '100.98.74.87', D1, 'b'),
        ('n8', 'nephthys', 'role_status', 'DECOMMISSIONED', D2, 'c'),
        ('b1', 'bes', 'hw', 'Trigkey N100 16GB', D1, 'old'),
        ('b2', 'bes', 'hardware', 'Trigkey Green G4 rev 10 sn BN1003', D1, 'dmidecode'),
        ('k1', 'khepri', 'hw', 'Mac Pro 6,1 64GB', D1, 'khepri-src'),
        ('l1', 'leantime', 'status', 'RETIRED 2026-07-16 (wq#267)', D1, 'wq#267'),
        ('m1', 'mem0', 'status', 'now ACTIVE again after rebuild', D2, 'x'),   # prose drifted
        ('w1', 'wadjet', 'status', 'FULLY RETIRED from vault-standby role', D1, 'Frank'),
        ('p1', 'lpp', 'status', 'ACTIVE — retired the old brain', D1, 'x'),
    ]
    for fid, s, p, v, vf, src in rows:
        c.execute('INSERT INTO state_facts (id,subject,predicate,value,valid_from,ingested_at,'
                  'source_ref,last_confirmed_at) VALUES (?,?,?,?,?,?,?,?)',
                  (fid, s, p, v, vf, vf, src, vf))
    c.commit(); c.close()
    return ls.DB


def q(sql, *p):
    c = sqlite3.connect(ls.DB)
    c.row_factory = sqlite3.Row
    out = [dict(r) for r in c.execute(sql, p).fetchall()]
    c.close()
    return out


def test_legacy_db_fails_check_then_migration_makes_it_clean(db):
    assert not ls.check()['ok']
    rep = mig.run(db)
    assert rep['check_before']['n'] >= 4 and rep['check_after']['ok']


def test_aliases(db):
    rep = mig.run(db)
    how = {(a['subject'], a['alias']): a['how'] for a in rep['aliases']}
    assert how[('nephthys', 'ts_ip')] == 'owner_kept_same_value'
    assert how[('nephthys', 'role_status')] == 'owner_kept_alias_value_dropped'
    assert how[('bes', 'hw')] == 'owner_kept_alias_value_dropped'
    assert how[('khepri', 'hw')] == 'value_moved_to_owner'
    k = q("SELECT * FROM state_facts WHERE subject='khepri' AND predicate='hardware'")[0]
    assert (k['value'], k['valid_from'], k['source_ref'], k['last_confirmed_at']) == \
        ('Mac Pro 6,1 64GB', D1, 'khepri-src', D1)          # evidence age carried, not reset
    old = q("SELECT * FROM state_facts WHERE id='k1'")[0]
    assert old['valid_to'] and old['closed_by'] == k['id']
    assert q("SELECT value FROM state_facts WHERE subject='bes' AND predicate='hardware' "
             "AND valid_to IS NULL")[0]['value'].startswith('Trigkey Green G4')


def test_lifecycle_backfill_closes_operational_and_skips_what_was_not_reviewed(db):
    rep = mig.run(db)
    done = {d['subject']: d for d in rep['lifecycle']}
    assert done['nephthys']['closed'] == ['dc_handle_store', 'embed_model', 'serving_model']
    assert done['leantime']['lifecycle'] == 'retired'
    skipped = {s['subject']: s['why'] for s in rep['lifecycle_skipped']}
    assert 'no longer starts with' in skipped['mem0']
    assert 'wadjet' not in done and 'lpp' not in done       # never in the plan
    lc = q("SELECT * FROM state_facts WHERE subject='nephthys' AND predicate='lifecycle'")[0]
    assert lc['valid_from'] == D2 and lc['last_confirmed_at'] == D2 and 'n5' in lc['source_ref']
    for fid in ('n1', 'n2', 'n3'):
        r = q('SELECT * FROM state_facts WHERE id=?', fid)[0]
        assert r['valid_to'] == D2 and r['closed_by'] == lc['id'] and r['source_ref'].startswith('orig-')
    assert q("SELECT valid_to FROM state_facts WHERE id='n4'")[0]['valid_to'] is None


def test_second_run_changes_nothing(db):
    mig.run(db)
    snap = q('SELECT * FROM state_facts ORDER BY id')
    rep = mig.run(db)
    assert rep['changes'] == 0 and rep['check_after']['ok']
    assert q('SELECT * FROM state_facts ORDER BY id') == snap


def test_dry_run_never_touches_the_db(db, capsys):
    before = q('SELECT * FROM state_facts ORDER BY id')
    rc = mig.main(['--db', db])
    assert rc == 0 and ls.DB == db          # module not left pointing at the deleted copy
    assert q('SELECT * FROM state_facts ORDER BY id') == before
    assert not q("SELECT 1 FROM state_facts WHERE predicate='lifecycle'")


def test_apply_writes_backup_first(db, tmp_path):
    bdir = tmp_path / 'bak'
    assert mig.main(['--db', db, '--apply', '--backup-dir', str(bdir)]) == 0
    baks = list(bdir.iterdir())
    assert len(baks) == 1
    c = sqlite3.connect(str(baks[0]))
    assert c.execute("SELECT valid_to FROM state_facts WHERE id='n1'").fetchone()[0] is None
    c.close()
