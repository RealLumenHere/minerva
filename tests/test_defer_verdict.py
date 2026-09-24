"""wq#2489: the reaper scores the WHOLE note. It used to slice to 6000 chars and read the
verdict-less head of a 6-10k note, so valid ADOPT/WATCH/SKIP notes were retired as formless."""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import minerva_defer_review as mrd  # noqa: E402

PID = "zz2489testpid"


def _long_note(verdict_line):
    return ("research prose line\n" * 500) + verdict_line + "\n"


@pytest.fixture(params=["contract", "inline"])
def mode(request, monkeypatch):
    if request.param == "inline":
        monkeypatch.setattr(mrd, "_CONTRACT", None)
    else:
        assert mrd._CONTRACT is not None, "output_contract failed to load"
    return request.param


def test_verdict_after_6000_chars_is_scored(mode):
    text = _long_note("RECOMMENDATION: ADOPT fits the box")
    assert len(text) > 9000
    assert mrd._verdict_from(text) == ("accept", "research says ADOPT: fits the box")


@pytest.mark.parametrize("word,status", [("ADOPT", "accept"), ("WATCH", "defer"), ("SKIP", "rejected_aged")])
def test_each_verdict_maps(mode, word, status):
    assert mrd._verdict_from("body\nRECOMMENDATION: %s\n" % word)[0] == status


def test_formless_note_is_rerun_never_a_retire(mode):
    assert mrd._verdict_from(_long_note("no contract line at all")) == ("rerun", "no RECOMMENDATION line")


@pytest.fixture
def reap_env(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    c = sqlite3.connect(db)
    c.execute("create table proposals (id text, status text, decision_reason text, "
              "decided_at text, title text, created_at text)")
    c.execute("insert into proposals (id, status, decision_reason) values (?, 'researching', '')", (PID,))
    c.commit()
    c.close()
    done = tmp_path / "done"
    done.mkdir()
    monkeypatch.setattr(mrd, "DB", str(db))
    monkeypatch.setattr(mrd, "PROMPTS", tmp_path)
    monkeypatch.setattr(mrd, "DONE", done)
    return db, done


def _status(db):
    c = sqlite3.connect(db)
    try:
        return c.execute("select status, decision_reason from proposals where id=?", (PID,)).fetchone()
    finally:
        c.close()


def test_reap_reads_verdict_at_end_of_long_note(reap_env):
    db, done = reap_env
    (done / (mrd.PREFIX + PID + "_1.md")).write_text(_long_note("RECOMMENDATION: ADOPT"))
    mrd.cmd_reap()
    status, why = _status(db)
    assert status == "accept" and "ADOPT" in why
    assert not list(done.glob("*.formfail"))


def test_reap_formless_note_is_moved_aside_not_retired(reap_env, monkeypatch):
    db, done = reap_env
    monkeypatch.setattr(mrd, "_rerun_direct", lambda prompt: None)   # gateway down
    monkeypatch.setattr(mrd, "build_prompt", lambda r: "p")
    (done / (mrd.PREFIX + PID + "_1.md")).write_text(_long_note("no verdict"))
    mrd.cmd_reap()
    status, _ = _status(db)
    assert status == "researching"
    assert len(list(done.glob("*.formfail"))) == 1
