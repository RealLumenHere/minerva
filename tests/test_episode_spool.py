"""wq#3173: memoryd's identity gate rejects ~33/day (agent not registered / no live
claim on source_ref / missing source_ref -- daemon.py resolve_identity, I-7). The gate
is correct; the client silently ate the reject. This covers the fix: the CLI now exits
non-zero with the reason as the first line, the rejected payload is spooled to
EPISODE_SPOOL_DIR, and lumen_episode_spool_replay.py drains that spool.
"""
import io
import json
import os
import sqlite3
import sys
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lumen_episode as le
import lumen_episode_spool_replay as replay


@pytest.fixture
def spool_dir(monkeypatch, tmp_path):
    d = tmp_path / "spool"
    monkeypatch.setattr(le, "EPISODE_SPOOL_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def hermetic_queue_db(monkeypatch, tmp_path):
    """wq#3309: the replayer now reads queue.db before every POST. No test in
    this module may touch the live one -- point both readers at a tmp path
    (absent until a test builds it, which exercises the fail-open-to-retry arm)."""
    db = tmp_path / "queue.db"
    monkeypatch.setattr(replay, "QUEUE_DB", str(db))
    monkeypatch.setattr(le, "QUEUE_DB", str(db))
    monkeypatch.setattr(replay.minerva_audit, "QUEUE_DB", str(db))
    # wq#4538: commit() reads this env; a live orch session exporting it must not
    # leak the S2 fold into unrelated tests.
    monkeypatch.delenv("WQ_SESSION_TICKET", raising=False)
    return db


# ---- _spool_rejected ------------------------------------------------------

def test_spool_rejected_writes_a_replayable_entry(spool_dir):
    name = le._spool_rejected(
        "session", "title", "content body", ["tag1"], "wq#3173",
        "agent does not hold a live claim on wq#3173", "fworker-wq3173")
    assert name is not None
    files = list(spool_dir.glob("*.json"))
    assert len(files) == 1
    entry = json.loads(files[0].read_text())
    assert entry["agent"] == "fworker-wq3173"
    assert entry["source_ref"] == "wq#3173"
    assert entry["kind"] == "session"
    assert entry["content"] == "content body"
    assert entry["attempts"] == 0
    assert "does not hold a live claim" in entry["reason"]
    assert entry["spooled_at"]


def test_spool_rejected_is_best_effort_on_an_unwritable_dir(monkeypatch):
    monkeypatch.setattr(le, "EPISODE_SPOOL_DIR", "/proc/nope/spool")
    assert le._spool_rejected("session", "t", "c", [], "wq#1", "reason", "agent") is None


# ---- _memoryd_commit reads the reject reason out of the 403 body ----------

class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code, body):
        super().__init__("http://x/commit", code, "reject", {}, io.BytesIO(body))
        self._body = body

    def read(self):
        return self._body


def test_memoryd_commit_extracts_reason_from_403_body(monkeypatch):
    monkeypatch.setattr(le.minerva_audit, "agent_id", lambda: "agent-1")

    class _Healthz:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req_or_url, timeout=None):
        if isinstance(req_or_url, str):
            return _Healthz()
        raise _FakeHTTPError(
            403, json.dumps({"status": "reject",
                             "reason": "agent 'agent-1' is not in the wq session registry"}).encode())

    monkeypatch.setattr(le.urllib.request, "urlopen", fake_urlopen)
    nid, reason = le._memoryd_commit("session", "t", "c", [], "wq#1")
    assert nid is None
    assert "not in the wq session registry" in reason


def test_memoryd_commit_falls_back_to_http_code_when_body_is_not_json(monkeypatch):
    monkeypatch.setattr(le.minerva_audit, "agent_id", lambda: "agent-1")

    class _Healthz:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req_or_url, timeout=None):
        if isinstance(req_or_url, str):
            return _Healthz()
        raise _FakeHTTPError(403, b"not json")

    monkeypatch.setattr(le.urllib.request, "urlopen", fake_urlopen)
    nid, reason = le._memoryd_commit("session", "t", "c", [], "wq#1")
    assert nid is None
    assert reason == "commit HTTP 403"


