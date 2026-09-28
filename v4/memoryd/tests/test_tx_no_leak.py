"""wq#2895 regression: no memoryd request may leave a minerva.db transaction open.

Incident 2026-09-28 04:36-05:11Z: the identity-reject audit did BEGIN IMMEDIATE,
COMMIT hit SQLITE_BUSY (a reader held SHARED), `except: pass` swallowed it, and a
BrokenPipe traceback pinned the handler frame holding the connection -- PENDING
lock held until GC, which an idle daemon never ran. Every writer fleet-wide locked.

The invariant tested here does NOT depend on GC: every connection the daemon opens
is tracked (a strong reference, i.e. the worst case the traceback created), and
after each request none of them may be open with a transaction in flight, and a
fresh writer must get BEGIN IMMEDIATE at once.

Runs against a throwaway DB built from the live schema of the four tables the
daemon touches. Never writes to /home/lumen/minerva/minerva.db.
"""
import importlib, json, os, sqlite3, sys, threading, time, urllib.error, urllib.request
from http.server import ThreadingHTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
MEMORYD = os.path.dirname(HERE)
LIVE_DB = "/home/lumen/minerva/minerva.db"
TABLES = ("archival_notes", "state_facts", "memory_audit", "memory_quarantine")


def _schema_sql():
    ro = sqlite3.connect("file:%s?mode=ro" % LIVE_DB, uri=True, timeout=10)
    try:
        rows = ro.execute(
            "SELECT type, sql FROM sqlite_master WHERE tbl_name IN (%s) AND sql IS NOT NULL "
            "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END"
            % ",".join("?" * len(TABLES)), TABLES).fetchall()
    finally:
        ro.close()
    return [r[1] for r in rows]


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    db = str(tmp_path_factory.mktemp("memoryd") / "minerva_test.db")
    con = sqlite3.connect(db)
    for sql in _schema_sql():
        con.execute(sql)
    con.commit(); con.close()
    os.environ["MEMORYD_DB"] = db
    sys.path.insert(0, MEMORYD)
    sys.modules.pop("daemon", None)
    daemon = importlib.import_module("daemon")
    assert daemon.K.db_path == db
    daemon.K.busy_timeout_s = 0.3          # shrink the busy wait so the test is fast
    tracked = []
    real_connect = type(daemon.K).connect

    def tracking_connect():
        c = real_connect(daemon.K)
        tracked.append(c)                  # strong ref = the pinned-frame worst case
        return c
    daemon.K.connect = tracking_connect
    srv = ThreadingHTTPServer(("127.0.0.1", 0), daemon.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"db": db, "daemon": daemon, "tracked": tracked,
           "url": "http://127.0.0.1:%d" % srv.server_address[1]}
    srv.shutdown()


