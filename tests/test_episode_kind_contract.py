"""wq#2901: `lumen_episode.py commit --kind` must answer to the memory contract.

`--kind` took any string while memoryd enforces a closed set (I-4), so `verify`,
`investigation` and `bugfix` each became a 422 the caller never saw -- the note fell
back to an unattributed direct write and the operator learned nothing. Known synonyms
map onto the contract vocabulary; anything else is a loud CLI error.

Design: v4/research/fworker_lane_attribution.md D4.
"""
import os, sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CONTRACT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "v4", "schema", "memory_contract_v1.yaml")


@pytest.fixture
def le(monkeypatch):
    """Pin the contract to the checkout's own copy -- an unpinned read of the live
    file is not re-runnable evidence (wq#2630)."""
    monkeypatch.setenv("MEMORYD_CONTRACT", CONTRACT)
    import importlib
    import lumen_episode
    return importlib.reload(lumen_episode)


def test_contract_kinds_are_read_from_the_pinned_contract(le):
    kinds = le.contract_kinds()
    assert kinds and "code-ship" in kinds and "session" in kinds


def test_a_contract_kind_passes_through(le):
    assert le.resolve_kind("code-ship") == ("code-ship", None)


@pytest.mark.parametrize("given,expected", [
    ("verify", "audit"),            # observed live in memory_audit reject rows
    ("verification", "audit"),
    ("investigation", "analysis"),
    ("bugfix", "fix"),
    ("postmortem", "lesson"),
])
def test_known_synonyms_map_instead_of_failing(le, given, expected):
    kind, err = le.resolve_kind(given)
    assert (kind, err) == (expected, None)
    assert kind in le.contract_kinds()


def test_an_unmappable_kind_fails_loud_at_the_cli(le):
    kind, err = le.resolve_kind("interpretive-dance")
    assert kind == "interpretive-dance"
    assert err and "not in the memory contract" in err
    assert "code-ship" in err          # the message names what IS allowed


def test_an_unreadable_contract_fails_open(le, monkeypatch, tmp_path):
    """The CLI must not become a new way for an episode commit to die."""
    monkeypatch.setattr(le, "CONTRACT_PATH", str(tmp_path / "gone.yaml"))
    assert le.contract_kinds() is None
    assert le.resolve_kind("whatever-the-caller-said") == ("whatever-the-caller-said", None)


def test_reject_breadcrumb_counts_up_and_never_raises(le, monkeypatch, tmp_path):
    marker = tmp_path / "memoryd_reject.json"
    monkeypatch.setattr(le, "MEMORYD_REJECT_MARKER", str(marker))
    le._breadcrumb_memoryd_reject("commit HTTP 403", "fworker-wq2901", "wq#2901")
    le._breadcrumb_memoryd_reject("commit HTTP 403", "fworker-wq2901", "wq#2901")
    import json
    got = json.loads(marker.read_text())
    assert got["count"] == 2
    assert got["last_agent"] == "fworker-wq2901" and got["last_ref"] == "wq#2901"
    # unwritable path: best-effort, must not raise into the commit path
    monkeypatch.setattr(le, "MEMORYD_REJECT_MARKER", "/proc/nope/marker.json")
    le._breadcrumb_memoryd_reject("x", "y", "z")