# ---- commit() spools on reject and returns the reason ---------------------

@pytest.fixture
def stub_mm(monkeypatch):
    """commit() must not touch the live DB in this test -- stub every mm.* call it
    makes on the fallback path."""
    monkeypatch.setattr(le.mm, "write_note_verified",
                        lambda *a, **k: ("local-note-1", None))
    monkeypatch.setattr(le.mm, "log_event", lambda *a, **k: 1)
    monkeypatch.setattr(le.mm, "verify_landed", lambda *a, **k: [])
    monkeypatch.setattr(le.mm, "embed_note", lambda *a, **k: None)


def test_commit_spools_and_returns_reason_on_reject(spool_dir, stub_mm, monkeypatch):
    monkeypatch.setattr(le, "_memoryd_commit",
                        lambda *a, **k: (None, "agent does not hold a live claim on wq#3173"))
    monkeypatch.setattr(le.minerva_audit, "agent_id", lambda: "fworker-wq3173")
    nid, reason = le.commit("did a thing", session_ref="wq#3173", kind="session", embed=False)
    assert nid == "local-note-1"
    assert reason == "agent does not hold a live claim on wq#3173"
    assert len(list(spool_dir.glob("*.json"))) == 1


def test_commit_does_not_spool_on_accept(spool_dir, stub_mm, monkeypatch):
    monkeypatch.setattr(le, "_memoryd_commit", lambda *a, **k: ("memoryd-note-1", None))
    monkeypatch.setattr(le.minerva_audit, "agent_id", lambda: "fworker-wq3173")
    nid, reason = le.commit("did a thing", session_ref="wq#3173", kind="session", embed=False)
    assert nid == "memoryd-note-1"
    assert reason is None
    assert not spool_dir.exists() or not list(spool_dir.glob("*.json"))


def test_commit_does_not_spool_without_an_agent(spool_dir, stub_mm, monkeypatch):
    """No $WQ_AGENT means nothing a replay could ever authenticate as."""
    monkeypatch.setattr(le, "_memoryd_commit",
                        lambda *a, **k: (None, "no $WQ_AGENT (memoryd needs a registered session)"))
    monkeypatch.setattr(le.minerva_audit, "agent_id", lambda: None)
    nid, reason = le.commit("did a thing", session_ref="", kind="session", embed=False)
    assert reason
    assert not spool_dir.exists() or not list(spool_dir.glob("*.json"))


# ---- CLI: reject exits non-zero with the reason as the first line ---------

def test_cli_commit_exits_4_with_reason_first_line_on_reject(monkeypatch, capsys):
    monkeypatch.setattr(le, "commit",
                        lambda *a, **k: ("local-note-1", "agent does not hold a live claim on wq#3173"))
    monkeypatch.setattr(le.mm, "embed_note", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv",
                        ["lumen_episode.py", "commit", "--summary", "s", "--ref", "wq#3173"])
    with pytest.raises(SystemExit) as exc:
        le._main()
    assert exc.value.code == 4
    err_lines = capsys.readouterr().err.splitlines()
    assert err_lines, "reject reason must be printed"
    assert "agent does not hold a live claim on wq#3173" in err_lines[0]


def test_cli_commit_exits_0_on_accept(monkeypatch, capsys):
    monkeypatch.setattr(le, "commit", lambda *a, **k: ("memoryd-note-1", None))
    monkeypatch.setattr(le.mm, "embed_note", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv",
                        ["lumen_episode.py", "commit", "--summary", "s", "--ref", "wq#3173"])
    le._main()  # must not raise/exit non-zero
    out = capsys.readouterr().out
    assert "committed episode memoryd-note-1" in out


# ---- replay script ----------------------------------------------------

