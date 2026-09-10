"""lumen_state / lumen_state_gen staleness rendering (wq#1612).

Display only: every assertion here is about what `current` prints and what the
LIVE_FLEET_STATE block contains. Nothing in the store is mutated by the code
under test, so the fixtures build a throwaway sqlite DB per test.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import lumen_state as ls  # noqa: E402
import lumen_state_gen as gen  # noqa: E402

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def ago(days):
    return (NOW - timedelta(days=days)).isoformat()


def fact(fact_class='identity', age=1.0, confirmed=None, **kw):
    """A current()-shaped row. `age` is the age of valid_from in days."""
    row = {'subject': 'thor', 'predicate': 'lan_ip', 'value': '10.0.0.1',
           'valid_from': ago(age), 'last_confirmed_at': confirmed,
           'fact_class': fact_class, 'render_tier': 'core'}
    row.update(kw)
    return row


# ── the ordinal classes (§3.6) ──────────────────────────────────────────────

@pytest.mark.parametrize('age,expected', [
    (10, ls.FRESH),    # identity default 90d -> r = 0.11
    (100, ls.DUE),     # r = 1.11
    (300, ls.STALE),   # r = 3.33
])
def test_identity_class_thresholds(age, expected):
    assert ls.staleness(fact(age=age), now=NOW)[0] == expected


def test_ratio_boundaries_are_closed_below():
    """r == 1.0 is DUE and r == 3.0 is STALE, per the review's `<` / `<=`."""
    assert ls.staleness(fact(age=90), now=NOW)[0] == ls.DUE
    assert ls.staleness(fact(age=270), now=NOW)[0] == ls.STALE


def test_class_defaults_differ_per_fact_class():
    """The same 20-day-old evidence reads differently per class (§3.3):
    status 3d, config 14d, identity 90d."""
    assert ls.staleness(fact('status', age=20), now=NOW)[0] == ls.STALE
    assert ls.staleness(fact('config', age=20), now=NOW)[0] == ls.DUE
    assert ls.staleness(fact('identity', age=20), now=NOW)[0] == ls.FRESH


def test_narrative_is_unverifiable_not_decayed():
    """Prose has no probe; that is a coverage gap, never an age judgement."""
    cls, _age, interval = ls.staleness(fact('narrative', age=400), now=NOW)
    assert cls == ls.UNVERIFIABLE
    assert interval is None


def test_explicit_interval_overrides_the_class_default():
    row = fact('identity', age=10, verify_interval_days=1.0)
    assert ls.staleness(row, now=NOW)[0] == ls.STALE


def test_reconfirmation_resets_the_evidence_clock():
    """last_confirmed_at is the anchor -- an old fact re-observed today is FRESH."""
    row = fact(age=300, confirmed=ago(2))
    assert ls.staleness(row, now=NOW)[0] == ls.FRESH
    assert ls.evidence_age_days(row, now=NOW) == pytest.approx(2.0, abs=0.01)


def test_contradicted_overrides_everything():
    row = fact(age=0, confirmed=ago(0), contradicted_at=ago(1))
    assert ls.staleness(row, now=NOW)[0] == ls.CONTRADICTED


def test_missing_verify_recipe_column_does_not_mean_unverifiable():
    """Before the verifier registry lands there is no verify_recipe column, and
    reading its absence as 'no probe' would label every fact UNVERIFIABLE."""
    assert ls.staleness(fact(age=1), now=NOW)[0] == ls.FRESH
    assert ls.staleness(fact(age=1, verify_recipe=None), now=NOW)[0] == ls.UNVERIFIABLE
    assert ls.staleness(fact(age=1, verify_recipe='fleet_inventory'), now=NOW)[0] == ls.FRESH


# ── the evidence column ─────────────────────────────────────────────────────

def test_evidence_labels_are_ordinal_and_carry_no_probability():
    labels = [
        ls.annotate(fact(age=2, confirmed=ago(2)), now=NOW)['evidence'],
        ls.annotate(fact('config', age=19), now=NOW)['evidence'],
        ls.annotate(fact('config', age=94), now=NOW)['evidence'],
        ls.annotate(fact('narrative'), now=NOW)['evidence'],
        ls.annotate(fact(contradicted_at=ago(1)), now=NOW)['evidence'],
    ]
    assert labels[0] == '[verified 2d ago]'
    assert labels[1] == '[DUE — 19d unverified]'
    assert labels[2] == '[STALE — 94d unverified]'
    assert labels[3] == '[UNVERIFIABLE — no probe]'
    assert labels[4].startswith('[⚠ CONTRADICTED')
    assert not any('%' in lab for lab in labels)


def test_due_label_names_the_probe_when_one_is_registered():
    row = ls.annotate(fact('config', age=19, verify_recipe='fleet_inventory'), now=NOW)
    assert row['evidence'] == '[DUE — 19d, probe fleet_inventory]'


def test_evidence_today_reads_as_today():
    assert ls.annotate(fact(age=0.1), now=NOW)['evidence'] == '[verified today]'


