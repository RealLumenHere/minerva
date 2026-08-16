# wq#574 — Tail attack: entity channel → fusion weights → rerank (UNBUNDLED)

**Agent:** mv4-w574-25aa93 · **Lane:** memory · **Date:** 2026-08-14 (PDT)
**Golden:** `eval/golden_v4_frozen_20260814.jsonl` sha256 `8ba5afb0ce12585606b8dcff5ffb8245af24b0f0f32ccbddf3d4121f1bbf46b7`
(frozen, 0444, identical to live at run time — every arm records it)

---

## 0. Stage table — the whole ticket in one view

| Stage | Arm | R@1 | MRR | trap@1 | p50 | Verdict |
|---|---|---|---|---|---|---|
| — | **control** (V3 weights, entity off) | .732 | .795 | .250 | 170ms | baseline |
| 1 | entity alone, §3.1 formula, w=0.10 | .644 | .722 | .250 | 178ms | **LOSS** |
| 1 | entity alone, abs-idf, w=0.04 | .678 | .754 | .250 | 178ms | **LOSS** |
| 1 | entity alone, abs-idf, w=0.08 | .649 | .725 | .250 | 177ms | **LOSS** |
| 2 | **tuned weights + entity w=0.02** | **.778** | **.837** | **.188** | 175ms | **ADOPT** |
| 3 | tuned + cross-encoder rerank top-20 | .824 | .871 | **.250** | 339ms | **REJECT** |

**Flag state now:** `MEMORY_V4_ENTITY` **ON** (w 0.02) · weights **0.59d / 0.34lex / 0.05g / 0.00rec** ·
`MEMORY_V4_RERANK` **OFF**. All six env-overridable; V3 behaviour recoverable without a code change.

Baseline note (fq F1): the control was **re-measured this session**, not taken from #595. It reads
**.732/.795**, not the recorded .728/.793 — minerva.db moved under other lanes. Everything below gates
against my own number.

---

## 1. Stage 1 — entity channel alone: LOST, twice, and the loss was informative

Attempt 1 implemented wq#569 §3.1 verbatim and cost **−.088 R@1 / −.073 MRR**, with `decision`
down **.290**. That is far too much damage for a 0.10 additive term, so it was decomposed rather
than retuned.

**The defect is in §3.1's formula.** It normalises by the query's own total idf:

```
ent = Σ_matched idf(e)·type_w(e)  /  Σ_{e∈E(q)} idf(e)·type_w(e)
```

On a single-entity query — the common case — the denominator *is* the numerator, so `ent` = 1.0
whether the entity is `backup` (df 127) or `enp0s25` (df 1). The normaliser cancels exactly the
specificity the channel exists to capture, and hands a flat `+w` to every one of the ~127 notes
carrying the common anchor. Attempt 2 divides by a **constant** ceiling `idf_max = ln(N/2)` instead,
so `backup` contributes 0.35·w and a df==1 anchor contributes 1.0·w.

That fixed the mechanism and shrank the loss monotonically with w (−.054 at w=0.04, −.083 at w=0.08)
but never crossed zero. Both attempts spent; recorded as a loss per the stop-rule.

**The real finding: the entity channel is not an independent signal on this corpus.** It is a second
positive vote for the same anchors the `graph` channel already rewards. Adding it at fixed V3 weights
is effectively raising `w_g`, which drowns dense. This is why it had to be measured alone before
tuning — and why stage 2 changes the verdict.

Coverage ceiling from #569 held up exactly as predicted: only 33% of outage-rca and 19% of procedure
queries name any entity, so no version of this channel was ever going to move procedure much.

---

## 2. Stage 2 — fusion weights: ADOPT (and it is where the tail actually moved)

Full-set evaluation via the bench costs ~45s per arm, so the search runs over a cached feature dump
(`eval/wq574/dump_features.py` → 257 queries × per-candidate `d/lex/g/rec/ent` + `gold`/`trap` labels,
`ent` computed by calling **production** `_entity_scores`, not a re-implementation).

### 2.1 The harness had to prove itself first — and it went RED twice

Per audit discipline, the replay is not trusted until it reproduces the live gate instrument.

1. **RED #1 — trap@1 .267 vs bench .250.** My denominator counted queries whose *pool contained* a
   trap; `recall_bench` counts queries that *declare* traps. qid244's trap doc (`ollama-embed-warm`)
   never enters the pool, so 4/15 vs 4/16. Fixed by reading `traps` from the golden file.
