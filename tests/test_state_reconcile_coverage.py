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


# ── wq#1608: the live (non-dry-run) path must never compare-and-skip ────────

@pytest.fixture
def live_wire(monkeypatch):
    """Same as `wire`, but drives the real (non-dry-run) path and records
    report_observation() calls instead of stubbing ingest() -- this is the
    path the old code silently skipped on a match."""
    calls = []

    def fake_report_observation(subject, predicate, observed, probe, **kw):
        calls.append((subject, predicate, observed, probe))
        return {'outcome': 'confirmed', 'subject': subject, 'predicate': predicate}

    monkeypatch.setattr(sr, "report_observation", fake_report_observation)
    monkeypatch.setattr(sys, "argv", ["state_reconcile.py"])  # no --dry-run

    def setup(peers, facts, known=("hub", "leaf")):
        monkeypatch.setattr(sr, "known_subjects", lambda: set(known))
        monkeypatch.setattr(sr, "ts_status", lambda: peers)
        monkeypatch.setattr(sr, "current", lambda **kw: [
            {"subject": s, "predicate": "tailscale_ip", "value": v} for s, v in facts.items()])
    return setup, calls


def test_a_match_still_reports_the_observation_when_not_dry_run(live_wire, capsys):
    """The wq#1608 bug, pinned: a wire value equal to the stored value must
    still be reported (so ingest() can bump last_confirmed_at) instead of
    being discarded with a bare `continue`."""
    setup, calls = live_wire
    setup([("hub", "100.64.0.1", True), ("leaf", "100.64.0.3", True)],
          {"hub": "100.64.0.1", "leaf": "100.64.0.3"})
    sr.main()
    assert ("hub", "tailscale_ip", "100.64.0.1", "tailscale_ip") in calls
    assert ("leaf", "tailscale_ip", "100.64.0.3", "tailscale_ip") in calls


def test_a_drift_is_also_reported_not_compared_locally(live_wire, capsys):
    setup, calls = live_wire
    setup([("hub", "100.64.0.9", True)], {"hub": "100.64.0.1"})
    sr.main()
    assert ("hub", "tailscale_ip", "100.64.0.9", "tailscale_ip") in calls


def test_frank_stated_conflict_is_surfaced_not_silently_applied(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["state_reconcile.py"])
    monkeypatch.setattr(sr, "known_subjects", lambda: {"hub"})
    monkeypatch.setattr(sr, "ts_status", lambda: [("hub", "100.64.0.9", True)])
    monkeypatch.setattr(sr, "current", lambda **kw: [
        {"subject": "hub", "predicate": "tailscale_ip", "value": "100.64.0.1"}])
    monkeypatch.setattr(sr, "report_observation", lambda *a, **k: {
        'outcome': 'unknown', 'conflict': True, 'stored': '100.64.0.1'})
    out = _run(capsys)
    assert "FRANK-STATED CONFLICTS" in out
    assert "hub: store=100.64.0.1 wire=100.64.0.9" in out
