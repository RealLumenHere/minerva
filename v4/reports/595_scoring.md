# wq#595 — Golden v4 scoring semantics (SHIPPED 2026-08-14)
Agent: mv4-w595-07ae4c · lane memory · blocks #573/#574 adopt measurement.

## Headline change (same corpus, same golden — scoring only)
| Scoring | n | R@1 | R@5 | R@10 | MRR |
|---|---|---|---|---|---|
| wq#572 (old: abstention-as-rank-1, federated in, no trap penalty) | 255 | .694 | .839 | .886 | .757 |
| **wq#595 (new: retrieval-only headline, trap penalty)** | **239** | **.728** | **.874** | **.921** | **.793** |

Attribution is proven, not asserted: the new bench embeds a `legacy_wq572_headline_ALL_SCORED`
block that reimplements the old rule, and I ran the **backed-up old script** against the same
corpus in the same window — `.694/.839/.886/.757` **byte-identical** to the legacy block
(results_wq595_oldscoring_20260814_2159.json vs results_wq595_new_20260814_2158.json).
So the whole +.034 R@1 / +.036 MRR is scoring, zero retrieval.

Separate note: the 19:46 artifact of record read .690, not .694. That .004 is **corpus**, not
scoring — minerva.db moved 19:37Z → 21:58Z under other lanes and 3 queries shifted
(qid157 8→miss, qid165 2→3, qid255 2→1). Traps unchanged. Stated here so #573/#574 do not
re-derive it.

## New axes (each its own number, none of them in the headline)
- **abstention** pass-rate `.500` (n=6, fail qids 240/241/243) — a correct refusal is no longer
  injected as a rank-1 retrieval hit (fq#559). 3 of 6 rows were inflating the old headline.
- **federated coverage** R@1 `.000` (n=10: prose-section 7, session-archive 3) — the #573 target,
  reported separately so landing it registers as coverage, not as headline drift.
- **trap@1** `.250` (4 of 16 trap-bearing rows: 230, 238, 241, 243) — first-class and alarmed.
- **orig_193** R@1 `.720` / MRR `.790` (n=193, sha256-pinned subset) vs V3's 193q `.725/.795`.
  Verified against recall_golden.jsonl.bak.20260814_pre_wq572: all 193 rows byte-identical,
  qids 1..193, zero changed fields. So that delta is **corpus growth, not regression** — the
  same queries over a corpus that has grown ~1,128 notes and keeps moving.

## Trap penalty (fq#560 — Frank's correctness axis)
For `contradiction-resolution` and `knowledge-update` only: a trap ranked **above** the correct
note is now a **MISS**, not a hit. Penalized qids 230, 231, 235, 238.
- contradiction-resolution .600/.800/1.000 MRR .683 → **.600/.600/.600 MRR .600**
- knowledge-update .333/.833/.833 MRR .533 → **.333/.667/.667 MRR .500**
Those two classes were previously being paid for serving the stale note first. qid230 (stale
scout-outage over its closure, trap@1 correct@4) and qid238 (Bes Grafana over power-dash,
trap@1 correct@5) now score zero, which is what "stale DETECTED, not served" has to mean.
Also fixed a latent matching bug: hit/trap were an `if/elif` over the same position, so a doc
matching both could be counted as the trap. Expected now wins the tie.

## Frozen snapshot (blocks #574 per tail_techniques.md §5.1)
- `eval/golden_v4_frozen_20260814.jsonl`, mode 0444 (a frozen set you can edit is not frozen).
- sha256 `8ba5afb0ce12585606b8dcff5ffb8245af24b0f0f32ccbddf3d4121f1bbf46b7` (identical to live today).
- `recall_bench.py --freeze` emits it; `--golden <path>` scores against it. **Every** artifact now
  records `golden_path`, `golden_sha256`, `golden_is_frozen_snapshot`, `golden_live_sha256`,
  `golden_differs_from_live`, and `orig_193_sha256`.
- The scheduled bench is pinned to the newest frozen snapshot, so the nightly series is
  reproducible even as the live set grows. #574 tunes against this file and cites its sha256.

## Drift alarm — rewritten and PROVEN RED
`run_sched_bench.sh` now compares the **retrieval-only** headline (top-level R@1/MRR mirror
`summary.headline`), so #573 federated coverage cannot trip a false drift. Added two correctness
alarms — trap@1 on **any** increase and abstention pass-rate on **any** drop (retrieval is
deterministic here; a change is a real change, not noise). p95 still never alarms (decision C wq#567).
Added a comparability gate: if `scoring_semantics` or `golden_sha256` differs from the baseline,
it fires `baseline-stale` and **skips** the comparison rather than comparing across sets.

Negative controls run this session — not a clean sweep, a sweep that went red first:
1. **baseline-stale** fired unprompted on the first run against the old wq#572 baseline → aq#572,
   comparison correctly skipped. Resolved.
2. **drift**: doctored baseline (R@1/MRR .90, trap@1 0.0, abstention 1.0) → aq#573 fired all four
   axes in one message: `retrieval R@1 drift 0.9->0.728; retrieval MRR drift 0.9->0.793; trap@1
   ROSE 0.0->0.25 (qids [230,238,241,243]); abstention pass-rate FELL 1.0->0.5 (qids [240,241,243])`.
   Real baseline restored, aq#573 resolved, doctored artifact deleted.
3. Clean re-run after restore: `no drift` + both positive controls GREEN, exit 0. aq recall-bench rows: 0.

## State
- Baseline of record re-seeded: **eval/results_sched_20260814_2206.json** → `eval/sched_baseline.json`
  (R@1 .728 / R@5 .874 / R@10 .921 / MRR .793 / p50 169ms, trap@1 .250, abstention .500,
  federated .000, orig193 .720/.790, control_ok true, golden 8ba5afb0…, scoring `wq595`).
- Timer `lumen-recall-bench.timer` untouched and enabled, next fire Sat 2026-08-15 10:40 UTC.
- Backups: `recall_bench.py.bak.20260814_pre_wq595`, `recall_golden.jsonl.bak.20260814_pre_wq595`,
  `run_sched_bench.sh.bak.20260814_pre_wq595`, `sched_baseline.json.bak.20260814_pre_wq595` (md5-verified).
- Golden set itself **unmodified** (byte-identical to its pre-wq595 backup) — this ticket changed
  scoring only. No services stopped, no DB writes, no reboots, no Anthropic API spend.

## For #573 / #574
- Adopt against `summary.headline` (retrieval-only) per class, plus `summary.federated_coverage`
  for #573's own win condition. Do **not** read the top-level number as the old .690 series.
- #574 must pass `--golden /home/lumen/minerva/eval/golden_v4_frozen_20260814.jsonl` and cite
  sha256 8ba5afb0… in every arm's artifact; the control arm's effective runtime config is already
  embedded automatically.
- trap@1 `.250` is the current floor to beat — the rerank/entity work should drive it toward 0,
  and any arm that raises it is disqualified regardless of MRR.
