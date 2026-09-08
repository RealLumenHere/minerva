"""minerva_promote (wq#1535) — ticket bodies only cite worker-readable paths.

No live db, no wq: the proposal/plan rows are dicts and the publish root is a tmp
dir. The sweep module is the real harness/read_dir_sweep.py (so a masked token really
is below every pattern's floor).
"""
import os
import re
import sys
from pathlib import Path

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import minerva_promote as mp  # noqa: E402

PROP = {"id": "abcdef012345", "title": "Upgrade Glance to v0.8.6", "status": "staged",
        "created_at": "2026-09-03T18:45", "source_intel_id": None, "current_state": "runs glance",
        "proposed_change": "bump", "effort": "S", "impact": "M", "risk": "S", "rollback": "revert",
        "eval_result": '{"judge_score": 1.0, "passed": true}', "wq_ticket": None}
PLAN = {"id": "0123456789ab", "proposal_id": PROP["id"], "lane": "yellow", "change_type": "service_change",
        "status": "staged", "created_at": "x", "applied_at": None, "targets": "[]",
        "gate_result": '{"lane_reasons": ["plannable_not_whitelisted:service_change"]}',
        "notes": "n", "params": "{}", "apply_cmd": "docker pull x", "health_cmd": "curl x",
        "rollback_cmd": "docker run old", "draft_diff": "api_key: sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"}

ALLOWED = ("/home/lumen/proposals", "/home/lumen/instructions/approvals/F")


class Row(dict):
    def keys(self):
        return list(super().keys())


@pytest.fixture(autouse=True)
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(mp, "PUBLISH_ROOT", tmp_path)
    monkeypatch.setattr(mp, "_md_path", lambda pid: None)
    monkeypatch.setattr(mp, "_eval_scores", lambda prop, c=None: {"passed": True})
    return tmp_path


def test_publish_masks_secret_shaped_tokens(root):
    out = mp.publish(Row(PROP), Row(PLAN))
    assert out["masked"] >= 1
    txt = Path(out["plan"]).read_text()
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123" not in txt
    assert "sk-A…" in txt
    sweep = mp._sweep_module()
    for _name, rx in sweep.PATTERNS:
        assert not rx.search(txt), _name   # the daily sweep can never re-fire on our output


def test_publish_is_fail_closed_without_the_sweep(root, monkeypatch):
    monkeypatch.setattr(mp, "_sweep_module", lambda: None)
    out = mp.publish(Row(PROP), Row(PLAN))
    assert out == {"proposal": None, "plan": None, "masked": 0}
    assert list(root.iterdir()) == []


def test_body_cites_only_granted_roots(root):
    """The lint invariant: every /home/lumen path in a promoter body is under the
    published root or is the approvals closure artifact. minerva.db, the factory's
    apply_cmd/targets and /home/lumen/bin must never appear."""
    body = mp._detail(Row(PROP), None, Row(PLAN), "factory-yellow", "glanceapp/glance")
    body = body.replace(str(root), "/home/lumen/proposals")
    for p in re.findall(r"/home/lumen/[\w./~-]+", body):
        assert p.startswith(ALLOWED), p
    assert "minerva.db" not in body and "docker pull x" not in body
    assert "F0123456789ab.approved" in body and "--approved" in body
    assert "minerva-factory close abcdef012345 --applied" in body


def test_red_body_has_no_approval_and_a_close_verb(root):
    plan = Row({**PLAN, "lane": "red", "change_type": "unplannable"})
    body = mp._detail(Row(PROP), None, plan, "factory-red")
    assert ".approved" not in body and "RED:" in body
    assert "minerva-factory close abcdef012345" in body


def test_planless_body_still_closes_by_verb(root):
    body = mp._detail(Row(PROP), None, None, "factory-unplanned")
    assert "sqlite3" not in body and "minerva-factory close abcdef012345" in body
