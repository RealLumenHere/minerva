"""wq#1700: one owner predicate per fact, lifecycle closure, predicate gate.

Throwaway sqlite DB per test; the registry is the repo's state_registry.json so
the tests exercise the shipped policy, not a fixture copy of it.
"""
import os
import sqlite3
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import lumen_state as ls  # noqa: E402

D1, D2, D3 = '2026-08-01T00:00:00+00:00', '2026-08-22T00:00:00+00:00', '2026-09-05T00:00:00+00:00'


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(ls, 'DB', str(tmp_path / 'test.db'))
    ls.init_schema()
    return ls.DB


def rows(sql, *p):
    c = sqlite3.connect(ls.DB)
    c.row_factory = sqlite3.Row
    out = [dict(r) for r in c.execute(sql, p).fetchall()]
    c.close()
    return out


def open_preds(subject):
    return {r['predicate']: r['value'] for r in ls.current(subject)}


# ── aliases ─────────────────────────────────────────────────────────────────

def test_alias_write_lands_on_owner_and_supersedes_it(db):
    ls.ingest('bes', 'tailscale_ip', '100.1.1.1', valid_from=D1)
    res = ls.ingest('bes', 'ts_ip', '100.2.2.2', valid_from=D2)
    assert res['action'] == 'superseded' and res['alias_of'] == 'tailscale_ip'
    assert open_preds('bes') == {'tailscale_ip': '100.2.2.2'}
    assert not rows("SELECT 1 FROM state_facts WHERE predicate='ts_ip'")


def test_every_alias_targets_a_non_alias(db):
    reg = ls.registry()
    for alias, owner in reg['aliases'].items():
        assert owner not in reg['aliases'], alias
        assert alias != owner


# ── predicate gate ──────────────────────────────────────────────────────────

def test_unknown_predicate_refused_with_nearest_name_and_nothing_written(db):
    ls.ingest('thoth', 'status', 'OFFLINE', valid_from=D1)
    with pytest.raises(ls.PredicateNotRegistered) as e:
        ls.ingest('thoth', 'statuss', 'FIXED', valid_from=D2)
    assert 'status' in str(e.value)
    assert len(rows('SELECT * FROM state_facts')) == 1
    assert not rows('SELECT * FROM state_predicate_additions')


def test_new_predicate_with_reason_is_recorded_then_accepted(db, monkeypatch):
    monkeypatch.setenv('WQ_AGENT', 'test-agent')
    res = ls.ingest('thoth', 'tailnet_routing', 'FIXED', new_predicate='separate axis: route table state')
    assert res['new_predicate'] == 'separate axis: route table state'
    add = rows('SELECT * FROM state_predicate_additions')
    assert add[0]['predicate'] == 'tailnet_routing' and add[0]['agent'] == 'test-agent'
    assert ls.ingest('seshat', 'tailnet_routing', 'OK')['action'] == 'insert'


def test_blank_reason_does_not_open_the_gate(db):
    with pytest.raises(ls.PredicateNotRegistered):
        ls.ingest('thoth', 'brand_new_axis', 'x', new_predicate='   ')


def test_predicate_already_in_store_is_accepted(db):
    c = sqlite3.connect(ls.DB)
    c.execute("INSERT INTO state_facts (id,subject,predicate,value,valid_from,ingested_at) "
              "VALUES ('legacy','x','legacy_pred','v',?,?)", (D1, D1))
    c.commit(); c.close()
    assert ls.ingest('y', 'legacy_pred', 'v')['action'] == 'insert'


@pytest.mark.parametrize('subject', ['lumen-persona', 'lpp-probe-persona'])
def test_open_namespace_subject_takes_any_predicate(db, subject):
    # lpp-probe-persona is what pokemon/src/probe_persona_state.ts writes to
    assert ls.ingest(subject, 'i_like__caves', 'caves')['action'] == 'insert'


def test_namespace_does_not_leak_to_other_subjects(db):
    ls.ingest('lumen-persona', 'i_like__caves', 'caves')
    # the predicate now has a row, so it is store vocabulary; a fresh one on a fleet subject is not
    with pytest.raises(ls.PredicateNotRegistered):
        ls.ingest('thor', 'i_like__mountains', 'x')