def _write_spool_entry(spool_dir, name="entry.json", **overrides):
    spool_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "agent": "fworker-wq3173", "kind": "session", "title": "t", "content": "c",
        "tags": [], "source_ref": "wq#3173", "reason": "agent does not hold a live claim",
        "spooled_at": OLD_TS, "attempts": 0,
    }
    entry.update(overrides)
    path = spool_dir / name
    path.write_text(json.dumps(entry))
    return path


def test_replay_one_removes_the_file_on_landing(spool_dir, monkeypatch):
    path = _write_spool_entry(spool_dir)
    monkeypatch.setattr(replay.le, "_memoryd_commit", lambda *a, **k: ("new-note-1", None))
    outcome, detail = replay.replay_one(str(path))
    assert outcome == "landed"
    assert detail == "new-note-1"
    assert not path.exists()


def test_replay_one_replays_under_the_original_agent_identity(spool_dir, monkeypatch):
    path = _write_spool_entry(spool_dir, agent="original-agent-9")
    seen = {}

    def fake_commit(*a, **k):
        seen["agent"] = os.environ.get("WQ_AGENT")
        return None, "still no claim"

    monkeypatch.setattr(replay.le, "_memoryd_commit", fake_commit)
    monkeypatch.delenv("WQ_AGENT", raising=False)
    replay.replay_one(str(path))
    assert seen["agent"] == "original-agent-9"
    assert "WQ_AGENT" not in os.environ  # restored, not leaked


def test_replay_one_keeps_and_increments_attempts_on_repeat_reject(spool_dir, monkeypatch):
    path = _write_spool_entry(spool_dir, attempts=3)
    monkeypatch.setattr(replay.le, "_memoryd_commit", lambda *a, **k: (None, "still rejected"))
    outcome, detail = replay.replay_one(str(path))
    assert outcome == "still-rejected"
    assert path.exists()
    entry = json.loads(path.read_text())
    assert entry["attempts"] == 4
    assert entry["last_reason"] == "still rejected"


def test_replay_one_expires_after_max_attempts(spool_dir, monkeypatch):
    path = _write_spool_entry(spool_dir, attempts=replay.MAX_ATTEMPTS - 1)
    monkeypatch.setattr(replay.le, "_memoryd_commit", lambda *a, **k: (None, "still rejected"))
    outcome, _ = replay.replay_one(str(path))
    assert outcome == "expired"
    assert not path.exists()
    assert (spool_dir / "expired" / path.name).exists()


def test_replay_one_skips_an_entry_with_no_agent(spool_dir):
    path = _write_spool_entry(spool_dir, agent="")
    outcome, detail = replay.replay_one(str(path))
    assert outcome == "no-agent"
    assert path.exists()  # left in place, not lost


def test_main_reports_empty_spool(spool_dir, monkeypatch, capsys):
    monkeypatch.setattr(replay.le, "EPISODE_SPOOL_DIR", str(spool_dir))
    monkeypatch.setattr(sys, "argv", ["lumen_episode_spool_replay.py"])
    assert replay.main() == 0
    assert "spool empty" in capsys.readouterr().out


# ---- wq#3309: terminal state (recovered/) + terminality pre-check ----------

# wq#4559: was the literal "2026-09-29T00:00:00+00:00", which aged past
# MAX_AGE_DAYS 14 days after it was written and flipped every retry-path test
# to "expired". Relative keeps the invariant the comment always claimed:
# well past GRACE_HOURS, well under MAX_AGE_DAYS.
OLD_TS = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()


def _make_queue_db(path, sessions=(), retired=(), tasks=()):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS agent_sessions "
                 "(session_id TEXT PRIMARY KEY, last_seen TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS retired_sessions "
                 "(session_id TEXT PRIMARY KEY, base_name TEXT, retired_at TEXT, "
                 "kind TEXT, reason TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS tasks "
                 "(id INTEGER PRIMARY KEY, lane TEXT, claimed_by TEXT)")
    for s in sessions:
        conn.execute("INSERT INTO agent_sessions VALUES (?, '2026-10-01')", (s,))
    for s in retired:
        conn.execute("INSERT INTO retired_sessions VALUES (?, ?, '2026-10-01', 'k', 'r')",
                     (s, s))
    for tid, lane, claimant in tasks:
        conn.execute("INSERT INTO tasks VALUES (?,?,?)", (tid, lane, claimant))
    conn.commit()
    conn.close()


