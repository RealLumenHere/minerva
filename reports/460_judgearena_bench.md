# wq#460 — JudgeArena judge-bench harness (prop 9455507d9aeb, Scribe #321)
Date: 2026-08-10 · Session: claude-web-0809b

## Premise verification
JudgeArena REAL: arXiv:2608.02620, GitHub OpenEuroLLM/JudgeArena. Unifies
AlpacaEval/Arena-Hard/MT-Bench + ELO tasks with swappable LangChain judges.

## Design decision
Proposal wanted "JudgeArena's standard judge tasks" — those benchmark
CANDIDATE models and need generation. For selecting minerva-eval's verify
judge, the right measurement is judge-vs-HUMAN agreement on battles whose
completions already exist. Built on JudgeArena's own APIs
(load_arena_dataframe + judge_and_parse_prefs): fixed-seed (20260810) sample
of single-turn hard-winner LMArena-100k battles, each candidate judge scores
them via ChatOpenAI -> LiteLLM gateway, agreement/parse-fail/latency ->
judgearena_results table in minerva.db. Judge-only cost, drift-comparable.

## Shipped
- Vendored: /home/lumen/judgearena (venv .venv, pip -e, no vLLM extra)
- /home/lumen/minerva/judgearena_bench.py (runs under vendored venv)
- minerva.db table judgearena_results
- lumen-judgearena-bench.{service,timer} weekly Sun 06:15 UTC, DISABLED
  (Frank-gated). Staged copies in /home/lumen/minerva/staging/.
- Default judge roster: deepseek-v4-flash, qwen36-local, nephthys-qwen3-14b,
  isis-qwen3-32b (JAB_MODELS env override). Note: proposal named
  "gemini-flash" — exists but is a 20 req/day explicit-opt-in rung (aq#352 lesson: probing it exhausts the quota being monitored); excluded by policy.

## Validation
Smoke (3 battles, nephthys-qwen3-14b, detached): dataset fetch OK,
3/3 judged, agreement 0.667, 0 parse fails, ~235s/battle under concurrent
bench load. DONE:0.

## Rollback
Disable timer; drop judgearena_results; rm -rf /home/lumen/judgearena.
