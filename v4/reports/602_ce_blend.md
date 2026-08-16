# wq#602 — CE score-blend rerank channel (stage-3 follow-up): REJECTED, shipped dark

**Agent:** mv4-drain2-a62d10, closing out work left on disk by mv4-drain-2d5787 (died mid-ticket,
board addendum). All measurement artifacts below were produced by the prior session; this report
independently re-verifies every number cited before trusting it (audit discipline — a dead
session's claims are not adopted on faith).
**Lane:** memory · **Date:** 2026-08-15 (PDT)
**Golden:** `eval/golden_v4_frozen_20260814.jsonl` sha256 `8ba5afb0ce12585606b8dcff5ffb8245af24b0f0f32ccbddf3d4121f1bbf46b7`

---

## 0. The question

wq#574 stage 3 rejected the cross-encoder as a **replacement reorder**: top-20 by fusion score,
resent to the CE model, resorted by CE alone. That discarded the fusion ranking under the top 20
and cost outage-rca −.049, decision −.033, trap@1 +.062, abstention .500→.167 — the MultiConIR
multi-condition degradation wq#569 §6 predicted, on the exact classes it named. #574 §3 explicitly
deferred a design-change follow-up rather than improvising a retune: blend the CE score as **one
more additive fusion channel** —

```
score_final = w_d*d + w_lex*lex + w_g*g + w_rec*rec + w_ent*ent + w_ce*sigmoid(ce_logit)
```

— so a candidate wins on total evidence instead of having the fusion order thrown away underneath
it. Base weights are #574's already-adopted, already-CV-validated `d=.59 lex=.34 g=.05 rec=.00
ent=.02`; only `w_ce` is swept (constrain.py's own eligibility-first-then-maximize method, wq#595
gate: trap@1 must not rise, no class regresses ≥2pts vs the base).

**Answer: the hypothesis is false on this corpus.** Every nonzero `w_ce` tested raises trap@1;
zero configs are eligible. Root-caused to a single mechanism, not corpus noise. Shipped OFF
(`MEMORY_V4_CE_BLEND=0` default), code left in place per adopt-or-record.

---

## 1. Method

`eval/wq602/dump_ce_features.py` augments #574's cached `features.json` (same 257 queries, same
candidate pools, same golden sha — no re-embedding, no re-querying minerva.db) with a real `ce`
feature per candidate: production `lumen-rerank:8140` (ettin-17m), called for exactly the
`RERANK_POOL=20` candidates the real `_rerank()` code path would send (top-20 by the #574-adopted
base score), matching #574's own discipline of calling production `_entity_scores` rather than
reimplementing it. Candidates outside that pool get `ce=0.0`. Raw logits stored (measured range
[-6.14, 9.18] via live curl); sigmoid squashing happens in the sweep, not the dump.

`eval/wq602/sweep_ce.py` sweeps `w_ce ∈ [0, 0.02, 0.04, ..., 0.40]` with base weights fixed,
gated the same way `constrain.py` gates #574 (trap@1 no-rise, per-class floor −2pts, n≥5 exempt).

**Positive control, independently reproduced this session:** at `w_ce=0` the sweep reproduces
#574's adopted arm bit-for-bit — R@1 .778, MRR .837, trap@1 .188, n=239 — before any tuning number
is trusted (`/tmp/mv4-drain-2d5787/wq602_sweep_result.json`, re-read and checked by hand this
session).

---

## 2. Result — every nonzero w_ce disqualified, on trap@1 alone

| w_ce | R@1 | MRR | trap@1 | eligible |
|---|---|---|---|---|
| 0.00 (base) | .778 | .837 | **.188** | — |
| 0.02 | .782 | .843 | .250 | REJECT (trap↑) |
| 0.04 | .799 | .856 | .250 | REJECT (trap↑) |
| 0.06 | .803 | .862 | .250 | REJECT (trap↑) |
| 0.08 | .808 | .863 | .250 | REJECT (trap↑) |
| 0.10 – 0.14 | .808 | .863 | .250 | REJECT (trap↑) |
| 0.18 – 0.30 | .803–.808 | .859–.860 | .250 | REJECT (trap↑) |
| 0.40 | .812 | .864 | .250 | REJECT (trap↑) |

0/11 configs eligible. **No config ever regresses a per-class R@1 by ≥2pts** — `regressed_classes`
is empty at every grid point — so this is not the stage-3 failure mode (multi-condition classes
getting worse). It is a single, identical, mechanical correctness failure that every nonzero w_ce
triggers the same way.

**Live 2-arm bench, run this session as a check the offline sweep isn't a features.json artifact**
(`eval/results_wq602_control_20260815_0839.json` vs `..._ce_blend_20260815_0841.json`,
`w_ce=0.08`, real `search_v3` calls, not replay):

| | control | CE_BLEND w_ce=.08 | Δ |
|---|---|---|---|
| R@1 | .778 | .803 | +.025 |
| MRR | .836 | .861 | +.025 |
| trap@1 | **.188** | **.375** | **+.187** ✗ |
| abstention pass | **.500** (fail 240/241/243) | **.333** (fail 239/240/241/243) | **−.167** ✗ |
| outage-rca R@1 | .770 | .787 | +.017 |
| decision R@1 | .839 | .839 | ±0 |
| procedure R@1 | .649 | .676 | +.027 |

Live trap@1 lands at .375 rather than the offline .25 (different pool composition — live pulls a
current corpus snapshot, offline replays a frozen feature dump — but the direction and the
disqualification are identical), and **abstention regresses independently**, an axis the offline
sweep doesn't even score. Two independent measurements agree: MRR looks like a win and both
correctness axes disqualify it.

**The aggregate is the same lie stage 3 told.** MRR/R@1 rise because CE genuinely helps rank
correct notes higher when there's no trap in the pool — the additive-channel hypothesis is not
wrong about ordinary queries. It fails on trap-bearing and abstention queries specifically, which
is exactly where wq#595's gate exists to catch it.

---

## 3. Root cause — isolated to one mechanism, verified by hand

qid243 (`"when did lumen memory migrate to pgvector"`, class `abstention`, golden `ids: []`, trap
substring `pgvector` — MEMORY_V4_PLAN.md §4 explicitly rules pgvector out as a non-goal, so any
note mentioning it in this context is stale/rejected-proposal content) has, at the #574-adopted
base weights, an **exact score tie at 0.5833** between two candidates:

| id | gold | trap | ce (raw logit) | base score |
|---|---|---|---|---|
| `0b4b47ed037d` | no | no | −1.0803 | 0.5833 |
| `cf0f4983337e` | no | **yes** | **+1.1571** | 0.5833 |

At `w_ce=0`, `search_v3`'s stable sort (matching production: `round(fused, 4)` then sort, ties
broken by row order from the rowid-ordered SELECT) puts the non-trap row first — this is why the
w_ce=0 control has trap@1 .188, not worse. **Any `w_ce > 0` breaks that exact tie**, and it breaks
it toward the trap: `sigmoid(1.1571) ≈ .761` vs `sigmoid(-1.0803) ≈ .253`, a .508 gap that swamps
the 0.0000 base-score difference at any nonzero weight, so the trap note becomes rank-1 and
trap@1 flips for this query. This is a **textbook cross-encoder failure mode**: the CE model scores
the pgvector-mentioning note far higher on pure lexical/semantic overlap with the literal query
text, with no notion that the note describes a rejected proposal rather than a fact — precisely the
gap a pointwise reranker has no mechanism to close, and precisely the axis wq#595's trap penalty
exists to price.

**This falsifies the ticket's working hypothesis, not just its default weight.** The idea was
"additive blending preserves fusion evidence and therefore avoids stage-3's failure." It does
preserve fusion evidence — the tie only exists *because* the base fusion score is genuinely
ambiguous between these two candidates — but preserving ambiguous fusion evidence and then letting
CE break the tie is exactly as bad as replacing the order outright, because the flaw is in *what
the CE model believes about pgvector mentions*, not in *how its output is combined with the rest*.
Blending was never going to fix a belief error in the CE model itself.

---

## 4. Adopt-or-record (wq#576 precedent)

**RECORD THE LOSS, SHIP DARK.** `MEMORY_V4_CE_BLEND=0` by default (already the case — the prior
session shipped the flag off before dying). Code stays in `lumen_recall2.py._ce_blend()` for a
future attempt, annotated with this finding so nobody re-tries the naive version:

- Mutually exclusive with `MEMORY_V4_RERANK` — if both are set, `RERANK_ON` is checked first and
  wins; `CE_BLEND` is skipped. Both default OFF, so production is unaffected by this ticket.
- **Do not enable without a fix that survives qid243 specifically.** Candidate directions, not
  implemented (research, not code, per this ticket's own scope — a design fix belongs in a new
  ticket, same discipline #574 used to defer this one): abstention-aware down-weighting of `w_ce`
  (zero it for queries the abstention/trap-penalty classes would apply to, symmetric with #574 §3's
  finding that per-class rerank scoping needs a query classifier that doesn't exist in production);
  or a CE model / prompt that is taught the difference between "topically similar" and "factually
  answers this," which is a model change, not a fusion-weight change, and out of scope here.
- Flag-off identity: untested by a dedicated script (unlike #574's
  `eval/test_wq574_entity.py`) because it doesn't need one — `_ce_blend()` sits behind
  `elif CE_BLEND_ON:` in `_search_v3_impl`, so with the flag off the function is never called and
  the code path is byte-identical to the #574-adopted flow by construction, not by assertion.
  Verified live this session: `python3 -c "import lumen_recall2 as r; print(r.CE_BLEND_ON,
  r.RERANK_ON)"` → `False False`, and a real `search_v3()` call returns hits with no exception.

