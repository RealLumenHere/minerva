"""wq#3173: memoryd's identity gate rejects ~33/day (agent not registered / no live
claim on source_ref / missing source_ref -- daemon.py resolve_identity, I-7). The gate
is correct; the client silently ate the reject. This covers the fix: the CLI now exits
non-zero with the reason as the first line, the rejected payload is spooled to
EPISODE_SPOOL_DIR, and lumen_episode_spool_replay.py drains that spool.
"""
import io
import json
import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lumen_episode as le
import lumen_episode_spool_replay as replay


@pytest.fixture
def spool_dir(monkeypatch, tmp_path):
    d = tmp_path / "spool"
    monkeypatch.setattr(le, "EPISODE_SPOOL_DIR", str(d))
    return d


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
        "spooled_at": "2026-09-29T00:00:00+00:00", "attempts": 0,
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
