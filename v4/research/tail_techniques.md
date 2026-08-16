# wq#569 [mv4-P0a] — Retrieval-tail techniques, VALIDATED + LOCALIZED to Lumen

_Session mv4-w569-908756 · 2026-08-14 PDT · READ-ONLY on production stores._
_Input: `minerva/v4/research/SOTA_2026-08.md` (RQ1 + Recommendations) + `MEMORY_V4_PLAN.md` §0-1b._
_This report does NOT re-survey. It checks the SOTA report's claims against live state and measures its
latency table on our CPU. Raw data: `/scratch/mv4-569-bench/results_t{4,16}_n60.json`, harness `bench.py`._

## 0. TL;DR for #574

1. **Every latency number in SOTA RQ1 is ~8-10x optimistic for this box.** Measured against the report's
   own reference model as a calibration anchor. Use the table in §2, not RQ1's.
2. **ONNX-int8 is a REGRESSION here, 1.1-1.8x SLOWER than fp32, in all 12 paired cells.** This CPU has
   no AVX-512/VNNI. The dispatch's "ONNX-int8 preferred, fp32 fallback" ordering must be INVERTED.
3. **Ettin-17M fp32 @16 threads, pool 20 = 247ms p50 — the only configuration that clears p50<400ms.**
   Rerank is viable, but only at pool ~20 and only if the 4-thread containment cap is raised (§2.4 risk).
4. **Ettin's quality/speed Pareto advantage survives the port** — 17M is 2.1x faster than the incumbent
   MiniLM-L6-v2 at higher published MTEB. Model choice in the report is right; its numbers are not.
5. **MultiConIR per-class regression risk is REAL and structurally aimed at our two target classes.**
   Adopt rule is non-negotiable: MRR up AND no class -2pts. See §6.

## 1. LIVE-STATE CORRECTIONS (fq F1/F2/F3 — verified this session, not from stored prose)

| Claim in report/ticket | Live state 2026-08-14 | Impact |
|---|---|---|
| wq#388 rerank kit "built, UNWIRED" | **TRUE but incomplete**: `lumen-rerank.service` is **ACTIVE**, up 6d12h, pid 2761, 127.0.0.1:8140, `/health` 200, **614MB RSS**, serving `cross-encoder/ms-marco-MiniLM-L-6-v2`. `lumen_recall2.py` rerank refs = **0** (confirmed). | There IS already a resident reranker on Lumen. The "no warm models" directive is being violated today by a service nobody is using. Frank decision in §7. |
| `jhu-clsp/ettin-reranker-17m` / `-32m` | **404 — do not exist.** Real: `tomaarsen/ms-marco-ettin-17m-reranker`, `tomaarsen/ms-marco-ettin-32m-reranker`. Also official `cross-encoder/ettin-reranker-68m-v1`. | #574 must use the corrected IDs. |
| "ONNX-int8 pre-exported exists" | **No ONNX for 17m/32m.** Must export+quantize locally (~11-16s each, done, cached at `/scratch/mv4-569-bench/onnx/`). 68m DOES ship ONNX incl. separate `quint8_avx2` vs `qint8_avx512_vnni` variants — upstream itself distinguishes the two ISA targets. | Confirms the VNNI dependency is real, not theoretical. |
| "tune on the 193 labels" | **Golden set is 257 queries and CHANGED MID-SESSION** (193 at 19:38 → 257 at 19:42, `.bak.20260814_pre_wq572` present). #572 is actively rewriting it. New strata present: `abstention`, `contradiction-resolution`, `knowledge-update`, `control-hit`, `control-miss`. | "193" is stale everywhere it appears. Tuning MUST pin a frozen snapshot (§5.1). |
| "RRF baseline" | **Production is NOT RRF.** `search_v3` uses fixed-weight linear: `fused = 0.50*dense + 0.28*lex + 0.16*graph + 0.06*recency`. | Task (d) reframed: tune those weights; RRF is a comparison arm, not the baseline. |
| Entity channel "from familiarity_scorer" | `familiarity.db` = 19,876 entities / 26,369 mentions, but top entities are `openai, litellm, deepseek, nvidia, chatgpt` — the **AI-news intel stream, not our ops vocabulary**. | Using it directly would be a mistake. See §3. |

