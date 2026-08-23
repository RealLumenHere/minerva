# wq#603a (654) — Federated tier-penalty sweep

**Agent:** claude-web-mv4-603-8d8a9e · **Lane:** memory · **Date:** 2026-08-23
**Golden:** `eval/golden_v4_frozen_20260814.jsonl` sha256 `8ba5afb0ce12585606b8dcff5ffb8245af24b0f0f32ccbddf3d4121f1bbf46b7` (frozen, 0444)
**Scoring:** wq#595 semantics (retrieval-only headline, trap penalty, abstention split)
**Verdict: NO penalty value clears the adopt rule. Recommendation: record loss, keep flag OFF.**

---

## 0. Problem and mechanism

From wq#573 §3: federated chunks score on `d+lex` only, while archival notes get
up to `.05·g + .02·ent` from the graph and entity-specificity channels. On-topic
prose therefore lands one slot above the correct note on the sorted list. The 573
report measured this as 5 headline classes down ≥2pts with federated ON.

This sweep tests a **tier penalty**: a fixed amount subtracted from each federated
fusion score at the point of scoring, compensating for the structural asymmetry.

```
fused = (W_D·d + W_LEX·lex + W_REC·rec) · FED_SCALE − FED_PENALTY
```

Code: `MEMORY_V4_FED_PENALTY` env var in `lumen_recall2.py` (default 0.0, no
production change). Backup: `lumen_recall2.py.bak.20260823_pre_wq603`.

## 1. Frontier table

Baseline = sched_baseline.json (flag OFF): R@1 .778 / MRR .836 / trap@1 .188 / abst .500.

| p | R@1 | R@5 | R@10 | MRR | fed R@1 | trap@1 | abst | Verdict |
|---|---|---|---|---|---|---|---|---|
| **0.00** (no penalty) | .749 | .900 | .933 | .814 | **.700** | .250 | .500 | **FAIL** — 5 classes, trap |
| 0.01 | .762 | .900 | .933 | .824 | .700 | .250 | .500 | FAIL — 3 classes, trap |
| 0.02 | .762 | .904 | .933 | .825 | .700 | .250 | .500 | FAIL — 3 classes, trap |
| 0.03 | .766 | .904 | .933 | .827 | .600 | .250 | .500 | FAIL — 2 classes, trap |
| 0.05 | .778 | .904 | .937 | .834 | .500 | .250 | .500 | FAIL — 1 class, trap |
| 0.08 | .778 | .904 | .937 | .834 | .300 | .250 | .500 | FAIL — 1 class, trap |

All values of p recover headline R@1 monotonically toward the baseline, but two
independent failures persist at every setting.

## 2. Per-class detail

Gate: no headline class may be ≥2pts below baseline (sched_baseline, flag OFF).

| class (n) | baseline | p=0.00 | p=0.01 | p=0.02 | p=0.03 | p=0.05 | p=0.08 |
|---|---|---|---|---|---|---|---|
| build (33) | .909 | .848 **−.061** | .879 −.030 | .879 −.030 | .879 −.030 | **.909** | **.909** |
| outage-rca (61) | .770 | .721 **−.049** | .754 −.016 | .754 −.016 | .754 −.016 | **.787** | **.787** |
| hardware (21) | .762 | .714 **−.048** | .714 −.048 | .714 −.048 | .762 | **.762** | **.762** |
| decision (31) | .839 | .806 **−.033** | **.839** | **.839** | **.839** | **.839** | **.839** |
| procedure (37) | .649 | .622 **−.027** | .622 **−.027** | .622 **−.027** | .622 **−.027** | .622 **−.027** | .622 **−.027** |
| gotcha (44) | .818 | .841 | .818 | .818 | .818 | .818 | .818 |
| contradiction (5) | .600 | .600 | .600 | .600 | .600 | .600 | .600 |
| knowledge-update (6) | .500 | .500 | .500 | .500 | .500 | .500 | .500 |
| milestone (1) | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |

**Procedure** is the stubborn class: −.027 at every penalty tested, including p=0.08
which crushes federated coverage to .300. At least one procedure query has a
federated chunk that outscores even with the maximum penalty.

The remaining 4 of the original 5 down-classes recover monotonically: decision at
p=0.01, outage-rca and build at p=0.05, hardware at p=0.03.

## 3. Why no penalty value passes

Two independent failures block adoption at every tested value:

**Failure 1 — trap@1 rises from .188 to .250 (universal).** This is NOT a
displacement issue — it is the trap penalty logic catching a stale or conflicting
federated chunk above the correct note on contradiction-resolution/knowledge-update
queries. The penalty subtracts from ALL federated scores equally, so it cannot
selectively suppress trap-carrying chunks without also suppressing the useful ones.
trap@1 is alarmed on ANY rise (wq#595 §2), so this alone disqualifies every arm.

**Failure 2 — procedure R@1 stays at .622 (−.027) at every penalty.** Even at
p=0.08, which reduces federated coverage from .700 to .300, at least one procedure
query has a federated chunk whose `d+lex` score is high enough to outrank the
correct note after the penalty. This chunk is topically correct (the prose covers
the same subject as the golden note), so the displacement is semantic, not noise —
and a uniform penalty cannot distinguish it from the useful coverage hits.

## 4. The tier-penalty mechanism is insufficient

The sweep proves that a fixed subtractive penalty is the wrong shape for this
problem:

1. It is ALL-OR-NOTHING per query. A penalty that clears procedure kills federated
   coverage (p=0.08 → .300). A penalty that preserves coverage (p=0.01 → .700)
   leaves 3 headline classes down.
2. It cannot address the trap@1 regression, which is orthogonal to the score gap.
3. The best aggregate point (p=0.05: R@1 .778, MRR .834, fed .500) still fails on
   both procedure and trap.

Alternative approaches that this result points toward but does NOT test:
- Per-query tier prior (requires a query classifier, which production lacks)
- Multiplicative reweight of g+ent channels (changes the adopted #574 weights)
- Trap-aware federated filtering (suppress federated chunks that match trap criteria)
- Accept procedure's −.027 as the cost of coverage and fix trap@1 separately

## 5. Recommendation for wq#655

**Record the loss, keep MEMORY_V4_FEDERATED default OFF, close.** Per the
adopt-or-record law (wq#576 precedent): no penalty value recovers the 5
down-classes to within tolerance while keeping trap@1 clean. The honest result
is that a uniform tier penalty is the wrong mechanism.

The `MEMORY_V4_FED_PENALTY` env var remains in `lumen_recall2.py` at default 0.0
(no production change, no cleanup needed). It is available for future experiments
that combine it with other approaches (e.g., trap-aware filtering + a moderate
penalty).

## 6. Artifacts

| artifact | path |
|---|---|
| sweep harness | `eval/wq603/sweep_tier_penalty.py` |
| results (all arms) | `eval/wq603/sweep_results_20260823_1321.json` |
| per-arm results | `eval/results_wq603_p{000,001,002,003,005,008}_20260823_13*.json` |
| code backup | `lumen_recall2.py.bak.20260823_pre_wq603` |
| this report | `v4/reports/603a_tier_prior_sweep.md` |

## 7. Runtime config (arm p=0.00, representative)

Golden sha256: `8ba5afb0ce12585606b8dcff5ffb8245af24b0f0f32ccbddf3d4121f1bbf46b7`
Engine: v3 (search_v3)
`MEMORY_V4_FEDERATED=1`, `MEMORY_V4_FED_PENALTY=<swept>`
All other env vars at adopted defaults (W_D=0.59, W_LEX=0.34, W_G=0.05, W_REC=0.00, ENTITY_W=0.02).
Host: lumen, minerva.db mtime stable across all arms.
