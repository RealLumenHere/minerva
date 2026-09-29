"""wq#3146: sweep_empty_source_proposals.main() closes its connection on every
exit path -- dry-run, --apply, and an exception mid-sweep."""
import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import sweep_empty_source_proposals as sweep  # noqa: E402

_real_connect = sqlite3.connect
opened = []


class _TrackedConn(sqlite3.Connection):
    closed = False

    def close(self):
        self.closed = True
        super().close()


def _tracked_connect(path):
    conn = _real_connect(path, factory=_TrackedConn)
    opened.append(conn)
    return conn


@pytest.fixture(autouse=True)
def _track(monkeypatch):
    opened.clear()
    monkeypatch.setattr(sweep.sqlite3, "connect", _tracked_connect)


def _seed(path):
    conn = _real_connect(path)
    conn.executescript(
        "CREATE TABLE classified_intel (id INTEGER PRIMARY KEY, url TEXT, summary TEXT);"
        "CREATE TABLE proposals (id TEXT PRIMARY KEY, title TEXT, source_intel_id INTEGER,"
        " status TEXT, decision TEXT, decision_reason TEXT);"
        "INSERT INTO classified_intel VALUES (1, '', ''), (2, 'https://example.org', 'fetched');"
        "INSERT INTO proposals VALUES ('p1', 'a', 1, 'pending', 'pending', NULL),"
        " ('p2', 'b', 2, 'pending', 'pending', NULL);")
    conn.commit()
    conn.close()


def _status(path):
    conn = _real_connect(path)
    try:
        return dict(conn.execute("SELECT id, status FROM proposals"))
    finally:
        conn.close()


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["sweep", *argv])
    return sweep.main()


def test_dry_run_writes_nothing_and_closes(tmp_path, monkeypatch):
    db = str(tmp_path / "m.db")
    _seed(db)
    assert _run(monkeypatch, "--db", db) == 0
    assert _status(db) == {"p1": "pending", "p2": "pending"}
    assert [c.closed for c in opened] == [True]


def test_apply_rejects_empty_source_and_closes(tmp_path, monkeypatch):
    db = str(tmp_path / "m.db")
    _seed(db)
    assert _run(monkeypatch, "--db", db, "--apply") == 0
    assert _status(db) == {"p1": "rejected", "p2": "pending"}
    assert [c.closed for c in opened] == [True]


def test_exception_mid_sweep_still_closes(tmp_path, monkeypatch):
    db = str(tmp_path / "empty.db")  # no classified_intel table: the ALTER raises
    with pytest.raises(sqlite3.OperationalError):
        _run(monkeypatch, "--db", db)
    assert [c.closed for c in opened] == [True]
