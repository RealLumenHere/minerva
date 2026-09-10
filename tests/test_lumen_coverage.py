"""lumen_coverage (wq#1611) — unverifiable is a reported, alarmed quantity.

Temp sqlite only, never minerva.db; the aq CLI is a stub that records its argv.
"""
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import lumen_coverage as lc  # noqa: E402
import lumen_state  # noqa: E402

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def _iso(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat()


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db")
    c.row_factory = sqlite3.Row
    lumen_state.init_schema(c)
    yield c
    c.close()


def _fact(conn, subject, predicate, value, fact_class="identity", age=0, confirmed=None):
    conn.execute(
        "INSERT INTO state_facts (id, subject, predicate, value, valid_from, ingested_at,"
        " confidence, fact_class, last_confirmed_at) VALUES (?,?,?,?,?,?,1.0,?,?)",
        (f"{subject}-{predicate}", subject, predicate, value, _iso(age), _iso(age),
         fact_class, _iso(confirmed) if confirmed is not None else None))
    conn.commit()


def _row(conn):
    return conn.execute("SELECT * FROM state_facts LIMIT 1").fetchone()


# ── §3.6 staleness buckets ───────────────────────────────────────────────────

def test_the_buckets_are_lumen_state_s_buckets(conn):
    """One §3.6 classifier: this line and the LIVE_FLEET_STATE block must not
    disagree about the same fact."""
    _fact(conn, "thoth", "lan_ip", "192.168.8.5", age=300)
    row = dict(_row(conn))
    assert lc.classify(row, NOW) == lumen_state.staleness(row, NOW)[0].lower()
    assert lc.classify(row, NOW) == "stale"


def test_a_class_no_probe_owns_is_unverifiable_not_fresh(conn):
    _fact(conn, "hub", "notes", "long prose", fact_class="narrative", age=1)
    assert lc.classify(_row(conn), NOW) == "unverifiable"


@pytest.mark.parametrize("age,bucket", [(5, "fresh"), (100, "due"), (300, "stale")])
def test_ratio_thresholds(conn, age, bucket):
    _fact(conn, "apis", "lan_ip", "192.168.8.6", age=age)   # identity: 90d TTL
    assert lc.classify(_row(conn), NOW) == bucket


def test_last_confirmed_at_is_the_evidence_anchor(conn):
    """Re-confirmation resets the clock; valid_from (world truth) does not move."""
    _fact(conn, "apis", "lan_ip", "192.168.8.6", age=300, confirmed=1)
    assert lc.classify(_row(conn), NOW) == "fresh"


def test_contradicted_overrides_everything(conn):
    conn.execute("ALTER TABLE state_facts ADD COLUMN contradicted_at TEXT")
    _fact(conn, "pbox", "status", "active", age=1)
    conn.execute("UPDATE state_facts SET contradicted_at=?", (_iso(0),))
    assert lc.classify(_row(conn), NOW) == "contradicted"


def test_a_null_verify_recipe_is_unverifiable_once_the_column_lands(conn):
    conn.execute("ALTER TABLE state_facts ADD COLUMN verify_recipe TEXT")
    _fact(conn, "hub", "lan_ip", "192.168.8.1", age=1)
    assert lc.classify(_row(conn), NOW) == "unverifiable"
    conn.execute("UPDATE state_facts SET verify_recipe='fleet_inventory'")
    assert lc.classify(_row(conn), NOW) == "fresh"


def test_closed_facts_are_not_counted(conn):
    _fact(conn, "pbox", "status", "active", age=1)
    conn.execute("UPDATE state_facts SET valid_to=?", (_iso(0),))
    assert lc.fact_coverage(conn, NOW)["facts"] == 0


def test_fact_line_is_the_handoff_one_liner(conn):
    _fact(conn, "apis", "lan_ip", "192.168.8.6", age=5)
    _fact(conn, "hub", "notes", "long prose", fact_class="narrative", age=1)
    assert lc.fact_line(lc.fact_coverage(conn, NOW)) == (
        "facts 2 · fresh 1 · due 0 · stale 0 · unverifiable 1 · contradicted 0")


# ── per-run reconciler coverage ──────────────────────────────────────────────

def test_coverage_line_and_clean():
    cov = lc.Coverage("state_reconcile:tailscale_ip")
    cov.count("matched")
    assert cov.clean()
    cov.count("unverifiable", "no-fact")
    assert not cov.clean()
    assert cov.line() == ("coverage[state_reconcile:tailscale_ip]: checked 2 · matched 1"
                          " · drifted 0 · unverifiable 1 (no-fact 1)")


def test_nothing_checked_is_never_clean():
    assert not lc.Coverage("empty").clean()


def test_unknown_verdict_is_a_programming_error():
    with pytest.raises(ValueError):
        lc.Coverage("x").count("MATCH")


# ── week-over-week regression ────────────────────────────────────────────────

@pytest.fixture
def aq(tmp_path, monkeypatch):
    log = tmp_path / "aq.log"
    stub = tmp_path / "aq"
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$0.log"\n')
    stub.chmod(0o755)
    monkeypatch.setattr(lc, "AQ", str(stub))
    return log


def _unverifiable(conn, subject):
    _fact(conn, subject, "notes", "long prose", fact_class="narrative", age=1)


def test_first_run_records_a_baseline_and_does_not_alarm(conn, aq):
    _unverifiable(conn, "thoth")
    counts, prev, delta = lc.check(conn, NOW, raise_aq=True)
    assert (prev, delta) == (None, None)
    assert counts["unverifiable"] == 1
    assert not aq.exists()


def test_growth_week_over_week_raises_one_aq_row(conn, aq):
    _unverifiable(conn, "thoth")
    lc.check(conn, NOW - timedelta(days=7), raise_aq=True)
    _unverifiable(conn, "apis")
    counts, prev, delta = lc.check(conn, NOW, raise_aq=True)
    assert delta == 1 and lc.unverified(prev) == 1 and lc.unverified(counts) == 2
    args = aq.read_text().splitlines()
    assert args[:2] == ["add", "minerva"]
    assert args[2] == "fact-coverage-2026-W37"
    assert "+1 week over week" in args[3]


def test_flat_or_improving_coverage_does_not_alarm(conn, aq):
    _unverifiable(conn, "thoth")
    _unverifiable(conn, "apis")
    lc.check(conn, NOW - timedelta(days=7), raise_aq=True)
    conn.execute("UPDATE state_facts SET fact_class='identity' WHERE subject='apis'")
    counts, prev, delta = lc.check(conn, NOW, raise_aq=True)
    assert delta == -1
    assert not aq.exists()


def test_a_same_day_rerun_is_not_last_week(conn, aq):
    """Two runs an hour apart must not compare against each other."""
    _unverifiable(conn, "thoth")
    lc.check(conn, NOW - timedelta(hours=1), raise_aq=True)
    _unverifiable(conn, "apis")
    _, prev, delta = lc.check(conn, NOW, raise_aq=True)
    assert (prev, delta) == (None, None)
    assert not aq.exists()


def test_cli_check_exits_3_on_regression(conn, tmp_path, aq, monkeypatch):
    _unverifiable(conn, "thoth")
    lc.check(conn, NOW - timedelta(days=7))
    _unverifiable(conn, "apis")
    monkeypatch.setattr(lc, "DB", str(tmp_path / "t.db"))
    assert lc.main(["line"]) == 0
    assert lc.main(["check"]) == 3
    assert lc.main(["facts", "--json"]) == 0
