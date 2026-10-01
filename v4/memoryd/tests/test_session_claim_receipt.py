"""wq#3352: a registered SESSION identity that just closed its own ticket (`wq done`)
gets lane attribution too; an impostor still doesn't.

wq#2901 fixed this for `fworker-wq<N>` identities only (claim RECEIPT survives a close,
kernel.py:327 ticket_claimant). Ordinary session identities (chat sessions, burn
workers, anything not matching FWORKER_IDENTITY_RE) still went through agent_claims()'s
live-lease check, which fails the instant the session runs `wq done` -- i.e. on every
normal "finish the ticket, then commit the episode" ordering. Confirmed live 2026-10-01
via `journalctl -u lumen-episode-spool-replay.service`: wq#3115/3298/3299/3303 spool
entries stuck on "agent does not hold a live claim on wq#N" indefinitely, because the
session stays registered (so wq#3309's terminal-state recovery never fires) while never
re-claiming an already-closed ticket.

Design: this fix mirrors wq#2901's fworker branch exactly -- same ticket_claimant()
fallback, same "any status" provenance check -- just applied to the session branch in
daemon.py's resolve_identity too.

Runs against throwaway minerva.db + queue.db. Never writes to the live store.
"""
import importlib, json, os, sqlite3, sys, threading, urllib.error, urllib.request
from http.server import ThreadingHTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
MEMORYD = os.path.dirname(HERE)
LIVE_DB = "/home/lumen/minerva/minerva.db"
TABLES = ("archival_notes", "state_facts", "memory_audit", "memory_quarantine")

# 9101: session closed its own ticket (`done`, claimed_by still names it) -- the
# normal case this ticket fixes.
# 9102: ticket re-claimed by someone else after the session closed it -- wq#2630
# can-fail probe: must still reject, provenance now points elsewhere.
# 9103: session still holds a LIVE claim -- regression check, unaffected code path.
TICKETS = [
    (9101, "build", "session-abc123", "done"),
    (9102, "build", "session-xyz789", "claimed"),
    (9103, "build", "session-live001", "claimed"),
]
SESSIONS = ["session-abc123", "session-live001", "session-xyz789"]


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
    root = tmp_path_factory.mktemp("memoryd3352")
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
    return {"kind": "code-ship", "title": title or ("wq3352 probe %d" % tid),
            "source_ref": "wq#%d" % tid,
            "content": "A session episode digest for wq#%d, committed right after "
                       "`wq done` closed the lease on the same ticket." % tid}


def test_closed_session_claim_still_attributes(env):
    """The fix: wq#9101 is `done`, not `claimed` -- a live-lease check rejects
    exactly the ordinary "finish, then commit" sequence. The claim receipt accepts."""
    with sqlite3.connect(env["queue"]) as c:
        assert c.execute("SELECT status FROM tasks WHERE id=9101").fetchone()[0] == "done"
    assert env["daemon"].K.ticket_claimant("9101") == "session-abc123"
    st, r = _post(env["url"], "/commit", _note(9101), agent="session-abc123")
    assert st == 201 and r["status"] == "accept", r
    with sqlite3.connect(env["db"]) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT lane, agent_id, trust, source_ref FROM "
                        "archival_notes WHERE id=?", (r["id"],)).fetchone()
    assert row["lane"] == "build"
    assert row["agent_id"] == "session-abc123"
    assert row["trust"] == "derived"
    assert row["source_ref"] == "wq#9101"


def test_reclaimed_ticket_still_rejects_the_old_claimant(env):
    """wq#2630 can-fail probe, drawn from outside the fix's target population:
    session-xyz789 is still a REGISTERED session (passes the registry check this fix
    never touches) but wq#9102's claim receipt has been overwritten by someone else --
    provenance now points elsewhere, so the write must still be refused, not silently
    accepted just because the agent is registered."""
    with sqlite3.connect(env["queue"]) as c:
        c.execute("UPDATE tasks SET claimed_by='session-someone-else' WHERE id=9102")
        c.commit()
    assert env["daemon"].K.ticket_claimant("9102") == "session-someone-else"
    st, r = _post(env["url"], "/commit", _note(9102), agent="session-xyz789")
    assert st == 403
    assert "does not hold a live claim" in r.get("reason", "")


def test_live_claim_still_works_unchanged(env):
    """Regression check: the ordinary live-lease path (status='claimed', agent still
    holds it) is untouched by the fallback -- it must accept without ever reaching
    ticket_claimant()."""
    st, r = _post(env["url"], "/commit", _note(9103), agent="session-live001")
    assert st == 201 and r["status"] == "accept", r


def test_unregistered_agent_still_rejects_before_any_claim_check(env):
    """An agent with no session row at all is refused at the registry check (wq#502),
    same as before this fix -- the fallback only widens what counts as "still holds
    the claim", never who counts as a registered session."""
    st, r = _post(env["url"], "/commit", _note(9101), agent="never-registered-xyz")
    assert st == 403
    assert "not in the wq session registry" in r.get("reason", "")
