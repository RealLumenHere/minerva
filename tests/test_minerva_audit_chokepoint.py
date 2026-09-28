"""wq#1634: every write through the chokepoint is answerable by SELECT on memory_audit.

MINERVA_PIPELINE_MAP.md §4.5. Throwaway sqlite DB per test, the repo's real
state_registry.json, and no network -- the memoryd probe is monkeypatched, because
the property under test is "the fail-open path keeps the trail", which only shows up
when the daemon is NOT there.
"""
import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import lumen_state as ls  # noqa: E402
import minerva_audit as ma  # noqa: E402

D1, D2, D3 = '2026-08-01T00:00:00+00:00', '2026-08-22T00:00:00+00:00', '2026-09-05T00:00:00+00:00'


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(ls, 'DB', str(tmp_path / 'test.db'))
    monkeypatch.delenv('WQ_AGENT', raising=False)
    ls.init_schema()
    return ls.DB


def audit_rows(where='1=1', *p):
    c = sqlite3.connect(ls.DB)
    c.row_factory = sqlite3.Row
    out = [dict(r) for r in c.execute(
        'SELECT * FROM memory_audit WHERE %s ORDER BY seq' % where, p).fetchall()]
    c.close()
    return out


def facts(sql='1=1', *p):
    c = sqlite3.connect(ls.DB)
    c.row_factory = sqlite3.Row
    out = [dict(r) for r in c.execute(
        'SELECT * FROM state_facts WHERE %s' % sql, p).fetchall()]
    c.close()
    return out


# ── the acceptance criterion ────────────────────────────────────────────────

def test_every_ingest_action_leaves_exactly_one_answerable_audit_row(db):
    ls.ingest('bes', 'tailscale_ip', '100.1.1.1', valid_from=D1, source_ref='wq#1634')
    ls.ingest('bes', 'tailscale_ip', '100.1.1.1', valid_from=D1, source_ref='wq#1634')
    ls.ingest('bes', 'tailscale_ip', '100.2.2.2', valid_from=D2, source_ref='wq#1634')
    rows = audit_rows()
    assert [r['action'] for r in rows] == ['write', 'reconfirm', 'supersede']
    assert {r['table_name'] for r in rows} == {'state_facts'}
    assert {r['source_ref'] for r in rows} == {'wq#1634'}
    # every audit row points at a state_facts row that actually exists
    ids = {f['id'] for f in facts()}
    assert all(r['row_id'] in ids for r in rows)


def test_audit_row_names_the_agent_that_wrote_the_fact(db, monkeypatch):
    monkeypatch.setenv('WQ_AGENT', 'wq1634-a1')
    ls.ingest('thoth', 'status', 'up', valid_from=D1, source_ref='wq#1634')
    row = audit_rows()[-1]
    assert row['agent_id'] == 'wq1634-a1'
    assert 'source_kind=agent_written' in row['detail']


def test_no_agent_is_recorded_as_unattributed_not_guessed(db):
    ls.ingest('thoth', 'status', 'up', valid_from=D1)
    row = audit_rows()[-1]
    assert row['agent_id'] is None
    assert 'source_kind=unattributed' in row['detail']
    assert facts("subject='thoth'")[0]['source_kind'] == 'unattributed'


# ── source_kind (wq#1607 §3.4) ──────────────────────────────────────────────

def test_stated_by_frank_is_stamped_at_write_time_not_inferred(db, monkeypatch):
    monkeypatch.setenv('WQ_AGENT', 'wq1634-a1')
    ls.ingest('bes', 'location', 'the office closet', valid_from=D1,
              source_ref='wq#1634', stated_by_frank=True)
    # the flag beats $WQ_AGENT: only a human can assert frank_stated, and wq#1613
    # refuses to supersede such a fact on a probe mismatch
    assert facts("subject='bes'")[0]['source_kind'] == ma.FRANK_STATED
    assert 'source_kind=frank_stated' in audit_rows()[-1]['detail']


def test_probe_writes_declare_observed(db):
    ls.ingest('bes', 'lan_ip', '192.168.1.9', valid_from=D1, source_kind='observed')
    assert facts("subject='bes'")[0]['source_kind'] == ma.OBSERVED


def test_unknown_source_kind_is_refused_with_nothing_written(db):
    with pytest.raises(ValueError):
        ls.ingest('bes', 'lan_ip', '192.168.1.9', valid_from=D1, source_kind='vibes')
    assert facts() == [] and audit_rows() == []


# ── atomicity: no accepted write without its audit row ──────────────────────

def test_a_refused_write_leaves_no_audit_row(db):
    with pytest.raises(ls.PredicateNotRegistered):
        ls.ingest('bes', 'flux_capacitance', '1.21GW', valid_from=D1)
    assert audit_rows() == []


def test_audit_row_and_fact_land_in_one_transaction(db, monkeypatch):
    """The audit row is written on the ingest connection BEFORE commit, so an audit
    failure takes the fact down with it: an accepted write with no audit row is
    impossible by construction (kernel.py:277 gives lane writes the same)."""
    def boom(*a, **k):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(ma, 'audit', boom)
    with pytest.raises(sqlite3.OperationalError):
        ls.ingest('bes', 'tailscale_ip', '100.1.1.1', valid_from=D1)
    assert facts() == [] and audit_rows() == []


