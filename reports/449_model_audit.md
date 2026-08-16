# wq#449 — Known-answer correctness audits (prop c1fdd68427b6, Scribe #310)
Date: 2026-08-10 · Session: claude-web-0809b

## Gap verified real
lumen-model-watchdog (wq#90) = liveness only ("Reply OK" -> HTTP 200 +
choices). pelican_bench (wq#441) = LOCAL models. No correctness check
existed for cloud rungs — a degraded upstream passes the watchdog.

## Shipped
- /home/lumen/minerva/model_audit.py — 4-probe deterministic battery
  (arith 17x23, strict JSON emit+parse, exact-word compliance, unit
  conversion) against the cloud roster; model_audit.db; edge-triggered
  aq+ntfy alert on pass-rate crossing below 0.75 (no per-run spam).
- Inherited watchdog discipline: NEVER touch messenger-chat/messenger-re
  (LiteLLM bug #22249); NEVER audit gemini-flash (20 req/day opt-in rung,
  aq#352 lesson).
- lumen-model-audit.{service,timer} 12h (03:40/15:40 UTC), DISABLED
  (Frank-gated).

## Validation
Live baseline run 1: all 5 cloud rungs (cerebras-llama, cerebras-oss-120b,
deepseek-v4-flash, deepseek-v4-pro, mistral-small) 4/4.

## Related fix
Corrected wq#460 report: gemini-flash IS a route (opt-in, 20 RPD), excluded
by quota policy — not "nonexistent" as originally written.

## Rollback
Disable timer; rm script+units+model_audit.db.
