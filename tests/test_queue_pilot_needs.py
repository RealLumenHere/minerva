"""wq#2688 (session 2026-09-25): _queue_pilot() opened pilot tickets with no needs:
tag, so fworker.resolve_exec_grant never minted a grant and every ssh/fleet-exec call
to the card's named node was denied (grant_missing_env / requires approval) --
documented and never fixed across ISSUE-LOG #257/#265/#337/#369/#397/#430/#663/#666/#778.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import minerva_defer_review as mrd  # noqa: E402

PID = "zz2688testpid"


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="added task 4242 [pilot]\n", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def pilot_env(tmp_path, monkeypatch):
    db_path = tmp_path / "m.db"
    c = sqlite3.connect(db_path)
    c.execute("create table proposals (id text, status text, decision_reason text, "
              "title text, current_state text, proposed_change text, impact text, "
              "effort text, risk text, created_at text)")
    c.execute("insert into proposals (id, status, title, decision_reason, current_state, "
              "proposed_change, impact, effort, risk, created_at) values "
              "(?, 'defer', 'a pilot proposal', 'needs a benchmark first', 'state', "
              "'change', 'M', 'S', 'M', '2026-09-25')", (PID,))
    c.commit()
    c.close()
    monkeypatch.setattr(mrd, "DB", str(db_path))
    monkeypatch.setattr(mrd, "PILOTS", tmp_path / "pilots")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _FakeCompleted()

    monkeypatch.setattr(mrd.subprocess, "run", fake_run)
    return calls


def _row(pid=PID):
    return {
        "id": pid, "title": "a pilot proposal", "decision_reason": "needs a benchmark first",
        "current_state": "state", "proposed_change": "change", "impact": "M",
        "effort": "S", "risk": "M", "created_at": "2026-09-25",
    }


def test_queue_pilot_passes_needs_ssh_node(pilot_env):
    calls = pilot_env
    tid = mrd._queue_pilot(_row())
    assert tid == "4242"
    assert len(calls) == 1
    argv = calls[0]
    assert "--needs" in argv, "wq add must carry a needs: tag or fworker can never reach the node"
    needs_val = argv[argv.index("--needs") + 1]
    assert needs_val == f"ssh({mrd.PILOT_NODE})"


def test_queue_pilot_needs_node_is_configurable(pilot_env, monkeypatch):
    monkeypatch.setattr(mrd, "PILOT_NODE", "isis")
    calls = pilot_env
    mrd._queue_pilot(_row())
    argv = calls[0]
    assert argv[argv.index("--needs") + 1] == "ssh(isis)"