## 2. (a) MEASURED — CPU reranker latency on Lumen

**Hardware:** Dell T5810, **Intel Xeon E5-2699 v4 @2.20GHz** (Broadwell-EP, 22C/44T), **AVX2 only —
no AVX-512, no VNNI, no AMX**. Reference in SOTA RQ1 is an i7-13700K (Raptor Lake, 2022, 5.4GHz boost).

**Method:** load-bench-unload, no resident model, no service touched, isolated venv at
`/scratch/mv4-569-bench` (production `ab_rag/rerank/.venv` untouched). 60 real queries sampled
seed-569 from `recall_golden.jsonl`; candidate pools built from **real `archival_notes` text**
(title + content[:2000], pool docs p50 = 710 chars), edge-anchored then padded — reproduces the real
token-length mix. 3 warmup calls discarded, p50 over n=57. max_len 512. Detached via `lumen-run`.

**Sample-size validation:** n=60 reproduced the n=254 full-set run to **1.1%** (1309.7 vs 1324.0ms,
anchor/pool20/t4). 60 is sufficient for a median.

### 2.1 Full matrix (p50 ms)

| Model | Arm | pool | thr=4 | thr=16 | RQ1 ref (i7-13700K) |
|---|---|---|---|---|---|
| **MiniLM-L6-v2** *(anchor, = deployed)* | fp32 | 20 | 1309.7 | 410.9 | 139 |
| | fp32 | 50 | 3601.8 | 1060.1 | 347 |
| | int8-avx2 | 20 | 1507.2 | 635.2 | — |
| | int8-avx2 | 50 | 3919.5 | 1687.6 | — |
| **ettin-17m** | fp32 | 20 | 628.5 | **247.4** | 75 |
| | fp32 | 50 | 1676.9 | 671.6 | 187 |
| | int8-avx2 | 20 | 858.0 | 404.5 | — |
| | int8-avx2 | 50 | 2359.0 | 1188.9 | — |
| **ettin-32m** | fp32 | 20 | 1465.3 | 573.6 | 216 |
| | fp32 | 50 | 4289.8 | 1589.7 | 540 |
| | int8-avx2 | 20 | 2023.4 | 906.6 | — |
| | int8-avx2 | 50 | 5443.3 | 2602.0 | — |

