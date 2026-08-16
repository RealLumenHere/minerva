# wq#443 — Entity-familiarity scorer (prop fd2622e69837, Scribe #304)
Date: 2026-08-10 · Session: claude-web-0809b

## Premise verification + honest scope
Paper REAL: arXiv:2607.13568 (Brzezinka) — graded entity-familiarity read
from INTERNAL ACTIVATIONS at the prompt point; unfamiliar entities predict
confabulation; steering the readout flips refusal.
MECHANISM GAP: activation probes need hidden states — unreachable via
LiteLLM/ollama APIs. Proposal's evaluator-refusal wiring also repeats the
wq#444 stale premise (evaluator has no entity refusal logic).
BUILT INSTEAD: the behavioral proxy that IS measurable — how often an entity
has appeared in Lumen's own intel stream — as the "verify harder on novel
entities" advisory signal. Same signal that caught 50 speculative artifact
integrations in the 2026-08-09 YELLOW triage, made systematic. Data layer +
CLI + advisory JSON only; ZERO live-service edits.

## Shipped
- /home/lumen/minerva/familiarity_scorer.py (deterministic extraction:
  TitleCase spans, CamelCase, arXiv IDs, repo slugs, acronyms; URL-stripped;
  <=4 words; time-decayed log familiarity, half-life 45d)
- /home/lumen/minerva/familiarity.db — 18,936 entities from 13,010 intel
  rows (0.9s full ingest; incremental thereafter)
- Advisory: reports/familiarity_summary.json (novel-7d list for verify gate
  / factory triage to consult)
- CLI: ingest | score | novel | stats
- lumen-familiarity.{service,timer} 6h, installed DISABLED (proposal said
  15min — needless; intel lands a few times daily)

## Validation
DeepSeek -> 0.985/high (111 mentions); SkillHEX/JudgeArena -> low x1
(correct: brand-new); fabricated name -> 0.0/novel. Top-10 clean after
noise pass (URLs, HINT artifact, headline fragments).

## Not built (documented)
- Activation probes (impossible via API), embedding fuzzy match (BGE-M3
  upgrade path noted), evaluator wiring (belongs behind EAR flag, own gate).

## Rollback
Disable timer; rm script+units+familiarity.db. Nothing else touched.
