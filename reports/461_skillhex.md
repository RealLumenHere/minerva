# wq#461 — SkillHEX hypothesis loop (prop 5ccfeb0adf86, Scribe #322)
Date: 2026-08-10 · Session: claude-web-0809b

## Premise verification
SkillHEX REAL: arXiv cs.AI Aug-2026 (Feng et al.) — falsifiable failure
hypotheses -> executable tests as dense reward; evidence-guided search over
persistent skill-revision branches; escapes the "exploitation trap" of
greedy single-incumbent refinement.
PREMISE CORRECTIONS: proposal paths /data/lumen/bin and
/data/lumen/prompt_engine/topics.yaml do not exist; and DGM v2 ALREADY
implements propose->paired_eval->bootstrap-gate->human-approve. Built the
genuine delta only.

## Shipped (delta over DGM v2)
/home/lumen/autonomy/agent5/skillhex.py:
1. EVIDENCE HARVEST — hypotheses grounded in real failures: recent db5
   dead+review queue feedback (124 dead envelopes available) injected into
   the proposer prompt as falsifiable failure-cause hypotheses.
2. PERSISTENT BRANCH BANK — skillhex_results in minerva.db: every candidate
   incl. discards, with lineage, scores, decision.
3. EXPLORE/EXPLOIT — 40% of runs branch from best near-miss prior candidate
   (positive mean, not promoted) instead of the incumbent.
Reuses DV2.propose_prompt_mutation + DV2.paired_eval + dgm.py pending/ntfy
human gate. NEVER self-promotes (at most writes reg["pending"];
promotion = dgm.py approve).
Units: lumen-skillhex.{service,timer} Mon/Wed/Fri 12:30 UTC, DISABLED
(Frank-gated), EnvironmentFile=/home/lumen/agent/.env.

## Validation
- Debug finding: bare-shell runs fail silently in a5_common.call_llm without
  LITELLM_MASTER_KEY exported (services get it via EnvironmentFile) — unit
  file includes it; manual runs must source /home/lumen/agent/.env.
- Propose-only smoke: harvested 15 evidence items, produced hypothesis #1
  targeting truncated/incomplete drafts — exactly the dominant real
  dead-queue failure class. Recorded untested branch in skillhex_results.
- Full paired-eval path left to timer/manual (reuses proven DGM v2 code).

## Rollback
Disable timer; rm skillhex.py + units; drop skillhex_results. No changes to
DGM, workers, or active config.
