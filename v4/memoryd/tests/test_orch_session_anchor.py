"""wq#4538/wq#4559: the S2 session-claim anchor changes NOTHING in the daemon —
these tests pin that down from both sides.

The orchestrator fix is entirely client-side (lumen_episode.py folds unverifiable
refs under the session ticket the boot block claims) plus a disabled `session` lane
entry in foreman's dispatcher/config.json. I-7 (daemon.py resolve_identity) stays
exactly as strict as before: a registered session citing a ticket whose claim
receipt names someone else must still reject (design test 1, the wq#2630 can-fail
probe — it exercises the gate S2 deliberately does not touch, so it falsifies a
gate regression smuggled in with the client changes), while a session committing
under its own claimed-or-closed session ticket is accepted through the existing
wq#2901/wq#3352 receipt machinery with lane read out of the ticket (tests 2-3).

Runs against throwaway minerva.db + queue.db. Never writes to the live store.
"""
import importlib, json, os, sqlite3, sys, threading, urllib.error, urllib.request
from http.server import ThreadingHTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
MEMORYD = os.path.dirname(HERE)
LIVE_DB = "/home/lumen/minerva/minerva.db"
TABLES = ("archival_notes", "state_facts", "memory_audit", "memory_quarantine")

# 9201: a ticket the orch shepherded but an fworker claimed and closed -- the
# exact reject class from the wq#4538 problem statement. MUST still reject.
# 9202: the orch's own synthetic session ticket, claim live -- accept, lane=session.
# 9203: same shape after `wq done` -- accept via the wq#3352 receipt fallback.
TICKETS = [
    (9201, "build", "fworker-wq9201", "done"),
    (9202, "session", "orch-anchor-aa11bb", "claimed"),
    (9203, "session", "orch-anchor-aa11bb", "done"),
]
SESSIONS = ["orch-anchor-aa11bb"]


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
    root = tmp_path_factory.mktemp("memoryd4538")
    db = str(root / "minerva_test.db")
    con = sqlite3.connect(db)
    for sql in _schema_sql():
        con.execute(sql)
    con.commit(); con.close()

    queue = str(root / "queue_test.db")
    q = sqlite3.connect(queue)
    q.execute("CREATE TABLE tasks(id INTEGER PRIMARY KEY, lane TEXT, "
              "claimed_by TEXT, status TEXT)")
    q.executemany("INSERT INTO tasks(id,lane,claimed_by,status) VALUES (?,?,?,?)", TICKETS)
    q.execute("CREATE TABLE agent_sessions(session_id TEXT PRIMARY KEY)")
    q.executemany("INSERT INTO agent_sessions(session_id) VALUES (?)",
                  [(s,) for s in SESSIONS])
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


def _post(url, path, body, agent=None):
    headers = {"Content-Type": "application/json"}
    if agent:
        headers["X-Memoryd-Agent"] = agent
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(),
                                 method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except ValueError:
            return e.code, {"raw": raw[:200]}


def _note(tid, title=None):
    return {"kind": "session", "title": title or ("wq4538 probe %d" % tid),
            "source_ref": "wq#%d" % tid,
            "content": "An orchestrator session episode citing wq#%d." % tid}


def test_other_agents_ticket_must_still_reject(env):
    """Design test 1 (the ticket's named probe): registered session, source_ref
    names a ticket whose receipt says fworker-wq9201 and whose status is done --
    the gate S2 does not touch must refuse it with exactly today's message."""
    assert env["daemon"].K.ticket_claimant("9201") == "fworker-wq9201"
    st, r = _post(env["url"], "/commit", _note(9201), agent="orch-anchor-aa11bb")
    assert st == 403
    assert "does not hold a live claim" in r.get("reason", "")


def test_own_live_session_anchor_is_accepted_with_session_lane(env):
    """Design test 2: the synthetic session ticket, claim live -- accepted, and
    the lane is read OUT of the ticket (lane='session'), never self-declared."""
    st, r = _post(env["url"], "/commit", _note(9202), agent="orch-anchor-aa11bb")
    assert st == 201 and r["status"] == "accept", r
    with sqlite3.connect(env["db"]) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT lane, agent_id, source_ref FROM archival_notes "
                        "WHERE id=?", (r["id"],)).fetchone()
    assert row["lane"] == "session"
    assert row["agent_id"] == "orch-anchor-aa11bb"
    assert row["source_ref"] == "wq#9202"


def test_closed_session_anchor_still_accepted_via_receipt(env):
    """Design test 3: the session closed its own anchor at handoff -- the claim
    receipt survives the close (wq#3352, daemon.py ticket_claimant fallback), so
    late spool replays still land."""
    with sqlite3.connect(env["queue"]) as c:
        assert c.execute("SELECT status FROM tasks WHERE id=9203").fetchone()[0] == "done"
    st, r = _post(env["url"], "/commit", _note(9203), agent="orch-anchor-aa11bb")
    assert st == 201 and r["status"] == "accept", r
