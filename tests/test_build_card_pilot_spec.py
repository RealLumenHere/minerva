"""wq#2709 — build_card() wires into foreman/harness/pilot_spec_author (wq#2690 half 2).

Regression: wq#1933/1953/2124/2654 parked because the card generator left the five
spec sections (Hypothesis / Primary metric / Fixed sample / Baseline / Treatment) as
template comments. build_card() must now hand the rendered card to
pilot_spec_author.fill_card() before returning, so every generated card is either
fully spec'd or carries the loud DO-NOT-RUN banner -- never silently blank.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, '/home/lumen/foreman/harness')
import minerva_defer_review as mrd  # noqa: E402
import pilot_spec_author as psa  # noqa: E402

PROPOSAL = {
    'id': 'zz2709testpid',
    'title': 'try a smaller judge rung',
    'decision_reason': 'we do not know until we measure it',
    'current_state': 'judging runs on the current rung today',
    'proposed_change': 'swap to a cheaper rung for judge calls',
    'impact': 'M',
    'effort': 'S',
    'risk': 'L',
    'created_at': '2026-08-01T00:00:00+00:00',
}

GOOD_SPEC = {
    'hypothesis': (
        'deepseek-v4-flash matches the current judge rung on verdict agreement within '
        '3 percent while cutting median judge latency by at least 40 percent.'),
    'primary_metric': (
        'Median judge latency in ms per note. Win: treatment median <= 60 percent of '
        'baseline median with verdict agreement >= 97 percent.'),
    'fixed_sample': (
        '/home/lumen/foreman/pilots/fixtures/judge_notes.jsonl, 400 notes, sha256 '
        'recorded in REPORT.md.'),
    'baseline': 'python3 pilots/judge_bench.py --rung current --out baseline.json',
    'treatment': 'python3 pilots/judge_bench.py --rung deepseek-v4-flash --out treatment.json',
}


def _stub_call(reply):
    def call(prompt):
        return reply
    return call


@pytest.fixture
def patched_gateway(monkeypatch):
    """Swap gateway_call for a stub so build_card never hits the real LiteLLM gateway.

    build_card() does `import pilot_spec_author` at call time, which binds to the
    same cached module object as `psa` here (same sys.modules entry), so patching
    psa.gateway_call is visible to build_card without touching mrd's internals.
    """
    def _apply(reply):
        monkeypatch.setattr(psa, 'gateway_call', lambda *a, **k: _stub_call(reply))
    return _apply


def test_build_card_ships_a_fully_specced_card(patched_gateway):
    import json
    patched_gateway(json.dumps(GOOD_SPEC))
    card = mrd.build_card(PROPOSAL)
    assert psa.unfilled_sections(card) == []
    assert 'SPEC AUTHORING FAILED' not in card


def test_build_card_ships_loud_banner_when_spec_authoring_fails(patched_gateway):
    patched_gateway(None)
    card = mrd.build_card(PROPOSAL)
    assert 'SPEC AUTHORING FAILED' in card
    assert 'DO NOT RUN' in card
