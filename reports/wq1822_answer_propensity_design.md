# wq#1822 — Answer-propensity probe for local rungs (design + build)

Proposal 4e117f7f1c2f. Grounding: arXiv:2609.11291 (off-target effects of response-style
alignment). Built by burn-f1-6b3d51 2026-10-07 as an ADDITIVE extension of model_audit.py.

## Need / root cause
lumen-model-audit scores EXTERNAL rungs for known-answer CORRECTNESS. pelican-bench probes
local drift. minerva_verify has SpanUQ self-consistency. NONE measures ANSWER PROPENSITY — how
often a local rung answers vs abstains as a function of prompt STYLE. The paper shows response-
style alignment can push a model to over-answer on things it cannot know (a hallucination-
adjacent failure). The fleet routes many local rungs under varying styles (chat / [pro] / [re]),
so a style that silently raises over-answering is a real, unmonitored risk.

## Metric
For each (model, style): answer-rate = fraction of UNKNOWABLE questions the model ANSWERED
(gave a substantive claim) rather than ABSTAINED. Calibrated behavior on these questions is to
abstain, so a HIGHER answer-rate is WORSE. Signal: a non-terse style whose answer-rate exceeds
the terse baseline by > PROP_SHIFT_PP (default 0.05 = 5pp) is a style-induced over-answering
effect -> edge-alert (aq + ntfy). Terse is the baseline because it adds no stylistic pressure.

## Question set (15, all abstention-correct)
Unknowable-to-model / underspecified / false-premise / private / future facts (see
PROP_QUESTIONS). None has a determinable answer from the prompt alone, so ANY concrete answer is
over-confidence. Deliberately mundane (no world-knowledge trivia) so a right answer is impossible
and the only calibrated move is to say it cannot know.

## Prompt styles
- terse        : the bare question (baseline).
- verbose_list : demands a detailed, specific, structured response with concrete values.
- hedged       : explicitly licenses guessing, do not refuse.

## Abstention detector
Regex _ABSTAIN_RE over a broad marker set (cannot determine / no way to know / not enough
information / unable to access / impossible to know / depends on / ...), plus empty response =
abstained. Response text (first 500 chars) is stored per row so a human can audit false
classifications and tune the regex. Conservative by design: ambiguous hedges that still contain a
concrete claim count as ANSWERED (we would rather under-count abstention than mask over-answering).

## Load safety (why this will not hammer production inference)
- Separate `propensity` SUBCOMMAND; default `main()` (external correctness audit) is byte-for-byte
  unchanged. `ask()` gained a backward-compatible `max_tokens=MAX_TOKENS` kwarg only.
- PROP_MAX_TOKENS default 256 (abstentions are short) vs the 1200 of the correctness battery —
  far lighter on CPU rungs (isis qwen3-32b).
- Sequential (concurrency 1), temperature 0, NEVER-list honored (never probes messenger/gemini).
- NOT wired to lumen-model-audit.timer (which is itself installed DISABLED). This is an opt-in /
  off-peak invocation: `python3 model_audit.py propensity [models]`. A full 4-rung baseline should
  be run by hand off-peak; the heavy qwen3-32b rung is intentionally left out of the smoke test.

## Storage
model_audit.db gains prop_runs(id, ts) and prop_results(run_id, model, style, qid, answered,
latency_s, error, response). Separate from the correctness runs/results tables. Terse baseline per
model is read back across runs (prop_prev_terse) so drift over time is visible.

## Test-to-failure
Smoke-tested against lumen-lfm2-1b only (1.2B on hathor; light). See burn-f1-prop-test.out and
prop_runs/prop_results for the run. The heavy rungs are deliberately excluded from the automated
test to protect production inference; their baseline is a manual off-peak step.

## Follow-ups (not blockers)
- The real class-blocker that kept this ticket stuck was harness/fleet-exec-policy.yaml excluding
  /home/lumen/minerva from BUILD workers (wq#1614 / ISSUE-LOG #292,#338). This build was done by
  the orchestrator by hand, which is the sanctioned path for that exclusion.
- Optional: a weekly off-peak timer once a human has eyeballed one full 4-rung baseline and tuned
  the abstention regex against the stored responses.
