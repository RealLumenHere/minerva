"""wq#2930: the WAL follow-ups outside the kernel.

  * memory_embed_guard turns a held minerva.db lock into retry -> ONE aq row -> exit 0
    (the wq#2570/2571 guard class) instead of a crash, exit 2 and restart/alert spam.
  * the connections the ticket named wait >= 30 s on a busy db: pinned, not assumed.
No test touches /home/lumen/minerva/minerva.db.
"""
import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import memory_embed_guard as g  # noqa: E402

# memory_embed_guard puts the PRIMARY tree at sys.path[0] on import. Re-assert this
# checkout first so the minerva_memory / lumen_state imports below test THIS tree, not
# whatever is deployed.
sys.path.insert(0, HERE)
assert os.path.dirname(os.path.abspath(g.__file__)) == HERE


def _load(name):
    """Load HERE/<name>.py by explicit path under a private module name. A bare
    `import minerva_memory` can resolve to the deployed primary tree (other modules and
    the memoryd tests put it on sys.path), which would test the wrong code."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("wq2930_" + name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _locked():
    return sqlite3.OperationalError("database is locked")


@pytest.fixture
def rig(monkeypatch):
    slept, alerts = [], []
    monkeypatch.setattr(g.time, "sleep", slept.append)
    monkeypatch.setattr(g, "alert_db_locked", alerts.append)
    return slept, alerts


def test_recovers_when_the_lock_clears(rig):
    slept, alerts = rig
    calls = []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise _locked()
        return 0
    assert g.run_with_lock_retry(fn) == 0
    assert slept == list(g.LOCK_BACKOFF_S[:2])
    assert alerts == []


def test_lock_that_outlasts_every_retry_is_one_alert_and_exit_zero(rig):
    slept, alerts = rig
    calls = []

    def fn():
        calls.append(1)
        raise _locked()
    assert g.run_with_lock_retry(fn) == 0          # not 2: no failed oneshot unit
    assert len(calls) == len(g.LOCK_BACKOFF_S) + 1
    assert slept == list(g.LOCK_BACKOFF_S)
    assert len(alerts) == 1 and "locked" in alerts[0]


def test_check_mode_does_not_raise_an_aq_row(rig):
    _, alerts = rig

    def fn():
        raise _locked()
    assert g.run_with_lock_retry(fn, alert=False) == 0
    assert alerts == []


def test_a_real_fault_is_not_swallowed_as_a_lock(rig):
    slept, alerts = rig

    def fn():
        raise sqlite3.OperationalError("no such table: archival_notes")
    with pytest.raises(sqlite3.OperationalError):
        g.run_with_lock_retry(fn)
    assert slept == [] and alerts == []


def test_main_does_not_call_a_held_lock_a_broken_store(monkeypatch):
    """The connect-time catch-all used to alarm 'BROKE' and return 2 for anything."""
    alarms = []
    monkeypatch.setattr(g, "alarm", lambda *a, **k: alarms.append(a))
    monkeypatch.setattr(g.sqlite3, "connect", lambda *a, **k: (_ for _ in ()).throw(_locked()))
    monkeypatch.setattr(sys, "argv", ["memory_embed_guard.py", "--check"])
    with pytest.raises(sqlite3.OperationalError):
        g.main()
    assert alarms == []


def test_main_closes_its_connection_when_scan_raises(monkeypatch):
    class Conn:
        closed = False

        def close(self):
            Conn.closed = True
    monkeypatch.setattr(g.sqlite3, "connect", lambda *a, **k: Conn())
    monkeypatch.setattr(g, "scan", lambda c: (_ for _ in ()).throw(_locked()))
    monkeypatch.setattr(sys, "argv", ["memory_embed_guard.py", "--check"])
    with pytest.raises(sqlite3.OperationalError):
        g.main()
    assert Conn.closed, "a retry must not inherit a lock-holding handle"


def test_lumen_state_connection_waits_thirty_seconds(tmp_path, monkeypatch):
    lumen_state = _load("lumen_state")
    db = tmp_path / "s.db"
    sqlite3.connect(str(db)).close()
    monkeypatch.setattr(lumen_state, "DB", str(db))
    c = lumen_state._conn()
    try:
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] >= 30000
    finally:
        c.close()


def test_episode_write_path_waits_thirty_seconds(tmp_path, monkeypatch):
    """lumen_episode writes through minerva_memory._connect (or memoryd's kernel)."""
    mm = _load("minerva_memory")
    db = tmp_path / "m.db"
    sqlite3.connect(str(db)).close()
    monkeypatch.setattr(mm, "DB", str(db))
    assert mm.DB_TIMEOUT >= 30.0, "the shipped default, not a patched value"
    c = mm._connect()
    try:
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] >= 30000
    finally:
        c.close()


def test_recall_fts_ddl_path_uses_the_long_timeout():
    """_ensure_recall_fts runs inside lumen-memoryd on every search and can take a write
    lock; it used to open with sqlite3's 5 s default."""
    import inspect
    mm = _load("minerva_memory")
    assert "timeout=DB_TIMEOUT" in inspect.getsource(mm._ensure_recall_fts)
