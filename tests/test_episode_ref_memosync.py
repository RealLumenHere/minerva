"""wq#5013: memoryd I-7 ref normalisation + MemoSync post-turn audit in lumen_episode."""
import json
import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import lumen_episode as le  # noqa: E402


@pytest.fixture
def queue_db(monkeypatch, tmp_path):
    db = tmp_path / "queue.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, claimed_by TEXT)")
    c.executemany("INSERT INTO tasks VALUES (?, ?)",
                  [(5104, "orch-a"), (5105, "orch-a"), (5111, "orch-b"), (4882, "fworker-wq4882")])
    c.commit(); c.close()
    monkeypatch.setattr(le, "QUEUE_DB", str(db))
    monkeypatch.delenv("WQ_SESSION_TICKET", raising=False)
    return db


# ── normalize_ref ───────────────────────────────────────────────────────────

def test_fworker_without_ref_gets_its_own_ticket(queue_db):
    assert le.normalize_ref("", "fworker-wq4882") == ("wq#4882", None)


def test_fworker_with_ref_is_untouched(queue_db):
    assert le.normalize_ref("wq#4882", "fworker-wq4882") == ("wq#4882", None)


def test_multi_ref_anchors_on_first_held_ticket(queue_db):
    ref, extra = le.normalize_ref("wq#5111,wq#5104,wq#5105", "orch-a")
    assert ref == "wq#5104"
    assert extra == "wq#5111,wq#5104,wq#5105"


def test_multi_ref_with_spaces_and_bare_numbers(queue_db):
    assert le.normalize_ref("wq#5104 wq#5105", "orch-a")[0] == "wq#5104"
    assert le.normalize_ref("wq5105, wq5104", "orch-a")[0] == "wq#5105"


def test_multi_ref_none_held_passes_through(queue_db):
    assert le.normalize_ref("wq#5104,wq#5105", "orch-z") == ("wq#5104,wq#5105", None)


def test_single_ref_and_non_wq_refs_pass_through(queue_db):
    assert le.normalize_ref("wq#5104", "orch-z") == ("wq#5104", None)
    assert le.normalize_ref("backfill:house", "orch-a") == ("backfill:house", None)
    assert le.normalize_ref("", "orch-a") == ("", None)


def test_unreadable_queue_db_fails_open(monkeypatch):
    monkeypatch.setattr(le, "QUEUE_DB", "/nonexistent/queue.db")
    assert le.normalize_ref("wq#1,wq#2", "orch-a") == ("wq#1,wq#2", None)


def test_commit_sends_normalized_ref_to_memoryd(queue_db, monkeypatch):
    sent = {}

    class Stop(Exception):
        pass

    def fake_memoryd(kind, title, content, tags, source_ref):
        sent.update(ref=source_ref, content=content, tags=tags)
        raise Stop()

    monkeypatch.setattr(le, "_memoryd_commit", fake_memoryd)
    monkeypatch.setattr(le.minerva_audit, "agent_id", lambda: "orch-a")
    with pytest.raises(Stop):
        le.commit("s", session_ref="wq#5111,wq#5105")
    assert sent["ref"] == "wq#5105"
    assert "## Tickets\nwq#5111,wq#5105" in sent["content"]
    assert "tickets:wq#5111,wq#5105" in sent["tags"]


# ── MemoSync audit ──────────────────────────────────────────────────────────

def test_parse_memo_ok():
    items = le.parse_memo("M-1b3c9e84c8cd=shaped; M-d4c48e6033c0=violated: had to restart it")
    assert items == [("1b3c9e84c8cd", "shaped", ""), ("d4c48e6033c0", "violated", "had to restart it")]


@pytest.mark.parametrize("bad", ["M-1b3c9e84c8cd=ignored", "1b3c9e84c8cd=shaped", "M-xyz=shaped"])
def test_parse_memo_rejects_bad_items(bad):
    with pytest.raises(le.MemoError):
        le.parse_memo(bad)


def test_violated_without_reason_refused():
    _, _, refuse, _ = le.memo_audit([("aaaaaa", "violated", "")], {})
    assert refuse


def test_violated_with_reason_warns_and_records():
    block, tags, refuse, warn = le.memo_audit([("aaaaaa", "violated", "Frank approved")], {})
    assert not refuse and any("VIOLATED" in w for w in warn)
    assert "M-aaaaaa: violated -- Frank approved" in block and "memo:violated" in tags


def test_lesson_with_no_visible_effect_and_no_reason_warns():
    surfaced = {"bbbbbb": {"kind": "lesson"}}
    _, _, refuse, warn = le.memo_audit([("bbbbbb", "no-visible-effect", "")], surfaced)
    assert not refuse and any("lesson memory" in w for w in warn)


def test_unaudited_surfaced_memories_warn():
    surfaced = {"aaaaaa": {"kind": "session"}, "cccccc": {"kind": "outage"}}
    _, _, _, warn = le.memo_audit([("aaaaaa", "shaped", "")], surfaced)
    assert any("M-cccccc" in w for w in warn)


def test_clean_audit_has_no_findings():
    surfaced = {"aaaaaa": {"kind": "session"}}
    block, tags, refuse, warn = le.memo_audit([("aaaaaa", "shaped", "")], surfaced)
    assert (refuse, warn) == ([], []) and tags == ["memo:shaped"]


def test_surfaced_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(le, "PRECHECK_CACHE_DIR", str(tmp_path))
    le.remember_surfaced([{"id": "abc123", "kind": "Lesson", "title": "t"}, {"title": "no id"}], "orch-a")
    assert le.surfaced("orch-a") == {"abc123": {"ts": pytest.approx(le.surfaced("orch-a")["abc123"]["ts"]),
                                                "kind": "lesson", "title": "t"}}
    assert le.surfaced("orch-b") == {}
    le.forget_surfaced("orch-a")
    assert le.surfaced("orch-a") == {}


class _Stop(Exception):
    pass


def test_commit_content_carries_memo_block(monkeypatch):
    """The memoryd stub raises after capturing, so nothing reaches any store."""
    sent = {}

    def fake_memoryd(kind, title, content, tags, source_ref):
        sent.update(content=content, tags=tags)
        raise _Stop()

    monkeypatch.setattr(le, "_memoryd_commit", fake_memoryd)
    monkeypatch.setattr(le.minerva_audit, "agent_id", lambda: "")
    with pytest.raises(_Stop):
        le.commit("s", memo_block="\n## Memory audit (MemoSync, wq#5013)\n- M-aa: shaped\n",
                  memo_tags=["memo:shaped"])
    assert "## Memory audit (MemoSync, wq#5013)" in sent["content"]
    assert "memo:shaped" in sent["tags"]