@pytest.fixture
def mem_db(monkeypatch, tmp_path):
    """Hermetic archival store: real note_exists/write_note_verified/audit code
    paths, tmp sqlite + tmp archival dir + tmp log."""
    db = tmp_path / "minerva.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE archival_notes (id TEXT PRIMARY KEY, kind TEXT, "
                 "title TEXT, content TEXT, embedding BLOB, tags TEXT, "
                 "source_ref TEXT, created_at TEXT, origin TEXT, trust TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(replay.mm, "DB", db)
    monkeypatch.setattr(replay.mm, "ARCHIVAL_DIR", tmp_path / "archival")
    monkeypatch.setattr(replay.mm, "LOG", tmp_path / "memory.log")
    return db


def _seed_local_note(db, kind="session", title="t", content="c"):
    nid = replay.mm.note_id(kind, title, content)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO archival_notes (id, kind, title, content) "
                 "VALUES (?,?,?,?)", (nid, kind, title, content))
    conn.commit()
    conn.close()
    return nid


def _audit_rows(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT invariant, detail FROM memory_audit").fetchall()
    except sqlite3.OperationalError:
        return []   # table never created == no audit row was ever written
    finally:
        conn.close()


@pytest.fixture
def no_post(monkeypatch):
    """A terminal entry must be recovered WITHOUT a POST."""
    def boom(*a, **k):
        raise AssertionError("POST must not happen for a terminal entry")
    monkeypatch.setattr(replay.le, "_memoryd_commit", boom)


@pytest.fixture
def reject_post(monkeypatch):
    monkeypatch.setattr(replay.le, "_memoryd_commit",
                        lambda *a, **k: (None, "agent does not hold a live claim on wq#3173"))


def test_purged_agent_with_local_copy_recovers_without_post(
        tmp_path, hermetic_queue_db, mem_db, no_post):
    # design §5.1: R1 -- registry readable, id absent (purged), content local.
    _make_queue_db(hermetic_queue_db)  # schema present, agent row absent
    nid = _seed_local_note(mem_db)
    path = _write_spool_entry(tmp_path / "spool", agent="wall-5-f539f2",
                              spooled_at=OLD_TS, attempts=32)
    outcome, detail = replay.replay_one(str(path))
    assert outcome == "recovered-local"
    assert not path.exists()
    moved = tmp_path / "spool" / "recovered" / path.name
    assert moved.exists()
    rec = json.loads(moved.read_text())
    assert rec["recovery"] == "local-only"
    assert rec["local_nid"] == nid
    assert "purged/retired" in rec["final_reason"]
    assert rec["recovered_at"]
    rows = _audit_rows(mem_db)
    assert len(rows) == 1
    assert rows[0][0] == "wq3309-recovered-local"
    assert "wall-5-f539f2" in rows[0][1]


def test_purged_agent_without_local_copy_gets_local_write(
        tmp_path, hermetic_queue_db, mem_db, no_post):
    # design §5.2: the spool held the only copy -- replayer writes it locally
    # through the existing fallback writer (NOT a daemon privilege), then recovers.
    _make_queue_db(hermetic_queue_db)
    path = _write_spool_entry(tmp_path / "spool", agent="mega-d-0961f0",
                              spooled_at=OLD_TS, attempts=4)
    outcome, _ = replay.replay_one(str(path))
    assert outcome == "recovered-local"
    rec = json.loads((tmp_path / "spool" / "recovered" / path.name).read_text())
    assert rec["recovery"] == "local-write"
    assert replay.mm.note_exists(rec["local_nid"])
    rows = _audit_rows(mem_db)
    assert len(rows) == 1
    assert rows[0][0] == "wq3309-recovered-local"
    assert "mega-d-0961f0" in rows[0][1]        # original agent in the audit detail
    assert "unreplayable" in rows[0][1]


def test_missing_source_ref_is_terminal_at_attempts_zero(
        tmp_path, hermetic_queue_db, mem_db, no_post):
    # design §5.3: R3 is immediate -- no grace window, no 200-attempt burn.
    _make_queue_db(hermetic_queue_db)
    _seed_local_note(mem_db)
    path = _write_spool_entry(tmp_path / "spool", agent="megac2-d7372d",
                              source_ref="", attempts=0,
                              spooled_at=datetime.now(timezone.utc).isoformat())
    outcome, _ = replay.replay_one(str(path))
    assert outcome == "recovered-local"
    rec = json.loads((tmp_path / "spool" / "recovered" / path.name).read_text())
    assert rec["attempts"] == 0
    assert "R3" in rec["final_reason"]


def test_still_registered_agent_is_not_recovered(
        tmp_path, hermetic_queue_db, mem_db, reject_post):
    # design §5.4, the can-fail probe (wq#2630): drawn from OUTSIDE the fix's
    # target population -- agent present in agent_sessions, local copy present,
    # so over-eager recovery WOULD trigger here and fail this test.
    _make_queue_db(hermetic_queue_db, sessions=["wall-5-alive99"])
    _seed_local_note(mem_db)
    path = _write_spool_entry(tmp_path / "spool", agent="wall-5-alive99",
                              spooled_at=OLD_TS, attempts=3)
    outcome, _ = replay.replay_one(str(path))
    assert outcome == "still-rejected"
    assert path.exists()
    assert not (tmp_path / "spool" / "recovered").exists()
    assert _audit_rows(mem_db) == []


def test_fworker_with_intact_claim_receipt_is_retried(
        tmp_path, hermetic_queue_db, mem_db, reject_post):
    # design §5.5a: claimed_by still names this fworker -- R4 cannot occur.
    _make_queue_db(hermetic_queue_db, tasks=[(77, "build", "fworker-wq77")])
    _seed_local_note(mem_db)
    path = _write_spool_entry(tmp_path / "spool", agent="fworker-wq77",
                              source_ref="wq#77", spooled_at=OLD_TS)
    outcome, _ = replay.replay_one(str(path))
    assert outcome == "still-rejected"
    assert path.exists()


def test_fworker_with_reclaimed_receipt_is_recovered(
        tmp_path, hermetic_queue_db, mem_db, no_post):
    # design §5.5b: the claim receipt was overwritten by a re-claim (survives
    # close, kernel.py ticket_claimant) -- it will never name this fworker again.
    _make_queue_db(hermetic_queue_db, tasks=[(77, "build", "orch-rescue-1")])
    _seed_local_note(mem_db)
    path = _write_spool_entry(tmp_path / "spool", agent="fworker-wq77",
                              source_ref="wq#77", spooled_at=OLD_TS)
    outcome, _ = replay.replay_one(str(path))
    assert outcome == "recovered-local"
    rec = json.loads((tmp_path / "spool" / "recovered" / path.name).read_text())
    assert "orch-rescue-1" in rec["final_reason"]


def test_unregistered_agent_inside_grace_window_is_retried(
        tmp_path, hermetic_queue_db, mem_db, reject_post):
    # design §5.6: a fresh entry may just be racing registration.
    _make_queue_db(hermetic_queue_db)
    _seed_local_note(mem_db)
    path = _write_spool_entry(tmp_path / "spool", agent="wall-5-newborn1",
                              spooled_at=datetime.now(timezone.utc).isoformat())
    outcome, _ = replay.replay_one(str(path))
    assert outcome == "still-rejected"
    assert path.exists()


def test_unreadable_queue_db_fails_open_to_retry(
        tmp_path, hermetic_queue_db, mem_db, reject_post):
    # design §5.7: hermetic_queue_db points at a path that does not exist.
    assert not hermetic_queue_db.exists()
    _seed_local_note(mem_db)
    path = _write_spool_entry(tmp_path / "spool", agent="wall-5-f539f2",
                              spooled_at=OLD_TS)
    outcome, _ = replay.replay_one(str(path))
    assert outcome == "still-rejected"
    assert path.exists()


def test_retry_budget_trimmed_to_96():
    # design §4: terminal classes exit eagerly, so MAX_ATTEMPTS no longer doubles
    # as the dead-identity backstop (~24 h at 15-min cadence, was ~50 h).
    assert replay.MAX_ATTEMPTS == 96


# ---- wq#4538 QW3: R2-terminal — does-not-hold that survives 3 replays -------

DOES_NOT_HOLD = "reject: agent does not hold a live claim on wq#4455,wq#4456"


def test_does_not_hold_after_three_replays_is_recovered(
        tmp_path, hermetic_queue_db, mem_db, no_post):
    # design wq#4538 test 4: registered non-fworker agent (so R1/R4 cannot fire),
    # list-form source_ref, attempts=3 with the daemon's does-not-hold reason —
    # post-wq#3352 the receipt was already consulted, so no replay can change it.
    _make_queue_db(hermetic_queue_db, sessions=["orch-main-aa11bb"])
    _seed_local_note(mem_db)
    path = _write_spool_entry(tmp_path / "spool", agent="orch-main-aa11bb",
                              source_ref="wq#4455,wq#4456", spooled_at=OLD_TS,
                              attempts=3, last_reason=DOES_NOT_HOLD)
    outcome, _ = replay.replay_one(str(path))
    assert outcome == "recovered-local"
    rec = json.loads((tmp_path / "spool" / "recovered" / path.name).read_text())
    assert "R2-terminal" in rec["final_reason"]
    assert rec["attempts"] == 3


def test_does_not_hold_at_two_attempts_is_still_retried(
        tmp_path, hermetic_queue_db, mem_db, reject_post):
    # design wq#4538 test 5 (wq#2630 can-fail): identical entry one replay short
    # of the threshold — over-eager recovery WOULD trigger here and fail this test.
    _make_queue_db(hermetic_queue_db, sessions=["orch-main-aa11bb"])
    _seed_local_note(mem_db)
    path = _write_spool_entry(tmp_path / "spool", agent="orch-main-aa11bb",
                              source_ref="wq#4455,wq#4456", spooled_at=OLD_TS,
                              attempts=2, last_reason=DOES_NOT_HOLD)
    outcome, _ = replay.replay_one(str(path))
    assert outcome == "still-rejected"
    assert path.exists()
    assert not (tmp_path / "spool" / "recovered").exists()


def test_transient_reason_at_high_attempts_is_still_retried(
        tmp_path, hermetic_queue_db, mem_db, reject_post):
    # design wq#4538 test 6 (can-fail): attempts past the threshold but a
    # transient reason class — R2-terminal must key on the reason, not the count.
    _make_queue_db(hermetic_queue_db, sessions=["orch-main-aa11bb"])
    _seed_local_note(mem_db)
    path = _write_spool_entry(tmp_path / "spool", agent="orch-main-aa11bb",
                              source_ref="wq#4455,wq#4456", spooled_at=OLD_TS,
                              attempts=5, last_reason="commit HTTP 503")
    outcome, _ = replay.replay_one(str(path))
    assert outcome == "still-rejected"
    assert path.exists()
    assert not (tmp_path / "spool" / "recovered").exists()


# ---- wq#4538 S2 client: fold unverifiable refs under the session anchor -----

@pytest.fixture
def capture_commit(monkeypatch):
    seen = {}

    def fake_commit(kind, title, content, tags, source_ref):
        seen.update(kind=kind, title=title, content=content,
                    tags=tags, source_ref=source_ref)
        return "memoryd-note-1", None

    monkeypatch.setattr(le, "_memoryd_commit", fake_commit)
    return seen


def test_commit_folds_range_ref_under_session_anchor(
        spool_dir, stub_mm, monkeypatch, hermetic_queue_db, capture_commit):
    # design wq#4538 test 7: an orch cites a range it shepherded; I-7 can only
    # verify the held anchor — ref moves to content/tags, source_ref = anchor.
    monkeypatch.setenv("WQ_SESSION_TICKET", "wq#9000")
    monkeypatch.setattr(le.minerva_audit, "agent_id", lambda: "orch-main-aa11bb")
    nid, reason = le.commit("shepherded a batch", session_ref="wq#4470-4474",
                            kind="session", embed=False)
    assert nid == "memoryd-note-1"
    assert capture_commit["source_ref"] == "wq#9000"
    assert "## Tickets\nwq#4470-4474" in capture_commit["content"]
    assert "tickets:wq#4470-4474" in capture_commit["tags"]


def test_commit_leaves_own_claimed_single_ref_alone(
        spool_dir, stub_mm, monkeypatch, hermetic_queue_db, capture_commit):
    # design wq#4538 test 7 can-fail: the fworker path must NOT be rewritten —
    # a single wq#N whose claim receipt names the committer goes through verbatim.
    _make_queue_db(hermetic_queue_db, tasks=[(3173, "build", "fworker-wq3173")])
    monkeypatch.setenv("WQ_SESSION_TICKET", "wq#9000")
    monkeypatch.setattr(le.minerva_audit, "agent_id", lambda: "fworker-wq3173")
    le.commit("did my ticket", session_ref="wq#3173", kind="session", embed=False)
    assert capture_commit["source_ref"] == "wq#3173"
    assert "## Tickets" not in capture_commit["content"]
    assert not any(t.startswith("tickets:") for t in capture_commit["tags"] or [])


def test_commit_folds_other_agents_single_ref(
        spool_dir, stub_mm, monkeypatch, hermetic_queue_db, capture_commit):
    # A single wq#N whose receipt names someone else is exactly the 314-a-week
    # reject class from the wq#4538 problem statement — folded under the anchor.
    _make_queue_db(hermetic_queue_db, tasks=[(4469, "build", "fworker-wq4469")])
    monkeypatch.setenv("WQ_SESSION_TICKET", "wq#9000")
    monkeypatch.setattr(le.minerva_audit, "agent_id", lambda: "orch-main-aa11bb")
    le.commit("shepherded one", session_ref="wq#4469", kind="session", embed=False)
    assert capture_commit["source_ref"] == "wq#9000"
    assert "## Tickets\nwq#4469" in capture_commit["content"]


def test_commit_without_anchor_is_unchanged(
        spool_dir, stub_mm, monkeypatch, hermetic_queue_db, capture_commit):
    # No WQ_SESSION_TICKET → today's behavior exactly (reject → fallback → spool).
    monkeypatch.setattr(le.minerva_audit, "agent_id", lambda: "orch-main-aa11bb")
    le.commit("shepherded a batch", session_ref="wq#4470-4474",
              kind="session", embed=False)
    assert capture_commit["source_ref"] == "wq#4470-4474"
    assert "## Tickets" not in capture_commit["content"]


def test_commit_stamps_local_nid_into_spool_entry(spool_dir, stub_mm, monkeypatch):
    # design §3: commit() stamps the fallback write's note id into the spool file
    # so replay can verify local durability without recomputing.
    monkeypatch.setattr(le, "_memoryd_commit",
                        lambda *a, **k: (None, "agent does not hold a live claim on wq#3173"))
    monkeypatch.setattr(le.minerva_audit, "agent_id", lambda: "fworker-wq3173")
    nid, _ = le.commit("did a thing", session_ref="wq#3173", kind="session", embed=False)
    assert nid == "local-note-1"
    files = list(spool_dir.glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["local_nid"] == "local-note-1"
