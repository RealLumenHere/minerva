"""wq#2895 follow-up: lumen_scout_expanded.insert_intel must not leave a
transaction open when an INSERT/COMMIT goes busy.

2026-09-28 06:01Z: an INSERT hit 'database is locked', the exception was swallowed
without rollback, and the hourly scout's long-lived connection held SHARED on
minerva.db for 4 minutes -- every minerva.db COMMIT fleet-wide failed meanwhile.
SCOUT_PATH env overrides the module under test (used to prove the old code fails).
"""
import importlib.machinery, importlib.util, os, sqlite3, sys

import pytest

SCOUT = os.environ.get("SCOUT_PATH", "/home/lumen/minerva/lumen_scout_expanded.py")


def _load():
    sys.path.insert(0, "/home/lumen/minerva")
    loader = importlib.machinery.SourceFileLoader("scout_under_test", SCOUT)
    spec = importlib.util.spec_from_loader("scout_under_test", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def test_busy_insert_releases_every_lock(tmp_path):
    scout = _load()
    db = str(tmp_path / "m.db")
    s = sqlite3.connect(db)
    s.execute("CREATE TABLE classified_intel (id TEXT PRIMARY KEY, source TEXT, title TEXT, "
              "url TEXT, self_relevance REAL, category TEXT, reasoning TEXT, ts TEXT)")
    s.commit(); s.close()

    conn = sqlite3.connect(db, timeout=0.2)            # scout-style: legacy implicit BEGIN
    blocker = sqlite3.connect(db, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")                  # RESERVED held elsewhere
    try:
        ok = scout.insert_intel(conn, "src", "title", "https://x", 0.5, "misc", "r")
    finally:
        blocker.execute("ROLLBACK"); blocker.close()
    assert ok is False, "INSERT should have failed busy (test is not exercising the path)"
    assert not conn.in_transaction, "insert_intel left a transaction open after a busy INSERT"

    w = sqlite3.connect(db, timeout=0.2, isolation_level=None)
    try:
        w.execute("BEGIN IMMEDIATE")
        w.execute("INSERT INTO classified_intel(id) VALUES ('probe')")
        w.execute("COMMIT")                              # needs EXCLUSIVE: no SHARED may remain
    finally:
        w.close()
    conn.close()
