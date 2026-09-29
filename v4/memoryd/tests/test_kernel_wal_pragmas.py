"""wq#2930: the kernel's per-connection pragmas after the wq#2907 WAL migration.

journal_mode=WAL is persisted in the db file by the migration; busy_timeout and
synchronous are per-connection, so the kernel has to set them itself on every connect.
Runs against throwaway dbs only, never /home/lumen/minerva/minerva.db.
"""
import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
MEMORYD = os.path.dirname(HERE)
sys.path.insert(0, MEMORYD)
import kernel  # noqa: E402

NORMAL, FULL = 1, 2   # PRAGMA synchronous values


def _kernel(db):
    return kernel.Kernel(db_path=str(db))


def _make_db(path, mode):
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=%s" % mode)
    con.execute("CREATE TABLE t (x)")
    con.commit()
    con.close()
    return path


def test_default_busy_timeout_is_thirty_seconds():
    assert kernel.Kernel.busy_timeout_s >= 30.0


def test_connect_waits_the_full_busy_timeout(tmp_path):
    db = _make_db(tmp_path / "w.db", "wal")
    con = _kernel(db).connect()
    try:
        assert con.execute("PRAGMA busy_timeout").fetchone()[0] >= 30000
    finally:
        con.close()


def test_wal_db_gets_synchronous_normal(tmp_path):
    db = _make_db(tmp_path / "w.db", "wal")
    con = _kernel(db).connect()
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert con.execute("PRAGMA synchronous").fetchone()[0] == NORMAL
    finally:
        con.close()


def test_delete_mode_db_keeps_synchronous_full(tmp_path):
    """A restored pre-WAL backup (the wq#2907 rollback path) must not inherit NORMAL:
    outside WAL it is a real durability loss."""
    db = _make_db(tmp_path / "d.db", "delete")
    con = _kernel(db).connect()
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert con.execute("PRAGMA synchronous").fetchone()[0] == FULL
    finally:
        con.close()


def test_a_second_writer_queues_instead_of_failing(tmp_path):
    """The behaviour the longer timeout buys: while one writer holds the lock a second
    waits (bounded by busy_timeout_s) and lands once it is released."""
    import threading
    import time
    db = _make_db(tmp_path / "w.db", "wal")
    k = _kernel(db)
    k.busy_timeout_s = 5.0
    holder = sqlite3.connect(str(db), isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    threading.Timer(0.6, lambda: holder.execute("COMMIT")).start()
    t0 = time.time()
    with k.tx() as con:
        con.execute("INSERT INTO t VALUES (1)")
    assert time.time() - t0 >= 0.5
    holder.close()
    chk = sqlite3.connect(str(db))
    assert chk.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    chk.close()
