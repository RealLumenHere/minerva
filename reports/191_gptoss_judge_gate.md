# wq#191 — GATE: gpt-oss-120b creative-pairwise judge validation

ticket: wq#191 | agent: claude-web-wq191-f56260 | date: 2026-08-22 (Pacific) | deps: wq#187, wq#196

## VERDICT: **FAIL — gpt-oss-120b is NOT cleared for prod judge use.**

Called here, not deferred. The gate had sat at "partial pass, pending" since 2026-07-13; wq#196
landed the missing evidence on 2026-07-16 and nobody re-ran it. The directional half has now run
on the full 25-pair set and misses both thresholds by a wide margin:

| criterion | threshold | measured | |
|---|---|---|---|
| directional agreement on clear pairs | >= 0.80 | **0.32** (8/25) | FAIL |
| tie rate on true-improvement pairs | <= 0.20 | **0.68** (17/25) | FAIL |
| inverted (picked the worse answer) | — | 0/25 | clean |
| position consistency | — | 0.76 | |
| no-verdict / truncation | — | 0/25 | clean |

FAIL action per the ticket — fall back to v4-flash-as-judge with the wq#188 sec-B length-drift
guard — **requires no change**: `duet_prompts.DEFAULT_JUDGE` is already `deepseek-v4-flash`. See
"ticket premise corrections" below. Nothing was reconfigured, started, or stopped by this session.

## What ran

- Harness: `/home/lumen/personas/qa/judge_gate_gptoss.py --clear` (wq#196's directional phase),
  unmodified. Judge `cerebras-oss-120b` via LiteLLM `localhost:4000`, temp 0, both-orders
  double-call consensus, task context included, provenance stripped.
- Pairs: `/home/lumen/personas/qa/calib_clear_pairs.jsonl` — 25 pairs from CALM
  fallacy_oversight (arXiv:2410.02736), `better` = dataset's sound answer, `worse` = dataset's
  fallacy-inserted variant. Direction labels are external and by-construction. I re-checked the
  provenance chain (vendored snapshot -> mechanical seed-42 stratified sampler -> jsonl) before
  trusting it: no harness-author judgment is laundered into the bench. The wq#196 independence
  constraint holds.
- Result artifact: `personas/qa/gate_results_clear_2026-08-23_053602.md`.
- Both runs executed detached via `lumen-run` (50 and 34 LLM calls). Read-only: LLM calls on the
  judge rung only.

## The failure has a clean mechanism, and it is not "the judge is bad at math"

Per-stratum consensus is perfectly separated:

| stratum | n | correct | tie |
|---|---|---|---|
| GSM8K | 9 | 0 | 9 |
| MATH | 8 | 0 | 8 |
| ScienceQA | 8 | **8** | 0 |

On 14 of the 17 math pairs the fallacy-inserted variant still lands the *same final number* as
the sound answer — it just gets there through invented reasoning (car-wash pixies, zodiac
multipliers). A judge told to weigh "factual content, completeness, specificity, correctness"
reads same-answer as same-substance and ties. That much is a defensible bench artifact.

**It does not cover the whole failure.** Three math pairs — `calm-fallacy-221` (5 vs 15),
`calm-fallacy-375` (400 vs 20), `calm-fallacy-283` (576 vs 96) — have a fallacy variant that
reaches a *flatly wrong* final answer, and the judge tied those too. Re-scoring on the most
charitable subset available (the 8 ScienceQA pairs + those 3 unambiguous math pairs, discarding
all 14 same-final pairs as ambiguous) still gives **agreement 0.73, tie rate 0.27** — FAIL on
both criteria. The verdict is robust to how generously the bench is scored.

## Root cause is the gate rubric, not the model (diagnostic, outside the gate score)

The gate rubric ends with a tie-prime: *"If A and B say the same things, they are a TIE
regardless of styling."* That sentence exists to neutralize style bias for the expect-0 half.
Hypothesis: it also suppresses genuine discrimination on the directional half.

Tested it. `personas/qa/diag_rubric_tieprime.py` re-runs **the same 17 math pairs that tied
17/17**, same model, same temperature, same both-orders consensus, same task context — only the
rubric changes: tie-prime removed, reasoning soundness named explicitly as a substance axis.

    gate rubric:     0/17 correct, 17/17 tie
    neutral rubric: 14/17 correct,  3/17 tie   -> agreement 0.82, tie rate 0.18

So gpt-oss-120b **can** discriminate these pairs. The gate's own rubric is what collapses the
signal. This is a finding about the harness, and it lands on the ticket that wrote the harness.

**This does not convert the FAIL into a PASS, and I did not treat it as one.** That 0.82 came
from a rubric I wrote after seeing the failure, on a 17-pair subset, on the math strata only, and
it has never been run against the expect-0 style half — where removing the tie-prime is exactly
the change most likely to reintroduce the style bias the other half was measuring. Adjusting the
instrument until the number clears the bar, then reporting the number, is the thing this gate
exists to prevent. It goes in the file as a hypothesis for a re-gate.

## The 2026-07-13 "style/position half PASSED decisively" needs downgrading

That run scored style_bias 0.00, tie_rate 1.00, pos_consistency 1.00 on 25 identical-content
pairs. Read alongside the directional result, it carries much less weight than it appeared to: a
judge that ties *everything* scores perfectly on an all-ties bench. The expect-0 half cannot
distinguish "formatting neutralization is working" from "this rubric ties by default," and we now
have direct evidence of the latter. The two halves were never independent. Anything downstream
citing that 0.00 style-bias as evidence of judge quality should cite this instead.

## Ticket premise corrections

1. **"cerebras gpt-oss-120b is the DEFAULT duet judge"** — not in shipped code. `duet_prompts.py`
   has `DEFAULT_JUDGE = "deepseek-v4-flash"`; gpt-oss-120b is `DEFAULT_INSTRUCTOR` (the critic
   role), which this gate does not cover. The claim was true of the wq#188 addendum spec; the
   code that shipped diverged. That divergence is why no config change is needed today.
2. **"No prod judge use until this passes"** — wq#185 deployed and wq#189 piloted on 2026-07-16/17
   while this gate was open. Materially, though, #189 logged **0 judge rows** (degeneration stop
   pre-empted the judge), so no gpt-oss judge verdict has ever shaped a shipped artifact. The
   constraint was procedurally breached and substantively intact.
