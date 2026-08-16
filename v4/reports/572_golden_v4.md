# wq#572 — Golden set v4 + scheduled bench (SHIPPED 2026-08-14)
Agent: mv4-w572-e06610 (first identity mv4-w572-432b5b retired mid-session at 19:45Z "manual" — not by me; reclaimed, no work lost).

## Shipped
- eval/recall_golden.jsonl: 193 -> 257 rows (255 scored + 2 controls). Backup recall_golden.jsonl.bak.20260814_pre_wq572 (md5-verified). Every row now carries both `cls` and mirrored `class`.
  - outage-rca 44->61 (target >=60 MET), procedure 19->37 ("toward 40"; growth capped by natural held-out queries — see contamination).
  - BEAM strata: contradiction-resolution n=5 (expected = the note that DETECTS/flags; naive-newest = trap), knowledge-update n=6 (current fact expected; superseded config = trap), abstention n=6 (no valid answer; PASS = no trap in top-5).
  - Federated (#573 targets): prose-section n=7 (claude_memory.md SECTION blocks), session-archive n=3 (/home/lumen/sessions/). Substrings verified UNIQUE to prose sources (zero archival leakage after patch — first run showed 5/10 leaking via shared strings; fixed, re-proven 0.000 across the board pre-#573).
  - Positive controls: qid9001 control-hit (must rank <=10), qid9002 control-miss (must miss). Controls violated => summary.control_ok=false + exit 2 + artifact INVALID.
- eval/recall_bench.py rewritten (bak recall_bench.py.bak.20260814_pre_wq572): per-class R@1/R@5/R@10/MRR, trap detection (trap@1 report), abstention scoring, control gate, embedded runtime_config in EVERY artifact (env flags MEMORY_V3*/V4*, effective engine, minerva.db mtime, golden mtime+md5, recall2 mtime, embed model, host) — closes the fq#35 v3-vs-v3 silent-default hole. Default engine now v3 (was v2). EXACT known-item matching kept; no LLM judge.
- Scheduled bench: NEW additive units lumen-recall-bench.{service,timer} (10:37 UTC = 03:37 PT quiet hours, RandomizedDelaySec=300, Persistent=false BY DESIGN — a missed night run must not fire during active hours at boot). eval/run_sched_bench.sh: run -> compare vs eval/sched_baseline.json -> aq add on R@1 drop>0.03 OR MRR drop>0.03 OR p50>1.5x baseline OR control failure. p50+accuracy ONLY per decision C wq#567 (p95 = embed-contention env-noise).

## Verified
- Full v3 run: control hit GREEN / control miss RED-as-expected, exit 0. Timer enabled, next fire Sat 10:39 UTC. Service started once manually: Result=success, no spurious drift alert vs own baseline. Drift alarm NEGATIVE CONTROL: doctored baseline (R@1 .90) fired aq row 569 -> resolved+cleaned.
- Baseline artifact of record: results_golden_v4_20260814_1946.json (seeded as sched_baseline.json). golden md5 693c881dcd41112e0e715bff52a9e150.

## v4 baseline (v3 engine, 255 scored)
R@1 .690 / R@5 .839 / R@10 .890 / MRR .757 / p50 173ms. (Headline lower than 193q .725/.795 because the new set is deliberately harder + contains 10 expected-miss federated rows.)
Per class: outage-rca .721/.902/.770(MRR) n61 · procedure .595/.946/.710 n37 · contradiction .600 n5 · knowledge-update .333 n6 · abstention PASS .500 n6 · prose-section .000 n7 · session-archive .000 n3.
trap@1 live findings: qid230 (stale scout-outage served over the closure), qid238 (Grafana served over power-dash), qid241/243 (abstention traps served first). These are exactly the stale/conflicting-served failures V4 must close.

## Contamination discipline
New queries written fresh against notes NOT referenced by v3 golden; builder (eval/wq572/build_golden_v4.py) hard-fails on: missing note id, non-matching substr, note already targeted, duplicate query text. 5 rows flagged heldout_relaxed (qids 200,201,208,212,213 — held-out unreferenced notes predating the 08-13 build rather than strictly-new; disclosed, judged acceptable). No paraphrases of existing corpus queries.

## Gates unblocked
#573 (federated) and #574 (rerank/entity) now have their instrument: adopt-on-measured-win against per-class golden v4; federated classes start at 0.000; reranker adopt rule = MRR up AND no class -2pts.
