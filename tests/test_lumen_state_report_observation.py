"""wq#1608: report_observation() / ingest(probe=...) reconciliation contract.

MINERVA_STALENESS_REVIEW.md §3.2: a reconciler reports (subject, predicate,
observed, probe) and NEVER compares; ingest() decides confirmed / superseded /
unknown, bumps last_confirmed_at, clears contradicted_at, and appends an
append-only fact_verifications row. §3.4: frank_stated is never auto-superseded.

Throwaway sqlite DB per test, same fixture pattern as
test_lumen_state_single_owner.py.
"""
import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import lumen_state as ls  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(ls, 'DB', str(tmp_path / 'test.db'))
    ls.init_schema()
    return ls.DB


def verifications(fact_id=None):
    c = sqlite3.connect(ls.DB)
    c.row_factory = sqlite3.Row
    sql = 'SELECT * FROM fact_verifications'
    params = ()
    if fact_id is not None:
        sql += ' WHERE fact_id=?'
        params = (fact_id,)
    out = [dict(r) for r in c.execute(sql, params).fetchall()]
    c.close()
    return out


def open_row(subject, predicate):
    c = sqlite3.connect(ls.DB)
    c.row_factory = sqlite3.Row
    r = c.execute('SELECT * FROM state_facts WHERE subject=? AND predicate=? '
                  'AND valid_to IS NULL', (subject, predicate)).fetchone()
    c.close()
    return dict(r) if r else None


def test_confirm_bumps_evidence_and_logs_verification(db):
    ls.ingest('thoth', 'tailscale_ip', '100.1.1.1')
    fid = open_row('thoth', 'tailscale_ip')['id']
    res = ls.report_observation('thoth', 'tailscale_ip', '100.1.1.1', 'tailscale_ip')
    assert res['outcome'] == 'confirmed'
    row = open_row('thoth', 'tailscale_ip')
    assert row['id'] == fid  # same row, no new fact
    assert row['last_confirmed_at']
    v = verifications(fid)
    assert len(v) == 1 and v[0]['outcome'] == 'confirmed' and v[0]['probe'] == 'tailscale_ip'


def test_confirm_clears_a_prior_contradiction(db):
    ls.ingest('thoth', 'tailscale_ip', '100.1.1.1')
    fid = open_row('thoth', 'tailscale_ip')['id']
    c = sqlite3.connect(ls.DB)
    ls._migrate_verify(c)  # column is lazy -- not present until a probe call
    c.execute("UPDATE state_facts SET contradicted_at=? WHERE id=?", ('2026-09-01', fid))
    c.commit(); c.close()
    ls.report_observation('thoth', 'tailscale_ip', '100.1.1.1', 'tailscale_ip')
    assert open_row('thoth', 'tailscale_ip')['contradicted_at'] is None


def test_mismatch_supersedes_through_real_ingest(db):
    ls.ingest('thoth', 'tailscale_ip', '100.1.1.1', source_kind='observed')
    old_fid = open_row('thoth', 'tailscale_ip')['id']
    res = ls.report_observation('thoth', 'tailscale_ip', '100.1.1.2', 'tailscale_ip')
    assert res['outcome'] == 'superseded'
    new = open_row('thoth', 'tailscale_ip')
    assert new['value'] == '100.1.1.2'
    assert new['id'] != old_fid
    v = verifications(old_fid)
    assert len(v) == 1 and v[0]['outcome'] == 'superseded' and v[0]['observed'] == '100.1.1.2'


def test_frank_stated_conflict_never_mutates(db):
    ls.ingest('thoth', 'tailscale_ip', '100.1.1.1', stated_by_frank=True)
    fid = open_row('thoth', 'tailscale_ip')['id']
    res = ls.report_observation('thoth', 'tailscale_ip', '100.1.1.99', 'tailscale_ip')
    assert res['outcome'] == 'unknown'
    assert res.get('conflict') is True
    row = open_row('thoth', 'tailscale_ip')
    assert row['id'] == fid
    assert row['value'] == '100.1.1.1'  # untouched
    v = verifications(fid)
    assert len(v) == 1 and v[0]['outcome'] == 'unknown' and v[0]['observed'] == '100.1.1.99'


def test_no_observation_is_unknown_and_writes_nothing(db):
    ls.ingest('thoth', 'tailscale_ip', '100.1.1.1')
    fid = open_row('thoth', 'tailscale_ip')['id']
    res = ls.report_observation('thoth', 'tailscale_ip', None, 'tailscale_ip')
    assert res['outcome'] == 'unknown'
    assert verifications(fid) == []
    assert open_row('thoth', 'tailscale_ip')['value'] == '100.1.1.1'


def test_no_open_fact_is_unknown_and_writes_nothing(db):
    res = ls.report_observation('nobody', 'tailscale_ip', '100.1.1.1', 'tailscale_ip')
    assert res['outcome'] == 'unknown'
    assert verifications() == []


def test_plain_ingest_without_probe_never_touches_verify_schema(db):
    """A non-probe caller (the overwhelming majority of existing call sites)
    must see zero behavioural change: no fact_verifications table forced into
    existence, no contradicted_at write attempted."""
    ls.ingest('thoth', 'tailscale_ip', '100.1.1.1')
    ls.ingest('thoth', 'tailscale_ip', '100.1.1.1')  # reconfirm path, no probe
    c = sqlite3.connect(ls.DB)
    exists = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                       "AND name='fact_verifications'").fetchone()
    c.close()
    assert exists is None