# ── current() over a real DB ────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(ls, 'DB', str(tmp_path / 'test.db'))
    ls.init_schema()
    return ls.DB


def backdate(days, subject=None):
    """Age a stored fact's evidence. ingest() anchors last_confirmed_at at write
    time (observing a fact now IS fresh evidence, whatever its valid_from), so
    old evidence has to be written directly."""
    import sqlite3
    c = sqlite3.connect(ls.DB)
    q = 'UPDATE state_facts SET last_confirmed_at=? WHERE valid_to IS NULL'
    p = [ago(days)]
    if subject:
        q += ' AND subject=?'
        p.append(subject)
    c.execute(q, p)
    c.commit()
    c.close()


def test_current_annotates_rows_and_survives_a_pre_migration_schema(db):
    """state_facts has no verify_recipe/contradicted_at column yet; current()
    must select only the columns that exist and still annotate every row."""
    ls.ingest('thor', 'lan_ip', '10.0.0.1')
    ls.ingest('thor', 'note', 'x' * 600, new_predicate='fixture: narrative-class row')
    backdate(400)
    rows = {r['predicate']: r for r in ls.current(now=NOW)}
    assert rows['lan_ip']['staleness'] == ls.STALE
    assert rows['lan_ip']['evidence'] == '[STALE — 400d unverified]'
    assert rows['note']['staleness'] == ls.UNVERIFIABLE   # narrative, >500 chars
    assert 'verify_recipe' not in rows['lan_ip']


def test_reingesting_the_same_value_refreshes_the_evidence_column(db):
    ls.ingest('thor', 'lan_ip', '10.0.0.1')
    backdate(400)
    assert ls.current(now=NOW)[0]['staleness'] == ls.STALE
    assert ls.ingest('thor', 'lan_ip', '10.0.0.1')['action'] == 'reconfirmed'
    assert ls.current(now=NOW)[0]['staleness'] == ls.FRESH


# ── the rendered block ──────────────────────────────────────────────────────

def block(rows, tier='core', **kw):
    return gen._block(rows, rows, tier, **kw)


def test_block_marks_due_and_stale_and_leaves_fresh_unmarked():
    rows = [ls.annotate(fact(age=1, predicate='a'), now=NOW),
            ls.annotate(fact('config', age=19, predicate='b'), now=NOW),
            ls.annotate(fact('config', age=94, predicate='c'), now=NOW),
            ls.annotate(fact(predicate='d', contradicted_at=ago(1)), now=NOW)]
    body = block(rows)
    assert '**a**=10.0.0.1,' in body      # FRESH: no mark, renders as before
    assert '**b**=10.0.0.1?' in body
    assert '**c**=10.0.0.1‼' in body
    assert '**d**=10.0.0.1‼' in body


def test_block_prints_a_coverage_line_and_a_legend():
    body = block([ls.annotate(fact(age=1), now=NOW)])
    assert 'fresh 1 · due 0 · stale 0 · unverifiable 0 · contradicted 0' in body
    assert 'Ordinal only, not a probability' in body


# ── core-budget eviction ────────────────────────────────────────────────────

def test_eviction_order_is_stalest_first_and_spares_fresh_and_contradicted():
    fresh = ls.annotate(fact(age=1, predicate='a'), now=NOW)
    due = ls.annotate(fact('config', age=19, predicate='b'), now=NOW)
    stale_old = ls.annotate(fact('config', age=400, predicate='c'), now=NOW)
    stale_new = ls.annotate(fact('config', age=94, predicate='d'), now=NOW)
    unver = ls.annotate(fact('narrative', predicate='e'), now=NOW)
    contra = ls.annotate(fact(predicate='f', contradicted_at=ago(1)), now=NOW)
    order = gen._evictable([fresh, due, stale_new, stale_old, unver, contra])
    assert [r['predicate'] for r in order] == ['c', 'd', 'e', 'b']


def test_render_evicts_until_the_core_block_fits(monkeypatch, db):
    for i in range(40):
        ls.ingest(f'node{i:02d}', 'lan_ip', f'10.0.0.{i}')
    backdate(400)
    for i in range(40, 45):
        ls.ingest(f'node{i:02d}', 'lan_ip', f'10.0.0.{i}')
    monkeypatch.setattr(gen, 'STATE_SOFT_CAP_TOK', 400)
    body = gen.render()
    assert gen._tok(body) <= gen.STATE_SOFT_CAP_TOK
    assert 'held out of this block' in body
    for i in range(40, 45):                       # every FRESH fact survived
        assert f'10.0.0.{i}' in body


def test_render_full_never_evicts(monkeypatch, db):
    for i in range(60):
        ls.ingest(f'node{i:02d}', 'lan_ip', f'10.0.0.{i}')
    backdate(400)
    monkeypatch.setattr(gen, 'STATE_SOFT_CAP_TOK', 100)
    body = gen.render(tier='full')
    assert 'held out of this block' not in body
    assert body.count('10.0.0.') == 60