def test_memory_audit_is_append_only(db):
    ls.ingest('bes', 'tailscale_ip', '100.1.1.1', valid_from=D1)
    c = sqlite3.connect(ls.DB)
    for sql in ("UPDATE memory_audit SET action='write' WHERE seq=1",
                'DELETE FROM memory_audit WHERE seq=1'):
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(sql)
    c.close()


# ── lifecycle closure: each closed sibling is its own write ─────────────────

def test_lifecycle_closure_audits_every_row_it_closed(db):
    ls.ingest('nephthys', 'serving_model', 'qwen3:14b', valid_from=D1)
    ls.ingest('nephthys', 'mcp_failover', 'supergateway', valid_from=D1)
    ls.ingest('nephthys', 'ram', '64GB', valid_from=D1)
    before = len(audit_rows())
    ls.ingest('nephthys', 'lifecycle', 'decommissioned', valid_from=D2,
              source_ref='wq#1634')
    new = audit_rows()[before:]
    lc_id = facts("predicate='lifecycle'")[0]['id']
    assert [r['action'] for r in new] == ['write', 'supersede', 'supersede']
    assert new[0]['row_id'] == lc_id
    # the identity fact (ram) stayed open, so it has no closure row
    closed = {r['row_id'] for r in new[1:]}
    assert closed == {f['id'] for f in facts("closed_by=?", lc_id)}
    assert all('closed by lifecycle decommissioned' in r['detail'] for r in new[1:])


def test_historical_insert_is_audited(db):
    ls.ingest('nephthys', 'lifecycle', 'decommissioned', valid_from=D2)
    ls.ingest('nephthys', 'serving_model', 'qwen3:14b', valid_from=D1)
    row = audit_rows()[-1]
    assert row['action'] == 'write' and 'historical:' in row['detail']


# ── lane is read from the ticket, never from the writer ─────────────────────

def test_lane_is_read_from_the_ticket_named_by_source_ref(db, tmp_path, monkeypatch):
    q = tmp_path / 'queue.db'
    c = sqlite3.connect(str(q))
    c.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY, lane TEXT)')
    c.execute("INSERT INTO tasks VALUES ('1634','build')")
    c.commit(); c.close()
    monkeypatch.setattr(ma, 'QUEUE_DB', str(q))
    ls.ingest('bes', 'tailscale_ip', '100.1.1.1', valid_from=D1, source_ref='wq#1634')
    assert audit_rows()[-1]['lane'] == 'build'


def test_free_text_source_ref_leaves_lane_null_rather_than_guessing(db):
    ls.ingest('bes', 'tailscale_ip', '100.1.1.1', valid_from=D1,
              source_ref='frank said so in chat')
    assert audit_rows()[-1]['lane'] is None


def test_unreachable_queue_db_does_not_fail_the_ingest(db, monkeypatch):
    monkeypatch.setattr(ma, 'QUEUE_DB', '/nonexistent/queue.db')
    ls.ingest('bes', 'tailscale_ip', '100.1.1.1', valid_from=D1, source_ref='wq#1634')
    assert audit_rows()[-1]['lane'] is None
    assert len(facts()) == 1


# ── CLI episode commit (§1.4): memoryd first, audited fallback second ───────

@pytest.fixture
def episode(db, monkeypatch):
    """lumen_episode with its embedding/FTS/ledger satellites stubbed: this suite
    tests the write path and its audit row, not the recall stack."""
    import minerva_memory as mm
    import lumen_episode as le
    monkeypatch.setattr(mm, 'DB', ls.DB)
    monkeypatch.setattr(mm, 'ARCHIVAL_DIR', __import__('pathlib').Path(ls.DB).parent / 'archival')
    monkeypatch.setattr(mm, 'embed', lambda *a, **k: None)
    monkeypatch.setattr(mm, 'log', lambda *a, **k: None)
    monkeypatch.setattr(le.mm, 'log_event', lambda *a, **k: None)
    c = sqlite3.connect(ls.DB)
    c.execute('''CREATE TABLE archival_notes (id TEXT PRIMARY KEY, kind TEXT, title TEXT,
                 content TEXT, embedding BLOB, tags TEXT, source_ref TEXT,
                 created_at TEXT, origin TEXT, trust TEXT)''')
    c.commit(); c.close()
    return le


def test_cli_commit_falls_back_to_a_direct_write_that_is_still_audited(episode):
    nid = episode.commit('audit chokepoint landed', changed=['lumen_state.py'],
                         session_ref='wq#1634')
    rows = audit_rows("table_name='archival_notes'")
    assert len(rows) == 1
    assert rows[0]['row_id'] == nid and rows[0]['action'] == 'write'
    assert 'fallback' in rows[0]['detail']
    assert rows[0]['invariant'] == 'wq1634-fail-open'


