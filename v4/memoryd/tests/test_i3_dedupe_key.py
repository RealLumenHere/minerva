"""wq#5152: I-3 "exact live duplicate is a no-op" must not collapse distinct episodes.

Found in the wq#5013 spool replay: I-3 matched content_sha256 only. lumen_episode bodies are
often all-'(none)' sections, so fworker-wq4882/4921/4935 replays all returned the SAME id
(91a976768eb6) and the 4921/4935 titles never reached the governed store. The dedupe key is
now content + title + source_ref; a replay of the same episode is still a no-op.

Runs against throwaway minerva.db + queue.db (schema copied read-only from the live store).
Never writes to the live store.
"""
import importlib, json, os, sqlite3, sys, threading, urllib.error, urllib.request
from http.server import ThreadingHTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
MEMORYD = os.path.dirname(HERE)
LIVE_DB = "/home/lumen/minerva/minerva.db"
TABLES = ("archival_notes", "state_facts", "memory_audit", "memory_quarantine")
AGENT = "session-dedupe5152"
TICKETS = [(9201, "build", AGENT, "claimed"), (9202, "build", AGENT, "claimed")]

# The shape that collapsed: every section empty, so the body is identical across episodes.
EMPTY_BODY = ("## Changed\n(none)\n## Worked\n(none)\n## Failed\n(none)\n## Why\n(none)\n")


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
    root = tmp_path_factory.mktemp("memoryd5152")
    db = str(root / "minerva_test.db")
    con = sqlite3.connect(db)
    for sql in _schema_sql():
        con.execute(sql)
    con.commit(); con.close()

    queue = str(root / "queue_test.db")
    q = sqlite3.connect(queue)
    q.execute("CREATE TABLE tasks(id INTEGER PRIMARY KEY, lane TEXT, claimed_by TEXT, status TEXT)")
    q.executemany("INSERT INTO tasks(id,lane,claimed_by,status) VALUES (?,?,?,?)", TICKETS)
    q.execute("CREATE TABLE agent_sessions(session_id TEXT PRIMARY KEY)")
    q.execute("INSERT INTO agent_sessions(session_id) VALUES (?)", (AGENT,))
    q.commit(); q.close()

    os.environ["MEMORYD_DB"] = db
    sys.path.insert(0, MEMORYD)
    sys.modules.pop("daemon", None)
    daemon = importlib.import_module("daemon")
    daemon.K.queue_db = queue
    srv = ThreadingHTTPServer(("127.0.0.1", 0), daemon.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"db": db, "url": "http://127.0.0.1:%d" % srv.server_address[1]}
    srv.shutdown()


def _commit(env, title, tid, content=EMPTY_BODY):
    body = {"kind": "session", "title": title, "source_ref": "wq#%d" % tid, "content": content}
    req = urllib.request.Request(env["url"] + "/commit", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Memoryd-Agent": AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def _live_titles(env):
    with sqlite3.connect(env["db"]) as c:
        return sorted(r[0] for r in c.execute(
            "SELECT title FROM archival_notes WHERE revoked_at IS NULL AND superseded_by IS NULL"))


def test_same_body_different_title_is_two_episodes(env):
    st1, r1 = _commit(env, "fworker-wq4921 attempt 1 SUCCESS: probe A", 9201)
    st2, r2 = _commit(env, "fworker-wq4935 attempt 1 SUCCESS: probe B", 9201)
    assert st1 == 201 and r1["status"] == "accept", r1
    assert st2 == 201 and r2["status"] == "accept", r2
    assert r1["id"] != r2["id"], "distinct episodes collapsed onto one id (the wq#5152 bug)"
    assert {"fworker-wq4921 attempt 1 SUCCESS: probe A",
            "fworker-wq4935 attempt 1 SUCCESS: probe B"} <= set(_live_titles(env))


def test_same_body_and_title_different_ticket_is_two_episodes(env):
    _, r1 = _commit(env, "batch close-out", 9201)
    _, r2 = _commit(env, "batch close-out", 9202)
    assert r1["id"] != r2["id"]


def test_exact_replay_is_still_a_noop(env):
    """Can-fail probe from the other side: the spool replays the same episode until it
    lands. Same content, title and ref must keep returning the original id, no new row."""
    title = "replayed episode wq#9202"
    _, first = _commit(env, title, 9202, content="replay body " + EMPTY_BODY)
    before = len(_live_titles(env))
    st, again = _commit(env, title, 9202, content="replay body " + EMPTY_BODY)
    assert st in (200, 201), again
    assert again["id"] == first["id"]
    assert len(_live_titles(env)) == before
