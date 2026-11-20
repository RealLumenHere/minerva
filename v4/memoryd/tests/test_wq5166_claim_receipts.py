"""wq#5166: episodes survive wq clearing claimed_by (block/frank_gated/release/reap).

Kernel.ticket_claimed_by_ever(tid, agent): claimed_by==agent, else (claimed_by NULL AND
the latest claim_receipts row names agent). A ticket re-claimed by someone else still
rejects the old claimant (wq#2630 can-fail probe), even though that agent has a receipt.

Throwaway minerva.db + queue.db. Never writes to the live store.
"""
import importlib, json, os, sqlite3, sys, threading, urllib.error, urllib.request
from http.server import ThreadingHTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
MEMORYD = os.path.dirname(HERE)
LIVE_DB = "/home/lumen/minerva/minerva.db"
TABLES = ("archival_notes", "state_facts", "memory_audit", "memory_quarantine")

# (id, lane, claimed_by, status)
TICKETS = [
    (9201, "build", None, "blocked"),          # session blocked its own ticket
    (9202, "build", "session-other01", "claimed"),  # re-claimed by someone else (2630)
    (9203, "build", None, "open"),             # released by A, re-claimed+released by B
    (9204, "build", None, "frank_gated"),      # fworker parked its ticket
    (9205, "build", None, "open"),             # nobody ever claimed it
]
RECEIPTS = [  # (ticket_id, agent) in insertion order
    (9201, "session-blk001"),
    (9202, "session-old001"), (9202, "session-other01"),
    (9203, "session-aaa001"), (9203, "session-bbb001"),
    (9204, "fworker-wq9204"),
]
SESSIONS = ["session-blk001", "session-old001", "session-other01",
            "session-aaa001", "session-bbb001", "session-none01"]


def _schema_sql():
    ro = sqlite3.connect("file:%s?mode=ro" % LIVE_DB, uri=True, timeout=10)
    try:
        rows = ro.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name IN (%s) AND sql IS NOT NULL "
            "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END"
            % ",".join("?" * len(TABLES)), TABLES).fetchall()
    finally:
        ro.close()
    return [r[0] for r in rows]


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("memoryd5166")
    db = str(root / "minerva_test.db")
    con = sqlite3.connect(db)
    for sql in _schema_sql():
        con.execute(sql)
    con.commit(); con.close()

    queue = str(root / "queue_test.db")
    q = sqlite3.connect(queue)
    q.execute("CREATE TABLE tasks(id INTEGER PRIMARY KEY, lane TEXT, claimed_by TEXT, status TEXT)")
    q.executemany("INSERT INTO tasks VALUES (?,?,?,?)", TICKETS)
    q.execute("CREATE TABLE agent_sessions(session_id TEXT PRIMARY KEY)")
    q.executemany("INSERT INTO agent_sessions VALUES (?)", [(s,) for s in SESSIONS])
    q.execute("CREATE TABLE claim_receipts(id INTEGER PRIMARY KEY AUTOINCREMENT, "
              "ticket_id INTEGER NOT NULL, agent TEXT NOT NULL, claimed_at TEXT NOT NULL, "
              "fence INTEGER, kind TEXT NOT NULL DEFAULT 'claim')")
    q.executemany("INSERT INTO claim_receipts(ticket_id, agent, claimed_at) VALUES (?,?,'t')",
                  RECEIPTS)
    q.commit(); q.close()

    os.environ["MEMORYD_DB"] = db
    sys.path.insert(0, MEMORYD)
    sys.modules.pop("daemon", None)
    daemon = importlib.import_module("daemon")
    daemon.K.queue_db = queue
    srv = ThreadingHTTPServer(("127.0.0.1", 0), daemon.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"db": db, "queue": queue, "daemon": daemon,
           "url": "http://127.0.0.1:%d" % srv.server_address[1]}
    srv.shutdown()


def _post(url, body, agent):
    req = urllib.request.Request(url + "/commit", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-Memoryd-Agent": agent})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except ValueError:
            return e.code, {"raw": raw[:200]}


def _note(tid, who):
    return {"kind": "code-ship", "title": "wq5166 probe %d %s" % (tid, who),
            "source_ref": "wq#%d" % tid,
            "content": "Episode digest for wq#%d written by %s after wq cleared "
                       "claimed_by on block/release; the claim receipt must carry it." % (tid, who)}


def test_kernel_rule_table(env):
    K = env["daemon"].K
    assert K.ticket_claimed_by_ever("9201", "session-blk001") is True
    assert K.ticket_claimed_by_ever("9202", "session-old001") is False   # 2630
    assert K.ticket_claimed_by_ever("9202", "session-other01") is True
    assert K.ticket_claimed_by_ever("9203", "session-aaa001") is False   # not the latest
    assert K.ticket_claimed_by_ever("9203", "session-bbb001") is True
    assert K.ticket_claimed_by_ever("9205", "session-none01") is False   # never claimed
    assert K.ticket_claimed_by_ever("99999", "session-blk001") is False  # no such ticket
    assert K.ticket_claimed_by_ever("9201", "") is False


def test_blocked_ticket_episode_now_accepts(env):
    """The bug: claimed_by NULL after `wq block` -> I-7 reject. Receipt fixes it."""
    assert env["daemon"].K.ticket_claimant("9201") is None
    st, r = _post(env["url"], _note(9201, "blk"), "session-blk001")
    assert st == 201 and r["status"] == "accept", r
    with sqlite3.connect(env["db"]) as c:
        lane, agent = c.execute("SELECT lane, agent_id FROM archival_notes WHERE id=?",
                                (r["id"],)).fetchone()
    assert (lane, agent) == ("build", "session-blk001")


def test_reclaimed_ticket_rejects_old_claimant_despite_receipt(env):
    """wq#2630 can-fail probe: session-old001 HAS a receipt for 9202, but the ticket is
    claimed by someone else now -> refuse."""
    st, r = _post(env["url"], _note(9202, "old"), "session-old001")
    assert st == 403
    assert "does not hold a live claim" in r.get("reason", "")


def test_earlier_claimant_of_released_ticket_rejects(env):
    """A released, B re-claimed then released: only B (latest receipt) is attributed."""
    st, r = _post(env["url"], _note(9203, "aaa"), "session-aaa001")
    assert st == 403
    st, r = _post(env["url"], _note(9203, "bbb"), "session-bbb001")
    assert st == 201 and r["status"] == "accept", r


def test_parked_fworker_episode_accepts(env):
    st, r = _post(env["url"], _note(9204, "fw"), "fworker-wq9204")
    assert st == 201 and r["status"] == "accept", r


def test_missing_receipts_table_degrades_to_claimed_by(env, tmp_path):
    q2 = str(tmp_path / "queue_nort.db")
    q = sqlite3.connect(q2)
    q.execute("CREATE TABLE tasks(id INTEGER PRIMARY KEY, lane TEXT, claimed_by TEXT, status TEXT)")
    q.executemany("INSERT INTO tasks VALUES (?,?,?,?)",
                  [(1, "build", None, "blocked"), (2, "build", "s-x", "done")])
    q.commit(); q.close()
    K = env["daemon"].K
    old = K.queue_db
    try:
        K.queue_db = q2
        assert K.ticket_claimed_by_ever("1", "s-x") is False
        assert K.ticket_claimed_by_ever("2", "s-x") is True
    finally:
        K.queue_db = old