2. **RED #2 — trap@1 .250 replay vs .188 live**, found by checking the stage-2 winner against its own
   live arm. `search_v3` stores `round(fused, 4)` and sorts on the **rounded** value; the replay sorted
   full precision. On qid243 two candidates tie at 0.5833 after rounding, and the tie-break flipped a
   trap between rank 1 and 2. Fixed by rounding before the sort.

Post-fix the replay reproduces **both** live arms exactly — headline R@1/R@5/R@10/MRR, n, and trap@1 —
so the tuner and the gate measure the same thing. The unconstrained search was re-run after the fix and
returned the same winner with *stronger* fold agreement (3/5 → 4/5).

### 2.2 The gate belongs inside the search, not after it

The unconstrained optimum (d .73 / lex .22 / g 0 / rec .03 / ent .02) posts MRR **.844** — and is
**disqualified**: it raises trap@1 .250 → .375. wq#595 is explicit that any arm raising trap@1 is out
regardless of MRR, because serving the stale note first is a correctness failure, not a recall
trade-off. Optimising MRR and checking the gate afterwards finds the wrong optimum, so `constrain.py`
filters to gate-passing configs **first**, then maximises held-out MRR within them: 1120 configs → **64
eligible** (314 rejected for raising trap@1, 742 for regressing a class).

### 2.3 Winner — live-verified arm `wq574_s2_tuned`

**`d=0.59  lex=0.34  g=0.05  rec=0.00  ent=0.02`**

| | control | tuned | Δ |
|---|---|---|---|
| R@1 | .732 | **.778** | +.046 |
| R@5 | .874 | .912 | +.038 |
| MRR | .795 | **.837** | +.042 |
| trap@1 | .250 | **.188** | −.062 (better) |
| orig_193 MRR | .789 | .836 | +.047 |
| p50 | 170ms | 175ms | +5ms |

Per class, **nothing regresses**: build +.091, decision +.097, gotcha +.023, hardware ±0,
**outage-rca +.049**, **procedure +.027**, contradiction-resolution / knowledge-update flat.

**Generalisation:** 5-fold stratified CV by class, config picked on train folds only — held-out MRR
.7946 → **.8287, +.0341, permutation p = .0024**, 29 queries improved / 11 worsened. 4/5 folds pick
this exact config. orig_193 rising in step (+.047) says this is not an artifact of set growth.

*Honest caveat, also recorded in the artifact:* eligibility (trap@1, per-class floor) is evaluated on
the full frozen set, so config **eligibility** uses full-set information; only the MRR **ranking** is
cross-validated. trap@1 has n=16 and a per-class floor is a hard gate, not a tuned objective — neither
is a quantity CV can protect.

### 2.4 Two results worth reading as findings, not just numbers

- **Recency tuned to zero.** Freshness was costing recall, not buying it. `w_rec=0.06` was a guess that
  never paid.
- **Graph fell .16 → .05 while entity rose 0 → .02.** Both halves of the same finding: the flat
  anchor-*type* bonus is the weak form of the signal, idf-weighted anchor evidence is the strong form.
  And this is what flips stage 1's verdict — **entity is load-bearing inside the tuned set**. Dropping it
  (d .59 / lex .34 / g .05 / rec 0 / **ent 0**) regresses hardware R@1 **−.048** and gives procedure back,
  disqualifying that arm. It loses alone and wins in company.

### 2.5 The adopt is in the code, and proven to be

Defaults changed in `lumen_recall2.py`. Arm `wq574_s4_adopted_defaults` ran with **every
`MEMORY_V4_*` env var unset** (`env_flags: {(none set): defaults active}`) and reproduced the tuned arm
exactly — R@1 .778 / MRR .837 / trap@1 .188 / abstention .500 / orig193 .836, controls GREEN. The win is
in production behaviour, not in a shell script.

---

## 3. Stage 3 — cross-encoder rerank: REJECTED, exactly as #569 §6 predicted

Wired as the wq#388 kit pattern into `search_v3` after fusion: top-20 → `lumen-rerank:8140`
(ettin-17m, threads pinned 16), 5s timeout, **fail-open**. Fail-open proven, not assumed — pointed at a
dead port it still returns 10 hits in fusion order (`applied=False, ConnectionError`).