def _post(url, path, body, headers=None):
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                 headers=dict({"Content-Type": "application/json"}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _open_txn(tracked):
    bad = []
    for c in tracked:
        try:
            if c.in_transaction:
                bad.append(c)
        except sqlite3.ProgrammingError:   # closed -- the correct outcome
            pass
    return bad


def _writer_can_begin(db, timeout=0.2):
    c = sqlite3.connect(db, timeout=timeout, isolation_level=None)
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        c.close()


def _assert_clean(env):
    time.sleep(0.05)
    assert _open_txn(env["tracked"]) == [], "a memoryd connection survived with an open transaction"
    assert _writer_can_begin(env["db"]), "minerva.db write lock still held after the request"


def test_incident_repro_identity_reject_with_busy_commit(env):
    """The exact 04:36Z shape: a reader holds SHARED, the reject-audit COMMIT goes BUSY."""
    reader = sqlite3.connect(env["db"], isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM memory_audit").fetchone()   # SHARED held
    try:
        assert _post(env["url"], "/commit", {"title": "x"}) == 403   # no identity header
    finally:
        reader.execute("COMMIT"); reader.close()
    _assert_clean(env)


def test_identity_reject_when_audit_raises(env, monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("injected audit failure after BEGIN IMMEDIATE")
    monkeypatch.setattr(env["daemon"].K, "audit", boom)
    assert _post(env["url"], "/commit", {"title": "x"}) == 403
    _assert_clean(env)


def test_healthz_error_closes_connection(env, tmp_path, monkeypatch):
    monkeypatch.setattr(env["daemon"].K, "db_path", str(tmp_path / "empty.db"))
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(env["url"] + "/healthz", timeout=10)
    assert ei.value.code == 500
    assert _open_txn(env["tracked"]) == []
    leaked_open = []
    for c in env["tracked"]:
        try:
            c.execute("SELECT 1")
            leaked_open.append(c)
        except sqlite3.ProgrammingError:
            pass
    assert leaked_open == [], "_healthz left a connection open after a failed query"


def test_concurrent_writers_survive_a_reject_storm(env):
    """wq#2895 acceptance: concurrent writes work while memoryd is hammered on the
    reject path (the fworker 403 loop that drove the leaky path hot)."""
    stop = threading.Event()
    codes, writes, errors = [], [], []

    def storm():
        while not stop.is_set():
            codes.append(_post(env["url"], "/commit", {"title": "storm"}))

    def writer(n):
        c = sqlite3.connect(env["db"], timeout=5, isolation_level=None)
        try:
            for i in range(25):
                c.execute("BEGIN IMMEDIATE")
                c.execute("INSERT INTO memory_audit(ts,action,table_name,detail) VALUES "
                          "(datetime('now'),'write','test','writer-%d-%d')" % (n, i))
                c.execute("COMMIT")
                writes.append(1)
        except Exception as e:
            errors.append(repr(e))
        finally:
            c.close()
    ts = [threading.Thread(target=storm) for _ in range(6)]
    ws = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in ts + ws:
        t.start()
    for t in ws:
        t.join(60)
    stop.set()
    for t in ts:
        t.join(30)
    assert errors == [], errors
    assert len(writes) == 100
    assert codes and set(codes) <= {403, 503}, set(codes)
    _assert_clean(env)


def test_kernel_tx_releases_lock_when_commit_is_busy(env):
    K = env["daemon"].K
    reader = sqlite3.connect(env["db"], isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM memory_audit").fetchone()
    try:
        with pytest.raises(sqlite3.OperationalError):
            with K.tx() as con:
                K.audit(con, "reject", "archival_notes", detail="tx busy probe")
    finally:
        reader.execute("COMMIT"); reader.close()
    _assert_clean(env)


def test_class_guard_no_raw_transactions_in_daemon():
    """Make the class impossible to reintroduce: the daemon opens connections and
    write transactions ONLY through Kernel.tx()/ro()."""
    src = open(os.path.join(MEMORYD, "daemon.py")).read()
    assert "BEGIN IMMEDIATE" not in src
    assert "K.connect(" not in src


def test_search_v3_closes_connection_when_core_raises(monkeypatch):
    """wq#2895 sweep: lumen_recall2 runs inside memoryd on every /recall. A raise
    mid-query must not leave its minerva.db connection (and a SHARED lock) open."""
    sys.path.insert(0, "/home/lumen/minerva")
    import lumen_recall2 as lr
    opened = []
    real = sqlite3.connect

    def track(*a, **k):
        c = real(*a, **k)
        opened.append(c)
        return c

    def boom(conn, query, top_k=10):
        cur = conn.execute("SELECT id FROM archival_notes")   # un-reset SELECT => SHARED
        cur.fetchone()
        raise RuntimeError("injected mid-query failure")
    monkeypatch.setattr(lr.sqlite3, "connect", track)
    monkeypatch.setattr(lr, "_search_v3_core", boom)
    with pytest.raises(RuntimeError):
        lr._search_v3_impl("anything", top_k=3)
    assert opened, "search did not open a connection (test is not exercising the path)"
    for c in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            c.execute("SELECT 1")          # must already be closed
