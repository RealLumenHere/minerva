"""state_reconcile (wq#1611) — "no tailscale_ip drift; store matches the wire"
may only print when something was actually compared.

No tailnet, no db: ts_status/current/known_subjects/ingest are stubbed.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import state_reconcile as sr  # noqa: E402


@pytest.fixture
def wire(monkeypatch):
    """Returns a setup(peers, facts) helper; yields the ingest calls it recorded."""
    calls = []
    monkeypatch.setattr(sr, "ingest", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(sys, "argv", ["state_reconcile.py", "--dry-run"])

    def setup(peers, facts, known=("hub", "leaf")):
        monkeypatch.setattr(sr, "known_subjects", lambda: set(known))
        monkeypatch.setattr(sr, "ts_status", lambda: peers)
        monkeypatch.setattr(sr, "current", lambda **kw: [
            {"subject": s, "predicate": "tailscale_ip", "value": v} for s, v in facts.items()])
    return setup, calls


def _run(capsys):
    sr.main()
    return capsys.readouterr().out


def test_everything_compared_and_matching_is_the_clean_line(wire, capsys):
    setup, _ = wire
    setup([("hub", "100.64.0.1", True), ("leaf", "100.64.0.3", True)],
          {"hub": "100.64.0.1", "leaf": "100.64.0.3"})
    out = _run(capsys)
    assert ("[DRY-RUN] coverage[state_reconcile:tailscale_ip]: checked 2 · matched 2"
            " · drifted 0 · unverifiable 0") in out
    assert "store matches the wire." in out


def test_a_subject_the_wire_never_mentioned_is_not_a_clean_run(wire, capsys):
    """The §2.3 bug: an offline node used to make the store look confirmed."""
    setup, _ = wire
    setup([("hub", "100.64.0.1", True)], {"hub": "100.64.0.1", "leaf": "100.64.0.3"})
    out = _run(capsys)
    assert "· unverifiable 1 (not-seen 1)" in out
    assert "store matches the wire." not in out
    assert "1 could not be checked - NOT a clean run." in out


def test_a_peer_with_no_stored_fact_is_unverifiable_not_matched(wire, capsys):
    setup, _ = wire
    setup([("hub", "100.64.0.1", True), ("leaf", "100.64.0.3", True)], {"hub": "100.64.0.1"})
    out = _run(capsys)
    assert "checked 2 · matched 1 · drifted 0 · unverifiable 1 (no-fact 1)" in out
    assert "NOT a clean run." in out


def test_drift_is_counted_and_still_reported(wire, capsys):
    setup, calls = wire
    setup([("hub", "100.64.0.9", True), ("leaf", "100.64.0.3", True)],
          {"hub": "100.64.0.1", "leaf": "100.64.0.3"})
    out = _run(capsys)
    assert "checked 2 · matched 1 · drifted 1 · unverifiable 0" in out
    assert "hub: 100.64.0.1 -> 100.64.0.9" in out
    assert calls == []  # --dry-run


def test_nothing_on_the_wire_reports_zero_checked(wire, capsys):
    setup, _ = wire
    setup([], {"hub": "100.64.0.1"})
    out = _run(capsys)
    assert "checked 1 · matched 0 · drifted 0 · unverifiable 1 (not-seen 1)" in out
    assert "store matches the wire." not in out


def test_unknown_peers_are_flagged_not_counted_as_coverage(wire, capsys):
    setup, _ = wire
    setup([("hub", "100.64.0.1", True), ("sekhmet", "100.64.0.7", True)], {"hub": "100.64.0.1"})
    out = _run(capsys)
    assert "checked 1 · matched 1 · drifted 0 · unverifiable 0" in out
    assert "sekhmet 100.64.0.7" in out