| vs adopted tuned baseline | tuned | +rerank | |
|---|---|---|---|
| MRR | .837 | .871 | +.034 |
| R@1 | .778 | .824 | +.046 |
| **outage-rca R@1** | .770 | **.721** | **−.049** ✗ |
| **decision R@1** | .839 | **.806** | **−.033** ✗ |
| **trap@1** | .188 | **.250** | **+.062** ✗ |
| **abstention pass** | .500 | **.167** | **−.333** ✗ |
| p50 | 175ms | 339ms | +164ms |

**The aggregate is a lie here.** MRR up .034 while the two hardest correctness axes go the wrong way:
the cross-encoder pulls stale/trap notes *up*, which is the precise failure the trap penalty exists to
catch, and abstention collapses to 1-in-6. outage-rca — one of the two target tails — regresses. This is
the MultiConIR multi-condition degradation #569 §6 flagged, on the exact class it named.

**Per-class scoping is not available as a fallback.** `MEMORY_V4_RERANK_CLASSES` is implemented, but
production has **no query classifier** — nothing can tell `search_v3` a live query's class, so an arm
scoped by golden-set labels would measure something unshippable. Rerank is therefore **skipped
entirely**, per the dispatch's own instruction, with the code left in place and flagged OFF.

Latency, for the record: 339ms p50 measured on a quiet-ish box. Per #596 §4.2 the honest price is the
**contended** number, so a rerank-on path should be budgeted well above that — it was never close to
worth it here.

**Next experiment (new ticket, not a retune):** CE as a *blended channel* rather than a replacement
ordering, so fusion evidence is not discarded wholesale. That is a design change and was deliberately
not improvised at 01:00.

---

## 4. Invariants

- `lumen_episode.py precheck` before any change; **episode committed** after.
- Backups: `lumen_recall2.py.bak.20260814_pre_wq574` (md5-verified) ·
  `/home/lumen/backups/minerva.db.pre_wq574_20260814`.
- **No service was stopped, started, or restarted.** No resident process holds `lumen_recall2` — every
  consumer (`lumen_episode`, `memory_embed_guard`, `fa_backfill`, reflector, factory) is a short-lived
  CLI/timer invocation, so the adopt is live immediately and there is nothing to restore.
- No DB writes beyond `lumen_state.py ingest` + `state-regen`. No reboots. No Anthropic API spend.
- Every long job via `lumen-run`. Scratch under `/tmp/mv4-w574-25aa93/` and `/home/lumen/scratch/wq574/`.
- Positive controls green in every artifact; `control-miss` stayed missed throughout.
- Flag-off identity asserted against the backup module, not assumed (`eval/test_wq574_entity.py`).

## 5. Artifacts

`eval/results_wq574_s0_preedit_control_*` · `s1a_off` · `s1b_ent010` · `s1c_absidf_w004/w008` ·
`s2_tuned` · `s3_rerank` · `s4_adopted_defaults` (all with embedded effective runtime config)
`eval/wq574/{dump_features,sweep,constrain}.py` · `eval/test_wq574_entity.py` · `eval/cmp_wq574.py`
`/home/lumen/scratch/wq574/{features,sweep_result,constrained_result}.json`

## 6. Reproduce

```
ssh lumen@100.117.66.102 'cd /home/lumen/minerva/eval && python3 recall_bench.py \
  --label repro --golden /home/lumen/minerva/eval/golden_v4_frozen_20260814.jsonl'
ssh lumen@100.117.66.102 'python3 /home/lumen/minerva/eval/wq574/constrain.py \
  /home/lumen/minerva/eval/results_wq574_s1a_off_20260815_0054.json'
```

## 7. Follow-ups for the lane

1. **Re-baseline `eval/sched_baseline.json`** — the nightly drift alarm still holds #595's
   `.728/.793`. The adopted change moves the headline to `.778/.837` and it **will** fire drift on the
   next timer (Sat 10:40 UTC). Left for the operator rather than self-approved: re-seeding the baseline
   your own change just moved is exactly the audit smell that guard exists to catch.
2. **CE score-blend rerank** — the stage 3 follow-up above.
3. **Entity extraction on the query side** covers only 33%/19% of the two tail classes (#569 §3.3).
   That ceiling, not the scoring, is what caps further entity gains.
