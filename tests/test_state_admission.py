"""wq#5006: six-point admission gate for `lumen_state.py ingest` (CLI path)."""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import state_admission as sa  # noqa: E402

GOOD_SRC = "wq#5006 ssh verify 2026-11-19"


def levels(findings):
    return {(lvl, crit) for lvl, crit, _ in findings}


# ── good facts pass clean ────────────────────────────────────────────────────

@pytest.mark.parametrize("subject,predicate,value,source", [
    ("thor", "lan_ip", "192.168.8.132", "netplan static IP assignment, wq#4919"),
    ("ptah", "availability", "online", "ssh probe, wq#4953"),
    ("horus", "hardware", "Seneca USFS-05-V2, i3-1115G4, 8GB DDR4", "Frank stated 2026-11-12"),
    ("litellm", "bind", "127.0.0.1:4000 + 172.17.0.1:4000",
     "http://100.121.17.5:3000/lumen/claude-foreman/pulls/2674"),
])
def test_good_facts_pass(subject, predicate, value, source):
    assert sa.check(subject, predicate, value, source=source) == []


# ── criterion 4: traceable ───────────────────────────────────────────────────

def test_missing_source_refused():
    assert ("REFUSE", "4-traceable") in levels(sa.check("thor", "lan_ip", "192.168.8.132"))


def test_source_that_cites_nothing_refused():
    assert ("REFUSE", "4-traceable") in levels(
        sa.check("thor", "lan_ip", "192.168.8.132", source="manual"))


# ── criterion 5: working language ────────────────────────────────────────────

@pytest.mark.parametrize("value", [
    "token=" + "abcdef1234567890",
    "key is sk-ant-" + "api03-AAAAAAAAAAAAAAAAAAAA",
    "ghp_" + "A" * 30,
])
def test_secret_material_refused(value):
    assert ("REFUSE", "5-working-language") in levels(sa.check("x", "y", value, source=GOOD_SRC))


def test_secret_in_note_refused():
    f = sa.check("x", "y", "rotated", source=GOOD_SRC, note="password: " + "hunter2hunter2")
    assert ("REFUSE", "5-working-language") in levels(f)


@pytest.mark.parametrize("value", ["TBD", "unknown", "<placeholder>", "???", "ip is TODO"])
def test_placeholder_refused(value):
    assert ("REFUSE", "5-working-language") in levels(sa.check("x", "y", value, source=GOOD_SRC))


# ── criteria 1-3: warnings, still written ────────────────────────────────────

def test_transient_reading_warns():
    f = sa.check("ptah", "availability", "online, up 2d19h, load average 0.15", source=GOOD_SRC)
    assert ("WARN", "1-durable") in levels(f)
    assert not any(lvl == "REFUSE" for lvl, _, _ in f)


def test_transient_with_half_life_is_accepted():
    assert sa.check("ptah", "load", "load average 0.15", source=GOOD_SRC, half_life=1.0) == []


def test_code_warns():
    f = sa.check("x", "y", "```\ndef f():\n    pass\n```", source=GOOD_SRC)
    assert ("WARN", "2-not-reconstructable") in levels(f)


def test_oversized_value_warns():
    f = sa.check("x", "y", "decided A because " + "b" * 500, source=GOOD_SRC)
    assert ("WARN", "3-decision-level") in levels(f)


# ── criterion 6: explicit reversals ──────────────────────────────────────────

def test_reversal_without_why_warns():
    f = sa.check("thor", "lan_ip", "192.168.8.140", source=GOOD_SRC, prior_value="192.168.8.132")
    assert ("WARN", "6-explicit-reversal") in levels(f)


def test_reversal_without_why_refused_when_strict():
    f = sa.check("thor", "lan_ip", "192.168.8.140", source=GOOD_SRC,
                 prior_value="192.168.8.132", strict=True)
    assert ("REFUSE", "6-explicit-reversal") in levels(f)


def test_reversal_with_why_passes():
    assert sa.check("thor", "lan_ip", "192.168.8.140", source=GOOD_SRC,
                    prior_value="192.168.8.132", why="netplan moved to static .140") == []


def test_probe_reobservation_is_not_a_reversal():
    assert sa.check("thor", "lan_ip", "192.168.8.140", source=GOOD_SRC,
                    prior_value="192.168.8.132", source_kind="observed") == []


def test_same_value_is_not_a_reversal():
    assert sa.check("thor", "lan_ip", "192.168.8.132", source=GOOD_SRC,
                    prior_value="192.168.8.132") == []


# ── CLI wiring ──────────────────────────────────────────────────────────────

@pytest.fixture
def cli(tmp_path, monkeypatch):
    import lumen_state as ls
    monkeypatch.setattr(ls, "DB", str(tmp_path / "state.db"))
    monkeypatch.setattr(sa, "ADMISSION_LOG", str(tmp_path / "admission.jsonl"))
    ls.init_schema()

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["lumen_state.py", "ingest", *argv])
        try:
            ls._main()
            return 0
        except SystemExit as e:
            return e.code or 0
    return ls, run, tmp_path


def test_cli_refuses_and_writes_nothing(cli, capsys):
    ls, run, tmp = cli
    assert run("thor", "lan_ip", "192.168.8.132") == 3
    assert "REFUSED by admission gate" in capsys.readouterr().err
    assert ls.current("thor", "lan_ip") == []
    rec = json.loads((tmp / "admission.jsonl").read_text().splitlines()[-1])
    assert rec["decision"] == "refused"


def test_cli_accepts_good_fact(cli):
    ls, run, _ = cli
    assert run("thor", "lan_ip", "192.168.8.132", "--source", "wq#4919 netplan") == 0
    assert ls.current("thor", "lan_ip")[0]["value"] == "192.168.8.132"


def test_cli_override_writes_on_the_record(cli):
    ls, run, tmp = cli
    assert run("thor", "lan_ip", "192.168.8.132", "--admit-override", "bootstrap import") == 0
    assert ls.current("thor", "lan_ip")[0]["value"] == "192.168.8.132"
    rec = json.loads((tmp / "admission.jsonl").read_text().splitlines()[-1])
    assert rec["decision"] == "overridden" and rec["override"] == "bootstrap import"


def test_cli_reversal_warns_then_writes_with_why_in_source(cli, capsys):
    ls, run, _ = cli
    assert run("thor", "lan_ip", "192.168.8.132", "--source", "wq#4919 netplan") == 0
    assert run("thor", "lan_ip", "192.168.8.140", "--source", "wq#4919 netplan",
               "--why", "moved to static .140") == 0
    row = ls.current("thor", "lan_ip")[0]
    assert row["value"] == "192.168.8.140"
    assert "why: moved to static .140" in row["source_ref"]
    assert "6-explicit-reversal" not in capsys.readouterr().err