24/24 cells completed, 0 failures. p95 within 4-12% of p50 in every cell (quiet box; per decision C
wq#567 we judge p50 anyway).

### 2.2 Hardware factor vs the report's table (fp32, the only comparable arm)

At the report's implied threading, thr=4: **8.4x - 10.4x slower**. At thr=16: **2.7x - 3.6x slower**.
The anchor model pins this honestly — it is the *same model* the report benchmarked, so the ratio is a
clean hardware+config factor, not a model difference: **9.4x @pool20, 10.4x @pool50 (thr=4)**.

**Rule for #574: multiply any SOTA RQ1 latency by ~9 (thr=4) or ~3 (thr=16) before believing it.**

### 2.3 ONNX-int8 is slower here — 12/12 cells

int8/fp32 p50 ratio (>1 = int8 slower): thr=4 → 1.09-1.41x. thr=16 → **1.55-1.77x**.
The penalty gets WORSE with more threads.

**Why:** x86 int8 inference speedup comes from VNNI (`AVX512_VNNI`) or AMX. This Broadwell CPU has
neither, so ORT falls back to an AVX2 u8s8 path whose quantize/dequantize overhead exceeds the
narrower-arithmetic gain. `onnxruntime.get_available_providers()` = `['AzureExecutionProvider',
'CPUExecutionProvider']` — no acceleration EP. Upstream shipping distinct `quint8_avx2` vs
`qint8_avx512_vnni` artifacts for the 68m model is direct corroboration.

**VERDICT: on Lumen, use fp32 torch. Do not ship ONNX-int8.** (This flips on a VNNI-capable node —
if Hapi/BC-250 or an EPYC Horus lands, re-measure there before reusing this conclusion.)

### 2.4 Thread scaling — and the risk it re-opens

4→16 threads gives **2.5x - 3.4x** (sub-linear, as expected). This is what makes rerank feasible:

| Config (fp32) | p50 | vs 400ms budget |
|---|---|---|
| ettin-17m, pool 20, **thr=16** | **247ms** | **PASS (1.6x headroom)** |
| ettin-17m, pool 20, thr=4 | 628ms | FAIL 1.6x over |
| ettin-17m, pool 50, thr=16 | 672ms | FAIL 1.7x over |
| ettin-32m, pool 20, thr=16 | 574ms | FAIL 1.4x over |
| MiniLM-L6-v2 (incumbent), pool 20, thr=16 | 411ms | marginal FAIL |

**⚠ RISK — read before raising threads.** The 4-thread cap is not arbitrary. Drop-in
`/etc/systemd/system/lumen-rerank.service.d/` (Claude 2026-07-26) records: rerank found hung after 5d
with **215 threads, 812MB RSS + 490MB swap**, health endpoint dead, 11.7 MB/s continuous refault,
which held memory-stall PSI(full,avg60) at ~58% against mcp-pressure-guard's 25% threshold and caused
**~33 MCP gateway restarts/day**. Root cause was thread explosion (torch/BLAS defaulting to one thread
per core across several pools).

Nuance that makes 16 viable: the incident was *uncapped* threading (215 threads), not a deliberate
16-thread intra-op cap. A pinned `intra_op=16, inter_op=1` with `TOKENIZERS_PARALLELISM=false` is a
different regime. But this is exactly the failure that cost the most operator time in July, so #574
must: (1) pin explicitly, never rely on defaults; (2) keep `MemoryHigh/MemoryMax`; (3) watch PSI +
gateway restart rate for 48h after any thread change; (4) treat a restart-rate rise as auto-rollback.

**Budget recommendation for #574: rerank top-20 only, ettin-17m fp32, threads pinned 16.**
Pool 50 does not fit this CPU at any tested config. If per-class results need a deeper pool, the answer
is a faster node, not a bigger pool.

## 3. (b) Entity channel — design

**Core correction: use entity SPECIFICITY, not familiarity.** `familiarity_scorer.familiarity()` returns
HIGH for frequently-seen entities. Retrieval needs the inverse: a rare entity match is informative, a
common one is noise. Wiring familiarity in with a positive sign would actively boost the least
discriminative matches.

**Measured on our corpus** (N=1153 live notes, 870 distinct entities from `memory_edges` subject/ticket/file):

- entity DF: **p50=1, p90=3, max=123**. **68% of entities have DF==1** (maximally specific).
- IDF spread: rarest `ln(N/1)=7.05` → commonest `ln(N/123)=2.24`. **3.1x dynamic range** — enough to
  matter, and currently thrown away.
- Commonest (near-worthless) anchors: `backup(123), litellm(101), messenger(92), thor(88),
  nephthys(81), bench(76), mcp(66), skinnnyjay(64)`.

**The current `graph` channel already flattens this.** `search_v3` assigns a flat bonus by anchor TYPE
(ticket 1.2 / subject 1.0 / tag 0.9 / expansion 0.35), capped at 1.2 and normalized. Matching `backup`
(DF 123) scores identically to matching `enp0s25` (DF 1). **That is the actual defect the entity channel
should fix** — it is not a missing channel so much as an unweighted one.

### 3.1 Scoring

```
ent(q, note) = Σ_{e ∈ E(q) ∩ E(note)}  idf(e) * type_w(e)   /   Σ_{e ∈ E(q)} idf(e)
idf(e)       = ln(N_notes / (1 + df(e)))                     # df from memory_edges, corpus-derived
type_w       = ticket 1.2 | subject 1.0 | file 1.0 | tag 0.7
```
Normalized to [0,1] so it composes with the existing linear fusion. `E(q)` from the SAME extractors
`search_v3` already uses (`_subjects_re`, `TICKET_RE`, `_query_tags`) — no new NLP, no new failure mode.

`familiarity.db` enters ONLY as a weak tiebreaker prior for entities absent from `memory_edges`
(novel-entity signal, band `novel|low`), never as a positive relevance weight, and never as a filter.
Its vocabulary is the intel stream, so it must not drive ops-note ranking.

### 3.2 Fusion point

`lumen_recall2.py`, the scoring loop (`for r in rows:` — the `fused = ...` line). Additive,
non-filtering, flag-gated:

```
fused = w_d*d + w_lex*lex + w_g*g + w_rec*rec + w_ent*ent      # MEMORY_V4_ENTITY, default off
```
Candidate generation is UNCHANGED — the channel re-ranks the existing pool, never gates it. Flag off =
byte-identical scores to today (assert this in the test, don't assume it).

### 3.3 Honest coverage ceiling — measured, and it caps the upside

Share of golden queries containing at least one known subject anchor:

| class | coverage |
|---|---|
| outage-rca | **20/61 (33%)** |
| build | 9/33 (27%) |
| gotcha | 9/44 (20%) |
| procedure | **7/37 (19%)** |
| decision | 3/31 (10%) |
| hardware | 1/21 (5%) |

**The entity channel can only move ~33% of outage-rca and ~19% of procedure.** Even a perfect channel
cannot lift procedure R@1 much — its queries mostly don't name entities. This is a real ceiling and
#574 should not be surprised by a small procedure delta. The SOTA threshold "if RCA R@1 doesn't move
from ~0.57, inspect entity-extraction recall" is right to be suspicious, but the first thing to check
is query-side coverage, which is 33%, not extraction.

## 4. (c) HyPE — index-time question generation, offline only

**Design constraint discovered:** `lumen_dream.py` today makes **zero LLM calls** — it is entirely
rule-based (stale-IP scan, cosine dupes, instinct candidates, embed retry, origin stats), runs weekly
`Sun 18:00 UTC`, proposal-only. Bolting an LLM stage inside it would convert a deterministic,
always-succeeds job into one with a network dependency.

**Therefore: separate module `minerva/lumen_hype.py`, invoked by the dream timer as a distinct
ExecStart, failing open.** dream's determinism is preserved; HyPE failure degrades to "no new
hypothetical questions this week", never a dream failure.

- **Trigger:** notes with `hype_generated_at IS NULL` or `mtime > hype_generated_at`. Idempotency-guarded
  before first run (standing instinct: replay/backfill jobs must be guarded).
- **Prompt:** per note, generate 3-5 hypothetical questions this note would answer. Ops-flavoured
  ("what caused X", "how do I Y", "why is Z set that way") to match our query classes.
- **Routing:** LiteLLM `:4000`, **`cerebras-oss-120b` / `cerebras-llama` first (free tier)**, fallback
  `deepseek-v4-flash`. **NEVER `messenger-chat`/`messenger-re`** (standing instinct; also dodges
  LiteLLM bug #22249). `response_format=json_object` + tolerant parse + one retry + fail closed
  (standing instinct for unattended LLM-JSON).
- **Storage:** new table `note_hype(note_id, question, embedding, created_at)`. Questions are embedded
  and searched as an ADDITIONAL dense channel; **the source note is never mutated** (supersede-not-delete
  discipline). Retrieval returns the parent note, not the question.
- **Cost:** 1153 notes x ~1 call. One-off backfill on free tier; weekly delta thereafter is tens of calls.
- **Zero hot-path LLM** — satisfies the invariant. HyDE (query-time) stays SKIPPED.
- **Gate:** same as everything else — golden v4 win or it doesn't ship.

## 5. (d) Fusion-weight tuning plan

Baseline is the live fixed-weight linear (§1), **not RRF**. Five channels once entity lands.

### 5.1 Frozen-snapshot requirement (blocking)
The golden set changed under this session. Any tuning run MUST first
`cp recall_golden.jsonl golden_v4_frozen_<date>.jsonl` and tune against the frozen copy, recording its
sha256 in the result artifact. Otherwise results are irreproducible and non-comparable to #572's gate.
Coordinate with #572 — it owns the set.

### 5.2 Arms
1. **Control** — current weights `0.50/0.28/0.16/0.06`, entity off. Log the control arm's EFFECTIVE
   runtime config inside the result artifact (audit discipline).
2. **RRF** — `1/(k+rank)`, k=60, parameter-free reference.
3. **Tuned linear** — coordinate ascent or small random search over the 5-simplex, MRR objective.
4. **Percentile-norm + tuned** — per-channel percentile-rank normalization before weighting
   (calibrated-fusion, robust across heterogeneous channel scales; our channels are wildly
   heterogeneous — cosine 0-1, lex a ratio, graph a capped bonus, recency linear-in-age).

### 5.3 Protocol
- **Nested CV, 5-fold, stratified by `cls`.** 257 queries with 5 free parameters overfits trivially on a
  single split — report held-out fold means only. This is the single biggest methodological risk.
- Report **per-class** R@1/R@5/MRR, not just aggregate.
- **Adopt rule: aggregate MRR up AND no class down >2pts** (same rule as rerank).
- Small classes (`milestone` n=1, `control-hit` n=1, `control-miss` n=1, `session-archive` n=3) are
  **not** gate-eligible — n too small for a 2pt rule to mean anything. Use them as sanity canaries:
  control-miss SHOULD stay missed; if a weight change makes control-miss retrieve, the harness is wrong.
- Latency: weight tuning is free at query time (same channels, different multipliers). No budget impact.

## 6. (e) ⚠ PROMINENT FLAG FOR #574 — per-class regression risk

**Pointwise cross-encoder rerankers are documented to HURT multi-condition queries — which is exactly
what outage-rca and procedure are.**

- MultiConIR (arXiv:2503.08046): reranker Win Rate **-39%** on multi-condition queries; degradation ~2x
  the retriever's.
- NVIDIA (arXiv:2409.07691): MiniLM-L12 **LOWERED** NDCG@10 0.6100 → 0.5771 over a strong dense retriever.
- Conversational study (2507.13937): MiniLM-L12 cut MRR **42%**.

Our tails are not factoid lookups. "what caused the daily 13:00 UTC fleet deaths" and "minerva pipeline
dead for days on a silent fallback" are multi-condition causal queries — **the exact shape these papers
report regressing.** The prior should be "rerank may hurt RCA/procedure", not "rerank fixes the tail".

**Mandatory adopt rule for #574 (non-negotiable):**
> Adopt reranking ONLY if aggregate MRR rises **AND** no class regresses more than 2 points.
> If outage-rca or procedure regress: **scope reranking to the classes that win, or skip it entirely.**
> **The entity channel survives independently of the reranker verdict** — they are separate adopts, and
> the entity channel is the cheaper, lower-risk, no-latency-cost one. Ship it first, alone, and measure
> it alone. Do not bundle.

Sequencing recommendation: **entity channel → measure → fusion weights → measure → rerank last.**
Rerank is the only one that costs 247ms+ and the only one with published evidence of harming our
target classes.

## 7. FRANK-GATED

1. **`lumen-rerank.service` is running an unused 614MB resident model.** It contradicts the "no warm
   models on Lumen / ram-cap" directive and serves zero consumers (0 refs in `lumen_recall2.py`).
   Options: (a) stop+disable until #574 actually wires a reranker — reclaims 614MB now;
   (b) leave it, accept the RAM; (c) leave it and repoint `RERANK_MODEL` to ettin-17m so it is at least
   the right model when #574 arrives. **Recommend (a)** — nothing consumes it, and #574 will need a
   thread-pinning config change anyway. NOT executed: this ticket is read-only and it is a live service.
2. **Raising rerank threads 4→16** re-opens the July PSI/gateway-restart failure mode (§2.4). Needs
   explicit approval + 48h PSI watch, and belongs to #574, not here.
3. **wq#570 and wq#571 remain unclaimable** — same over-broad `minerva-db` subsystem declaration that
   blocked #569, contradicting `MEMORY_V4_PLAN.md:46` ("P0 parallel, research is read-only"). One
   `wq edit <id> --subsystems ""` each unblocks the dispatched workers. Not done unilaterally.

## 8. Reproduce

```
ssh lumen@100.117.66.102 'cd /scratch/mv4-569-bench && HF_HOME=/scratch/mv4-569-bench/hf \
  BENCH_THREADS=16 BENCH_NQ=60 ./venv/bin/python3 bench.py'
```
Artifacts: `bench.py`, `results_t4_n60.json`, `results_t16_n60.json`, `onnx/` (exported+quantized).
Scratch venv is disposable; production `ab_rag/rerank/.venv` was never modified.
