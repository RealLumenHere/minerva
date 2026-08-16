# Minerva defer-wall: root cause and fix pack
**2026-08-14 · triggered by Frank: "every proposal in the last 4 days has been deferred"**

## What was actually wrong

Not judgement. **Context starvation at three separate stages**, plus a dead-letter state.

| Stage | Symptom Frank saw | Real cause |
|---|---|---|
| Scorer (minerva_proposal) | ~90% defer, rejects fell to 0 | wq#412 raised PROPOSAL_LIMIT 10->25 on 08-08 to drain an 813-item backlog; the 2.5x flood reached low-relevance intel. Backlog is now 108 -> override removed. |
| Scorer context | dozens of defers reading "Lumen lacks an evaluation harness" | wq#127 (07-11) restored the auto-derived `lumen_state.txt`, which REPLACED the hand-maintained fallback string. The generator emits probed facts only, so the GAPS list, the ALREADY-BUILT list and the content policy silently left the scorer's context. Eight eval harnesses exist; the scorer did not know. |
| Feeds | 33 of 266 four-day defers were the same item | No dedup existed between intel and proposals (only between research *prompts*). Muse-Glimmer-30B arrived from Phoronix, HuggingFace, VentureBeat and The Decoder as four separate proposals. |
| Planner (minerva_factory) | 26 plans RED on `planner_flag:unclear` | The planner's whole world was two hardcoded paths plus "if you cannot name a real file, use unplannable". Every one of those 26 rationales says a version of "cannot name the file". `classify_lane` then scored that shrug identically to "this could brick Lumen". |
| Factory appliers | 40 plans YELLOW `new_service`, none ever applied | `new_service` was PLANNABLE but had **no applier**. Even approved, apply_plan returned `no_applier_for:new_service`. They waited for a human to hand-write the code. |
| Defer state | 495 defers, all `decided_at` NULL | `minerva_eval` reads `status='accept'` only. Nothing has ever read `defer`. Last human sweep: 08-10. |

## Fixes shipped

**`factory_context.py`** (new) - real-path factpack for the planner: every minerva/agent/bin
module with its purpose line, every `lumen-*`/`minerva-*` unit mapped to its actual ExecStart
script, all existing LiteLLM route names plus a copyable model_list entry shape. Cached 1h,
capped, fail-open to the old behaviour.

**`minerva_factory.py`**
- `unclear` is a SAFETY field again, not a confidence field. A shrug triggers one retry on an
  independent second model (`cerebras-oss-120b`, different vendor bucket). Only a shrug that
  survives both planners goes RED, tagged `unclear_after_2`. Rescued plans are capped at YELLOW -
  rescue can never raise a lane.
- `need_files`: the planner may request up to 3 files (path-allowlisted, read-only) before writing
  a diff, on both the primary and the rescue attempt. Came straight from the probe - the residual
  failures all said "cannot produce a concrete diff without seeing the code".
- `new_service` applier: stages an inert bundle (script + unit + timer + INSTALL.md) to
  `/home/lumen/factory/staged/<name>/`, py_compiled, `systemd-analyze verify`-ed, RED-pattern
  scanned, no exec bit, nothing in /etc, nothing started. GREEN because it activates nothing.
  Activation is a separate approval-gated `install` verb that undoes itself if the unit fails.
- `replan` verb to re-run proposals parked by the pre-factpack planner.
- Planner max_tokens 6000 -> 12000, escalating on JSON parse failure (a plan embedding a whole
  script was truncating mid-string into "Unterminated string").

**`minerva_dedup.py`** (new) - calibrated on 400 real proposals, not guessed:
random pairs top out at 0.798, true duplicates run 0.82-0.91. Cosine alone is a trap:
"Inkling-Small GGUF into LiteLLM" vs "MiniMax-H3 GGUF into LiteLLM" scores 0.902 and they are
different models - the boilerplate dominates the sentence. A duplicate therefore requires
**cosine >= 0.85 AND entity-token overlap >= 0.60** after stripping template words. Measured
separation on labelled pairs: true dups 0.67-1.00, distinct 0.00-0.50. Plus a deterministic layer
(a LiteLLM route that already exists is a duplicate by definition). Runs before the verify judge.
Local nomic-embed-text, no API spend.

**`minerva_proposal.py`** - INVARIANTS block appended to the live digest instead of being replaced
by it: GAPS, ALREADY BUILT (with "no eval harness is FALSE and is not a valid reason to defer"),
the Broadwell no-AVX-512/no-AMX hardware truth, and explicit decision discipline (infeasible or
already-covered = REJECT, not defer).

**`minerva_defer_review.py`** (new, `minerva-defer-review.timer` daily 09:10 UTC, 3 prompts/day) -
information-gap defers become research prompts in the queue the fleet workers already drain, with
the CPU-only constraint stated up front and a mandatory `RECOMMENDATION: ADOPT|WATCH|SKIP` line;
settled defers retire immediately; anything with no path to a decision retires at 45 days. On
reap, the proposal is re-scored against the returned note.

## Measured results

Replan probe, 4 proposals that had been RED/parked for days:
- `f967b5a3a060` LLM failure attribution: RED-unclear -> **GREEN new_service**, concrete targets, 196s
- `d7a11ddf66a6` G-theory sizing: RED-unclear -> **YELLOW code_edit** via rescue, 162s
- `5ca323483e81` IntelliAudit checks: parked -> **GREEN new_service**, 124s
- `d252ebf6c6e4` Gemini 3.7 Flash: still RED - rescue asked for files and was not served. **Fixed**
  by moving need_files into `_plan_with_files()` so both planners get the round.

First live defer-review run: 19 retired, 3 research prompts queued, pile 495 -> 473.
First live dedup catch: "Add unsloth/Qwen3.8-27B-NVFP4 to LiteLLM" suppressed as a near-duplicate
of "Integrate Qwen3.8-27B-FP8 into LiteLLM" (cosine 0.878, entity 1.0) with parent lineage.

## Open for Frank
1. `new_service` staging as GREEN - it writes inert files only; activation still needs your touch.
2. Defer-review cadence: currently daily, 3 research prompts/day (DEFER_MAX_PROMPTS in the unit).
3. The remaining 39 RED + 16 YELLOW parked proposals: `minerva-factory replan N --apply` recovers
   them in batches. Started with 5.
