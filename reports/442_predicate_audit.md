# wq#442 — Predicate-Substitution Grounding Audit (prop 7a0fa9fabd92, Scribe #303)
Date: 2026-08-10 · Session: claude-web-0809b

## Premise verification
Paper is REAL: arXiv:2607.13069 "Interventional Grounding Audits: Black-Box
Premise-Dependency Tests for LLM Chain-of-Thought via Predicate Substitution"
(Nakamura). F1=0.806 detecting proof-tree dependencies on ProntoQA w/ GPT-4o.
Author explicitly limits scope to formal, parsable reasoning.

## Design decision (honest adaptation)
Proposal wanted audits of live outputs. Live agent outputs are freeform prose —
no parsable premises; an LLM premise-extractor would itself hallucinate.
Faithful-to-paper build instead audits the MODELS: fixed-seed (20260810)
synthetic ProntoQA-style multi-hop syllogism chains (nonsense predicates,
known proof trees), deterministic ANSWER: yes|no|unknown parsing.
Per model: base_acc, sensitivity (on-chain substitution should flip answer),
specificity (distractor substitution should hold), grounding=min(sens,spec).
Low grounding = "right answer, wrong reasoning" drift — the paper's headline
failure class. Identical probes every run → valid drift signal (pelican_bench
pattern).

## Shipped
- /home/lumen/minerva/predicate_audit.py (local models only via LiteLLM :4000,
  zero cloud spend; temp=0; per-problem commit; aq + ntfy alert if grounding
  < 0.5)
- SQLite: /home/lumen/minerva/predicate_audit.db (runs, results)
- Summary for minerva-eval ingestion:
  /home/lumen/minerva/reports/predicate_audit_latest.json
- systemd: lumen-predicate-audit.{service,timer} installed DISABLED
  (Frank-gated enable). Timer 02,08,14,20:50 UTC — offset from eval :00 and
  factory :35 slots. Staged copies in /home/lumen/minerva/staging/.
- Path deviation: proposal named /data/lumen/scripts/ — that mount is
  backups-only; built in /home/lumen/minerva/ per fleet convention.

## Validation
Smoke (qwen36-local, 2 problems, detached lumen-run): 6/6 correct —
base=yes✓, on-chain→unknown (flip)✓, off-chain→yes (hold)✓. Grounding 1.0.
Full 3-model baseline queued detached (lumen-job-pa-baseline-*), chained
behind the isis pelican rerun to avoid inference contention.

## Rollback
systemctl disable --now lumen-predicate-audit.timer; rm script+units+db.
No stateful changes to any existing service.
