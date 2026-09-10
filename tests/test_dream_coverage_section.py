"""lumen_dream (wq#1611) — the weekly report carries the coverage line too.

§2.3: the dream report's "None — prose IPs match the store" is the third place a
zero-finding scan reads as success. Section-rendering only; run() is not invoked.
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import lumen_dream as ld  # noqa: E402

NOW = {"facts": 836, "fresh": 3, "due": 2, "stale": 4, "unverifiable": 827, "contradicted": 0}
WEEK_AGO = dict(NOW, unverifiable=820)


def test_first_week_has_no_comparison():
    L = ld.coverage_section(NOW, None, None)
    assert L[0] == ("- facts 836 · fresh 3 · due 2 · stale 4 · unverifiable 827 · contradicted 0")
    assert "baseline recorded" in L[1]


def test_regression_is_marked_and_attributed_to_the_aq_row():
    L = ld.coverage_section(NOW, WEEK_AGO, 7)
    assert "stale+unverifiable 824 -> 831 (+7)" in L[1]
    assert "REGRESSED — aq row raised" in L[1]


def test_improvement_is_reported_without_the_alarm():
    L = ld.coverage_section(WEEK_AGO, NOW, -7)
    assert "(-7)" in L[1]
    assert "REGRESSED" not in L[1]


def test_a_broken_coverage_check_says_so_instead_of_printing_nothing():
    assert ld.coverage_section(None, None, None, 'OperationalError: locked') == [
        '- coverage FAILED: OperationalError: locked']