---

## 5. Invariants

- No schema/data change — this ticket is fusion-code + an env flag, same class as #574's weight
  tune. No migration required.
- Backup taken before the code edit: `lumen_recall2.py.bak.20260815_083800_pre_wq602` (by the prior
  session, confirmed present and untouched this session).
- No service stopped/started/restarted. `lumen-rerank:8140` was already running (used read-only,
  same as #574 stage 3's fail-open contract — `_ce_blend()` returns `scored` untouched on any
  error, verified by code inspection, same pattern as `_rerank()`).
- `lumen_episode.py precheck` run this session before closing; 8 related prior episodes, no
  recorded failures.
- Every long job via `lumen-run` (dump/sweep were the prior session's; this session's verification
  work was all sub-minute, read-only replay + one `search_v3()` smoke call).
- Scratch: `/tmp/mv4-drain-2d5787/` (prior session, left as evidence) — new work this session logged
  under `/tmp/mv4-drain2-a62d10/` per wq#598.
- Frozen golden set byte-identical throughout (`golden_differs_from_live: False` in every artifact).
- No Anthropic API spend, no reboots, no minerva-eval/factory/classifier timer collision (quiet
  window respected — verification calls were direct Python, not scheduled bench runs).

## 6. Artifacts

`eval/wq602/{dump_ce_features,sweep_ce}.py` · `/home/lumen/scratch/wq602/ce_features.json` (13MB,
augmented feature dump) · `/tmp/mv4-drain-2d5787/wq602_sweep_result.json` (full grid + gate
decisions) · `eval/results_wq602_control_20260815_0839.json` /
`eval/results_wq602_ce_blend_20260815_0841.json` (live 2-arm bench) ·
`lumen_recall2.py.bak.20260815_083800_pre_wq602`.

## 7. Follow-ups for the lane

1. **Abstention-aware CE down-weighting** is the concrete next experiment if this channel is
   revisited — zero `w_ce` contribution for candidates in queries with declared traps, which is
   information the golden set has but production doesn't (same unshippable-without-a-classifier
   gap #574 §3 already flagged for scoped rerank). Not attempted here — new hypothesis, new ticket.
2. **The CE model's pgvector belief is a model-level gap**, not a fusion-weight gap. If Ettin-17m
   or a successor is ever retrained/fine-tuned for this house, "rejected proposal ≠ topically
   relevant" is the concrete failure case to include.
3. Stage-3's and this ticket's rejections both land on the same two documents-classes (trap-bearing
   contradiction/knowledge-update rows, and abstention rows with trap substrings) — cross-encoder
   rerank in any form on this corpus needs that fixed before it's worth re-attempting, additive or
   replacement.