# ── lifecycle ───────────────────────────────────────────────────────────────

def test_lifecycle_is_a_closed_enum(db):
    with pytest.raises(ls.LifecycleValueError):
        ls.ingest('nephthys', 'lifecycle', 'DECOMMISSIONED 2026-08-22 (wq#589)')
    assert ls.ingest('nephthys', 'lifecycle', 'Offline')['value'] == 'offline'


def seed_nephthys():
    ls.ingest('nephthys', 'serving_model', 'qwen3:14b', valid_from=D1, source_ref='orig-a')
    ls.ingest('nephthys', 'mcp_failover', 'supergateway', valid_from=D1, source_ref='orig-b')
    ls.ingest('nephthys', 'ram', '64GB', valid_from=D1)
    ls.ingest('nephthys', 'status', 'DECOMMISSIONED', valid_from=D2)


def test_decommission_closes_operational_keeps_identity_and_provenance(db):
    seed_nephthys()
    res = ls.ingest('nephthys', 'lifecycle', 'decommissioned', valid_from=D2)
    assert res['closed'] == ['mcp_failover', 'serving_model']
    assert set(open_preds('nephthys')) == {'ram', 'status', 'lifecycle'}
    lc_id = rows("SELECT id FROM state_facts WHERE predicate='lifecycle'")[0]['id']
    for r in rows("SELECT * FROM state_facts WHERE predicate IN ('serving_model','mcp_failover')"):
        assert r['valid_to'] == D2 and r['closed_by'] == lc_id
        assert r['source_ref'] in ('orig-a', 'orig-b')
    assert ls.check()['ok']


def test_closure_never_ends_a_window_before_it_began(db):
    ls.ingest('nephthys', 'ollama_version', '0.32.6', valid_from=D3)
    ls.ingest('nephthys', 'lifecycle', 'retired', valid_from=D2)
    r = rows("SELECT valid_from, valid_to FROM state_facts WHERE predicate='ollama_version'")[0]
    assert r['valid_to'] >= r['valid_from']


def test_operational_write_after_decommission_is_refused(db):
    seed_nephthys()
    ls.ingest('nephthys', 'lifecycle', 'decommissioned', valid_from=D2)
    with pytest.raises(ls.LifecycleClosedError):
        ls.ingest('nephthys', 'embed_model', 'nomic', valid_from=D3)
    with pytest.raises(ls.LifecycleClosedError):
        ls.ingest('nephthys', 'dc_handle_store', 'LIVE', valid_from=D3)   # pattern dc_*
    assert ls.check()['ok']


def test_historical_operational_write_before_decommission_is_allowed(db):
    ls.ingest('nephthys', 'lifecycle', 'decommissioned', valid_from=D2)
    res = ls.ingest('nephthys', 'embed_model', 'nomic', valid_from=D1)
    assert res['action'] == 'historical_insert'
    r = rows("SELECT * FROM state_facts WHERE predicate='embed_model'")[0]
    assert r['valid_from'] == D1 and r['valid_to'] == D2
    assert ls.as_of('2026-08-10', 'nephthys', 'embed_model')[0]['value'] == 'nomic'
    assert 'embed_model' not in open_preds('nephthys')
    assert ls.check()['ok']


def test_recommission_reopens_the_door(db):
    seed_nephthys()
    ls.ingest('nephthys', 'lifecycle', 'decommissioned', valid_from=D2)
    ls.ingest('nephthys', 'lifecycle', 'active', valid_from=D3)
    assert ls.ingest('nephthys', 'serving_model', 'qwen3:32b')['action'] == 'insert'
    assert ls.check()['ok']


def test_non_operational_write_after_decommission_is_fine(db):
    ls.ingest('nephthys', 'lifecycle', 'decommissioned', valid_from=D2)
    assert ls.ingest('nephthys', 'ram', '64GB', valid_from=D3)['action'] == 'insert'


# ── behaviour that must not change ──────────────────────────────────────────