def test_cli_commit_says_why_it_fell_back(episode, monkeypatch):
    monkeypatch.setenv('WQ_AGENT', 'wq1634-a1')
    monkeypatch.setattr(episode, 'MEMORYD_URL', 'http://127.0.0.1:1')
    episode.commit('daemon down', session_ref='wq#1634')
    assert 'fallback (' in audit_rows("table_name='archival_notes'")[0]['detail']


def test_cli_commit_defers_to_memoryd_when_it_accepts(episode, monkeypatch):
    """A daemon-accepted commit writes NO second audit row here: the kernel already
    wrote one inside its own transaction, and a duplicate would double-count the
    write in exactly the query this ticket exists to make trustworthy."""
    monkeypatch.setattr(episode, '_memoryd_commit',
                        lambda *a, **k: ('deadbeefcafe', None))
    # wq#2772: the daemon row is faked here, so fake its read-back too; the
    # real read-back on this path is covered by test_wq2772_* below.
    monkeypatch.setattr(episode.mm, 'verify_landed', lambda *a, **k: [])
    nid = episode.commit('lane commit', session_ref='wq#1634')
    assert nid == 'deadbeefcafe'
    assert audit_rows("table_name='archival_notes'") == []


def test_frank_stated_survives_the_fallback_path(episode):
    episode.commit('frank said the mac is retired', session_ref='wq#1634',
                   stated_by_frank=True)
    assert 'source_kind=frank_stated' in audit_rows("table_name='archival_notes'")[0]['detail']


def test_existing_write_note_callers_are_unaudited_and_unchanged(episode):
    """The audit hook is opt-in: `audit=None` is the default, so convo-learn,
    continual-learn and seed() keep writing exactly as before."""
    import minerva_memory as mm
    nid = mm.write_note('session', 'legacy caller', 'body', source_ref='x')
    assert nid and audit_rows("table_name='archival_notes'") == []


# ---------------- wq#2772: commit is DB-first, read-back verified, fail-loud ----------------

def _q(sql, *a):
    c = sqlite3.connect(ls.DB)
    try:
        return c.execute(sql, a).fetchall()
    finally:
        c.close()


def test_wq2772_commit_lands_row_and_fts_event_before_md(episode):
    import minerva_memory as mm
    nid = episode.commit('BLUEFOX regression probe', changed=['phone'], session_ref='wq#2772')
    assert _q('SELECT id FROM archival_notes WHERE id=?', nid) == [(nid,)]
    assert _q("SELECT ref FROM recall_fts WHERE recall_fts MATCH 'BLUEFOX'") == [('wq#2772',)]
    assert list(mm.ARCHIVAL_DIR.glob(f'*_{nid}_session.md'))
    assert not list(mm.ARCHIVAL_DIR.glob('*.tmp.*'))


def test_wq2772_embed_death_cannot_orphan_the_note(episode, monkeypatch):
    """The exact incident: the caller's timeout kills us inside embed(). Row and
    FTS event must already be committed; only the vector is lost (NULL = repair)."""
    import minerva_memory as mm
    def die(*a, **k):
        raise KeyboardInterrupt('simulated SIGKILL mid-embed')
    monkeypatch.setattr(mm, 'embed', die)
    with pytest.raises(KeyboardInterrupt):
        episode.commit('killed mid embed', session_ref='wq#2772')
    rows = _q("SELECT id, embedding FROM archival_notes WHERE title='killed mid embed'")
    assert len(rows) == 1 and rows[0][1] is None
    assert _q("SELECT count(*) FROM recall_fts WHERE recall_fts MATCH 'killed'") == [(1,)]


def test_wq2772_missing_row_raises_and_writes_no_md(episode, monkeypatch):
    import minerva_memory as mm
    monkeypatch.setattr(mm, 'verify_landed', lambda *a, **k: ['no archival_notes row'])
    with pytest.raises(mm.NotPersisted):
        episode.commit('ghost write', session_ref='wq#2772')
    assert not list(mm.ARCHIVAL_DIR.glob('*.md'))


def test_wq2772_memoryd_accept_with_no_row_is_not_success(episode, monkeypatch):
    import minerva_memory as mm
    monkeypatch.setattr(episode, '_memoryd_commit', lambda *a, **k: ('deadbeefcafe', None))
    with pytest.raises(mm.NotPersisted, match='no archival_notes row'):
        episode.commit('daemon lied', session_ref='wq#2772')


def test_wq2772_repeat_commit_keeps_provenance_and_embedding(episode, monkeypatch):
    import numpy as np
    import minerva_memory as mm
    monkeypatch.setattr(mm, 'embed', lambda *a, **k: np.ones(768, dtype=np.float32))
    n1 = episode.commit('dup commit', session_ref='wq#2772')
    ca1, emb1 = _q('SELECT created_at, embedding FROM archival_notes WHERE id=?', n1)[0]
    monkeypatch.setattr(mm, 'embed', lambda *a, **k: None)   # nomic down on the retry
    n2 = episode.commit('dup commit', session_ref='wq#2772')
    ca2, emb2 = _q('SELECT created_at, embedding FROM archival_notes WHERE id=?', n2)[0]
    assert n1 == n2 and ca1 == ca2 and emb2 == emb1 and emb2 is not None
