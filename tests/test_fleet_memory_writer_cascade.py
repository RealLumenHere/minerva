"""wq#4802: the wq#4795 fleet_facts cascade must not take a lifecycle closure
down with it on a DB that has no fleet_facts table.

fleet_facts is not created by lumen_state.init_schema() -- it was populated
once by the wq#4554 Postgres copy -- so a throwaway fixture DB, and any
minerva.db rebuilt before that copy runs, has none. The cascade runs inside
lumen_state's OWN transaction, so an uncaught `no such table` there rolled the
whole lifecycle write back (wq#4799 verify-pass: 12 failures).

Both halves are asserted here: no-op when the table is absent, and the real
cascade still firing (and still word-boundary-matching) when it is present,
against the live fleet_facts DDL copied into the fixture.
"""
import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import fleet_memory_writer as fmw  # noqa: E402
import lumen_state as ls  # noqa: E402

D1, D2 = '2026-08-01T00:00:00+00:00', '2026-08-22T00:00:00+00:00'

# minerva.db's real fleet_facts/fleet_decisions DDL (sqlite_master, 2026-10-31).
FLEET_DDL = '''
CREATE TABLE fleet_facts (
    id          INTEGER PRIMARY KEY,
    subject     TEXT NOT NULL,
    attribute   TEXT NOT NULL,
    value       TEXT,
    source      TEXT,
    status      TEXT NOT NULL,
    expires_at  TEXT
);
'''


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A DB exactly as init_schema() leaves it: state_facts, no fleet_facts."""
    monkeypatch.setattr(ls, 'DB', str(tmp_path / 'test.db'))
    monkeypatch.delenv('WQ_AGENT', raising=False)
    ls.init_schema()
    return ls.DB


def add_fleet_facts(rows):
    c = sqlite3.connect(ls.DB)
    c.executescript(FLEET_DDL)
    c.executemany(
        'INSERT INTO fleet_facts (id, subject, attribute, value, source, status)'
        ' VALUES (?,?,?,?,?,?)', rows)
    c.commit()
    c.close()


def fleet_rows():
    c = sqlite3.connect(ls.DB)
    c.row_factory = sqlite3.Row
    out = {r['id']: dict(r) for r in c.execute('SELECT * FROM fleet_facts')}
    c.close()
    return out


def test_table_exists_sees_both_cases(db):
    c = sqlite3.connect(ls.DB)
    assert fmw._table_exists(c, 'fleet_facts') is False
    assert fmw._table_exists(c, 'state_facts') is True
    c.close()


def test_cascade_no_ops_when_fleet_facts_absent(db):
    """Pre-fix this raised sqlite3.OperationalError: no such table."""
    c = sqlite3.connect(ls.DB)
    try:
        assert fmw.expire_facts_naming_subject(c, 'nephthys', 'wq#4802') == []
    finally:
        c.close()


def test_lifecycle_ingest_keeps_its_closure_on_a_db_without_fleet_facts(db):
    """The break wq#4799 found: the cascade's crash rolled back the whole
    lifecycle write, not just the cascade."""
    ls.ingest('nephthys', 'serving_model', 'qwen3:8b', valid_from=D1,
              source_ref='orig-a')
    res = ls.ingest('nephthys', 'lifecycle', 'decommissioned', valid_from=D2)
    assert res['closed'] == ['serving_model']
    c = sqlite3.connect(ls.DB)
    c.row_factory = sqlite3.Row
    lifecycle = [dict(r) for r in c.execute(
        "SELECT * FROM state_facts WHERE subject='nephthys'"
        " AND predicate='lifecycle'")]
    closed = [dict(r) for r in c.execute(
        "SELECT * FROM state_facts WHERE predicate='serving_model'")]
    c.close()
    assert len(lifecycle) == 1 and lifecycle[0]['value'] == 'decommissioned'
    assert closed[0]['valid_to'] == D2
    assert ls.check()['ok']


def test_cascade_still_expires_matching_rows_when_the_table_is_present(db):
    """The guard must not disable the cascade it guards: with fleet_facts
    there, the wq#4795 word-boundary behaviour is unchanged."""
    add_fleet_facts([
        (1, 'node:nephthys', 'role', 'inference box', 'pg', 'verified'),
        (2, 'misc:commander-ha-pair-nephthys-khnum', 'note',
         'pair notes', 'pg', 'verified'),
        (3, 'node:horus', 'role', 'runs nephthys failover', 'pg', 'verified'),
        (4, 'node:horus', 'role', 'unrelated', 'pg', 'verified'),
        (5, 'node:nephthysaurus', 'role', 'substring, not a word', 'pg',
         'verified'),
        (6, 'node:nephthys', 'old_role', 'already done', 'pg', 'expired'),
    ])
    c = sqlite3.connect(ls.DB)
    try:
        expired = fmw.expire_facts_naming_subject(c, 'nephthys', 'wq#4802')
        c.commit()
    finally:
        c.close()
    assert expired == [1, 2, 3]
    after = fleet_rows()
    assert [after[i]['status'] for i in (1, 2, 3)] == ['expired'] * 3
    assert all(after[i]['expires_at'] for i in (1, 2, 3))
    assert after[4]['status'] == 'verified' and after[5]['status'] == 'verified'
    assert after[6]['status'] == 'expired' and after[6]['expires_at'] is None


def test_lifecycle_ingest_cascades_when_the_table_is_present(db):
    add_fleet_facts([
        (1, 'node:nephthys', 'role', 'inference box', 'pg', 'verified'),
        (2, 'node:horus', 'role', 'unrelated', 'pg', 'verified'),
    ])
    ls.ingest('nephthys', 'lifecycle', 'retired', valid_from=D2)
    after = fleet_rows()
    assert after[1]['status'] == 'expired'
    assert after[2]['status'] == 'verified'
    c = sqlite3.connect(ls.DB)
    audit = c.execute(
        "SELECT COUNT(*) FROM memory_audit WHERE table_name='fleet_facts'"
        " AND row_id='1'").fetchone()[0]
    c.close()
    assert audit == 1