def test_supersession_records_closed_by(db):
    ls.ingest('thor', 'lan_ip', '10.0.0.1', valid_from=D1)
    ls.ingest('thor', 'lan_ip', '10.0.0.2', valid_from=D2)
    old, new = sorted(rows("SELECT * FROM state_facts WHERE predicate='lan_ip'"),
                      key=lambda r: r['valid_from'])
    assert old['valid_to'] == D2 and old['closed_by'] == new['id'] and new['valid_to'] is None


def test_reconfirm_and_out_of_order_guard_intact(db):
    ls.ingest('thor', 'lan_ip', '10.0.0.2', valid_from=D2)
    assert ls.ingest('thor', 'lan_ip', '10.0.0.2')['action'] == 'reconfirmed'
    res = ls.ingest('thor', 'lan_ip', '10.0.0.1', valid_from=D1)
    assert res['action'] == 'historical_insert'
    assert open_preds('thor') == {'lan_ip': '10.0.0.2'}


def test_refusal_on_caller_connection_leaves_caller_txn_alone(db):
    c = ls._conn()
    ls._migrate(c)
    c.execute("INSERT INTO state_facts (id,subject,predicate,value,valid_from,ingested_at) "
              "VALUES ('pending','z','status','x',?,?)", (D1, D1))
    with pytest.raises(ls.PredicateNotRegistered):
        ls.ingest('z', 'never_seen_axis', 'v', conn=c)
    c.commit(); c.close()
    assert rows("SELECT 1 FROM state_facts WHERE id='pending'")


# ── check(): each violation class is detected on a hand-corrupted DB ───────

def raw_insert(fid, subject, predicate, value):
    c = sqlite3.connect(ls.DB)
    c.execute("INSERT INTO state_facts (id,subject,predicate,value,valid_from,ingested_at) "
              "VALUES (?,?,?,?,?,?)", (fid, subject, predicate, value, D1, D1))
    c.commit(); c.close()


def test_check_detects_every_class(db):
    raw_insert('a', 'gameboy', 'ts_ip', '100.1.1.1')
    raw_insert('b', 'mirror', 'lifecycle', 'gone')
    raw_insert('c', 'pbox', 'lifecycle', 'decommissioned')
    raw_insert('d', 'pbox', 'serving_model', 'x')
    found = {v['class'] for v in ls.check()['violations']}
    assert found == {'open_alias_row', 'lifecycle_outside_enum',
                     'operational_under_closed_lifecycle'}
    # multiple_open_rows is prevented by idx_sf_one_open; prove the scan still sees it
    c = sqlite3.connect(ls.DB)
    c.execute('DROP INDEX idx_sf_one_open')
    c.commit(); c.close()
    raw_insert('e', 'thor', 'lan_ip', '1'); raw_insert('f', 'thor', 'lan_ip', '2')
    assert 'multiple_open_rows' in {v['class'] for v in ls.check()['violations']}


def test_cli_refusal_exits_2_and_check_exits_1(db, tmp_path):
    env = dict(os.environ)
    script = os.path.join(HERE, 'lumen_state.py')
    code = ("import sys,runpy; sys.argv=['lumen_state.py']+sys.argv[1:]; "
            f"import lumen_state as m; m.DB={ls.DB!r}; m._main()")
    run = lambda *a: subprocess.run([sys.executable, '-c', code, *a], cwd=HERE, env=env,
                                    capture_output=True, text=True)
    r = run('ingest', 'thoth', 'totally_new_axis', 'v')
    assert r.returncode == 2 and 'REFUSED' in r.stderr
    raw_insert('g', 'gameboy', 'ts_ip', '100.1.1.1')
    r = run('check')
    assert r.returncode == 1 and 'open_alias_row' in r.stdout
    assert os.path.exists(script)


def test_missing_registry_is_a_hard_error(db, monkeypatch, tmp_path):
    monkeypatch.setattr(ls, 'REGISTRY_PATH', str(tmp_path / 'nope.json'))
    with pytest.raises(FileNotFoundError):
        ls.ingest('thor', 'lan_ip', '10.0.0.9')
