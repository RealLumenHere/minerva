# wq#444 — EXECUTE-ASK-REFUSE routing in lumen-evaluator (prop b5d1bdebee4c, Scribe #305)
Date: 2026-08-10 · Session: claude-web-0809b

## Premise verification
Paper REAL: arXiv:2607.13594 "SAFETY SENTRY: Context-Aware Human Intervention
via EXECUTE-ASK-REFUSE Routing" (Chen/Hu/Wang, ShanghaiTech). Reframes binary
safe/unsafe guarding as per-instance three-way routing with a single tunable
threshold for the autonomy/safety tradeoff.
PREMISE CORRECTION: proposal claimed lumen-evaluator does "action gating" —
false. It is the agent5 research-draft quality gate (Redis gen->eval->
done/review/dead). Action gating already IS three-way via minerva_factory
GREEN/YELLOW/RED + lumen_exec approvals. The paper's trained guard checkpoint
is not reproducible locally; the pattern is.

## Shipped (honest adaptation)
lumen-evaluator gains a first-class ASK route: judge now emits confidence
0-1; when routing is enabled, any ACCEPT/REJECT below the ask threshold goes
to Q_REVIEW (human) instead of being trusted — "don't execute or refuse on a
guess." Files:
- /home/lumen/autonomy/agent5/evaluator.py (backup evaluator.py.bak.20260810_014828)
  - SYS prompt: + "confidence" field
  - judge(): parse/clamp confidence; absent/malformed -> 1.0 (pre-EAR behavior)
  - handle(): flag-gated ASK branch; structural rejects (deterministic) exempt
- Config keys (registry-overridable, code defaults): ear_enabled=False,
  ear_ask_threshold=0.55. FLAG DEFAULT OFF -> byte-identical behavior today.
  Enable: add {"ear_enabled": true} to active config_registry.json version.

## Validation
- py_compile OK; import check confirms flag OFF.
- Unit test, fake redis, 5 cases: low-conf ACCEPT->ASK/review; high-conf
  ACCEPT->done; low-conf REJECT->ASK/review; structural reject->normal path;
  flag OFF + low conf->done (unchanged). ALL PASS.
- lumen-evaluator restarted 01:49 UTC, active, clean logs ("evaluator up").

## Rollback
cp evaluator.py.bak.20260810_014828 evaluator.py && systemctl restart lumen-evaluator
