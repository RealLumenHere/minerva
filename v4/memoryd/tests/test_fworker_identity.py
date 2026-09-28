"""wq#2901: fleet-worker identities get lane attribution; nothing else does.

Before this, every fworker episode commit was a 403 "not in the wq session registry"
(400-2600/day since 2026-09-20) and fell back to an unattributed direct write. The
identity `fworker-wq<N>` is deliberately absent from `agent_sessions` -- `wq
holder_is_dead` reads that absence as half its liveness signal -- so attribution comes
from the claim RECEIPT instead: the identity names its own ticket and the ticket's
claimed_by names the identity back.

Design: v4/research/fworker_lane_attribution.md

Runs against throwaway minerva.db + queue.db. Never writes to the live store.
"""
import importlib, json, os, sqlite3, sys, threading, urllib.error, urllib.request
from http.server import ThreadingHTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
MEMORYD = os.path.dirname(HERE)
LIVE_DB = "/home/lumen/minerva/minerva.db"
TABLES = ("archival_notes", "state_facts", "memory_audit", "memory_quarantine")

# The fixture tickets. 9001 is the happy path and its lane (`build`) is one this
# ticket ADDED to the contract, so the probe exercises the new entry rather than a
# lane that already worked. 9002 is the falsifying case required by wq#2630: a
# well-formed fleet-worker identity for a ticket held by somebody else.
TICKETS = [
    (9001, "build", "fworker-wq9001", "done"),      # closed claim: the normal case
    (9002, "build", "fworker-wq9099", "claimed"),   # someone else's ticket
    (9003, "research", "fworker-wq9003", "claimed"),
    (9004, "no-such-lane", "fworker-wq9004", "claimed"),
]


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
    root = tmp_path_factory.mktemp("memoryd2901")
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


def _note(tid, title="wq2901 probe"):
    return {"kind": "code-ship", "title": title, "source_ref": "wq#%d" % tid,
            "content": "An fworker episode digest, committed through the governed "
                       "path so its lane comes from the ticket rather than the writer."}


def test_fworker_commit_accepted_with_lane_from_the_ticket(env):
    st, r = _post(env["url"], "/commit", _note(9001), agent="fworker-wq9001")
    assert st == 201 and r["status"] == "accept", r
    with sqlite3.connect(env["db"]) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT lane, agent_id, trust, origin, source_ref FROM "
                        "archival_notes WHERE id=?", (r["id"],)).fetchone()
    assert row["lane"] == "build"                 # read out of the ticket, not the body
    assert row["agent_id"] == "fworker-wq9001"
    assert row["trust"] == "derived"
    assert row["origin"] == "agent-lane:build:wq#9001"
    assert row["source_ref"] == "wq#9001"


def test_closed_claim_still_attributes(env):
    """wq#9001 is `done`, not `claimed`: fworker commits its episode AFTER the agent
    inside it ran `wq done`, so a live-lease check would reject exactly the runs that
    succeeded. The claim receipt is the durable fact."""
    with sqlite3.connect(env["queue"]) as c:
        assert c.execute("SELECT status FROM tasks WHERE id=9001").fetchone()[0] == "done"
    assert env["daemon"].K.ticket_claimant("9001") == "fworker-wq9001"
    assert "9001" not in env["daemon"].K.agent_claims("fworker-wq9001")


def test_fworker_for_a_ticket_it_does_not_hold_is_refused(env):
    """The falsifying probe (wq#2630): well-formed identity, existing ticket, lane in
    the contract -- and claimed_by names a different holder."""
    st, r = _post(env["url"], "/commit", _note(9002), agent="fworker-wq9002")
    assert st == 403 and "does not hold the claim" in r["reason"], r


def test_fworker_may_only_write_about_its_own_ticket(env):
    st, r = _post(env["url"], "/commit", _note(9003), agent="fworker-wq9001")
    assert st == 403 and "source_ref=wq#9001" in r["reason"], r


def test_fworker_with_a_path_source_ref_is_refused(env):
    """The pre-fix shape: --ref <path to episode.jsonl>. A path can never yield a lane."""
    body = dict(_note(9001), source_ref="/home/lumen/foreman/_runs/wq9001/episode.jsonl")
    st, r = _post(env["url"], "/commit", body, agent="fworker-wq9001")
    assert st == 403 and "source_ref=wq#9001" in r["reason"], r


def test_unknown_lane_is_still_fail_closed(env):
    st, r = _post(env["url"], "/commit", _note(9004), agent="fworker-wq9004")
    assert st == 422 and r["invariant"] == "I-4", r


def test_unregistered_non_fworker_identity_still_rejected(env):
    """Drill D9 must stay red: the new class is narrower than a session, not a bypass."""
    st, r = _post(env["url"], "/commit", _note(9001), agent="not-a-registered-session")
    assert st == 403 and "session registry" in r["reason"], r


def test_fworker_read_without_a_ref_is_unattributed_not_a_reject(env):
    """§2.5: reads are the lesser risk and must never starve a lane prompt."""
    ident, err = _resolve(env, "fworker-wq9001", {}, for_write=False)
    assert err is None and ident.lane == "unattributed"
    assert ident.agent_id == "fworker-wq9001"


def _resolve(env, agent, body, for_write):
    """resolve_identity without a socket: it reads only self.headers and the body."""
    daemon = env["daemon"]
    h = daemon.Handler.__new__(daemon.Handler)
    h.headers = {"X-Memoryd-Agent": agent}
    return daemon.Handler.resolve_identity(h, body, for_write=for_write)


def test_lane_drift_reports_lanes_with_no_contract_row(env):
    """The silence that let this run for eight days: `build` had no contract row and
    every write from it was refused with no signal anywhere."""
    assert env["daemon"].K.lane_drift() == ["no-such-lane"]