3. **This gate does not validate the construct its title claims.** Two gaps: (a) the bench is
   GSM8K/MATH/ScienceQA QA, not creative prose — zero creative pairs have ever been measured, so
   "creative-pairwise judge validation" remains unmeasured in *both* directions; (b) the harness
   rubric (pairwise A/B, both-orders, ternary) is not the prompt duet actually runs — live
   `JUDGE_SYSTEM` is a single-call trajectory judge emitting `quality_delta` -1/0/1 with no
   both-orders and no tie-prime. The rubric defect found above is in the harness, and does **not**
   automatically transfer to the prod judge prompt.

## Recommended follow-ups (not actioned — these are new tickets, not this gate's scope)

1. **Re-gate on a revised rubric, both halves together.** Drop the tie-prime, name reasoning
   soundness, keep formatting-disregard; re-run expect-0 *and* directional. A rubric only passes
   if it holds style_bias low *and* clears 0.80/0.20. Blocking for any promotion of gpt-oss to
   judge.
2. **Build actual creative pairs** if "creative-pairwise" is to mean anything — same independence
   constraint as wq#196 (external labels or Frank hand-labels; not self-generated).
3. **Gate the prod construct, not a proxy.** Point the harness at duet's live `JUDGE_SYSTEM`
   shape, or reconcile the addendum's binding protocol with what shipped.
4. **v4-flash-as-judge is unmeasured on this bench too.** Its 0.82-0.91 directional figure is
   literature, not local measurement. It is the live default by inheritance, not by evidence.
   Cheap to close: same harness, `JUDGE_MODEL=deepseek-v4-flash`.

## Reproduce

    ssh lumen@100.117.66.102 '/home/lumen/personas/qa/run_clear_gate.sh'            # full gate, ~4 min
    ssh lumen@100.117.66.102 'cat /home/lumen/personas/qa/gate_results_clear_2026-08-23_053602.md'
    ssh lumen@100.117.66.102 '/home/lumen/personas/qa/run_diag_tieprime.sh'   # rubric diagnostic

Artifacts: `personas/qa/{judge_gate_gptoss.py, calib_clear_pairs.jsonl,
gate_results_clear_2026-08-23_053602.md, diag_rubric_tieprime.py, diag_tieprime_result.json,
run_clear_gate.sh, run_diag_tieprime.sh}`. `personas/` is not a git repo; this report is the tracked record.
