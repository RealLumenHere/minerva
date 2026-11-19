"""wq#5028: the wq#5008 taxonomy ledger is surfaced read-only in `current` and the coverage line."""
import hashlib, json, os, sqlite3, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lumen_state as ls
import lumen_coverage as lc


def _h(v):
    return hashlib.sha256(v.encode()).hexdigest()


def _ledger(tmp_path, rows, summary=None, ts=None):
    p = tmp_path / "tax.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE fact_taxonomy (fact_id TEXT, subject TEXT, predicate TEXT, value_sha256 TEXT,"
              " status TEXT, scope TEXT, admission TEXT, issues TEXT, blocked_by TEXT, proof_ref TEXT,"
              " checks TEXT, probe TEXT, checked_at REAL)")
    c.execute("CREATE TABLE taxonomy_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, summary TEXT, engines TEXT)")
    for r in rows:
        c.execute("INSERT INTO fact_taxonomy (subject,predicate,value_sha256,status,scope,admission,issues,blocked_by)"
                  " VALUES (?,?,?,?,?,?,?,?)", r)
    if summary is not None:
        c.execute("INSERT INTO taxonomy_runs (ts, summary) VALUES (?,?)", (ts or time.time(), json.dumps(summary)))
    c.commit(); c.close()
    return str(p)


def _row(subj, pred, val, ev="[UNVERIFIABLE — no probe]"):
    return {"subject": subj, "predicate": pred, "value": val, "evidence": ev}


def test_blocked_reads_differently_from_unverifiable(tmp_path, monkeypatch):
    monkeypatch.delenv("LUMEN_NO_TAXONOMY", raising=False)
    db = _ledger(tmp_path, [
        ("a", "status", _h("UP"), "BLOCKED", None, "ADMIT", "[]",
         "probe: probe service_state last returned 'unknown' at 2026-10-07T06:00:34+00:00"),
        ("b", "kernel", _h("6.1"), "UNVERIFIABLE", None, "ADMIT", "[]", None),
        ("c", "nic", _h("eth0"), "VERIFIED", "world", "DENY", '["stale_evidence"]', None),
        ("d", "lan_ip", _h("10.0.0.1"), "VERIFIED", "formal", "ADMIT", "[]", None),
    ])
    rows = [_row("a", "status", "UP"), _row("b", "kernel", "6.1"),
            _row("c", "nic", "eth0", "[STALE — 55d, probe node_identity]"), _row("d", "lan_ip", "10.0.0.1")]
    ls.overlay_taxonomy(rows, ls.load_taxonomy(db))
    assert rows[0]["evidence"] == "[BLOCKED — probe service_state: unknown since 2026-10-07]"
    assert rows[1]["evidence"] == "[UNVERIFIABLE — no probe]"
    assert rows[2]["evidence"].endswith("[DENY: stale_evidence]")
    assert "formal ✓" in rows[3]["evidence"] and rows[3]["taxonomy"]["scope"] == "formal"


def test_stale_ledger_row_never_mislabels(tmp_path, monkeypatch):
    monkeypatch.delenv("LUMEN_NO_TAXONOMY", raising=False)
    db = _ledger(tmp_path, [("a", "status", _h("OLD"), "BLOCKED", None, "DENY", '["x"]', "probe: z")])
    rows = [_row("a", "status", "NEW")]
    ls.overlay_taxonomy(rows, ls.load_taxonomy(db))
    assert rows[0]["evidence"] == "[UNVERIFIABLE — no probe]" and "taxonomy" not in rows[0]


def test_missing_ledger_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("LUMEN_NO_TAXONOMY", raising=False)
    assert ls.load_taxonomy(str(tmp_path / "nope.db")) == {}
    assert lc.taxonomy_line(str(tmp_path / "nope.db")) is None


def test_overlay_never_writes_last_confirmed(tmp_path, monkeypatch):
    monkeypatch.delenv("LUMEN_NO_TAXONOMY", raising=False)
    db = _ledger(tmp_path, [("d", "lan_ip", _h("10.0.0.1"), "VERIFIED", "formal", "ADMIT", "[]", None)])
    r = _row("d", "lan_ip", "10.0.0.1"); r["last_confirmed_at"] = None
    ls.overlay_taxonomy([r], ls.load_taxonomy(db))
    assert r["last_confirmed_at"] is None


def test_coverage_line_counts_and_staleness(tmp_path, monkeypatch):
    monkeypatch.delenv("LUMEN_NO_TAXONOMY", raising=False)
    s = {"status": {"BLOCKED": 7, "UNVERIFIABLE": 9, "VERIFIED": 5}, "verified_scope": {"world": 3, "formal": 2},
         "admission": {"DENY": 1}}
    fresh = lc.taxonomy_line(_ledger(tmp_path, [], s))
    assert fresh.startswith("taxonomy: verified 3 world / 2 formal · blocked 7 · unverifiable 9 · deny 1 (scan 0h old")
    old_dir = tmp_path / "o"; old_dir.mkdir()
    assert "STALE" in lc.taxonomy_line(_ledger(old_dir, [], s, ts=time.time() - 5 * 86400))
