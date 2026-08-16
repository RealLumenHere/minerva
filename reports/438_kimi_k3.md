# wq#438 — Kimi K3 rung (prop 7490bceff002, Scribe #299)
Date: 2026-08-10 · Session: claude-web-0809b

## What shipped vs what was rejected
SHIPPED: kimi-k3 route in litellm_config.yaml -> openrouter/moonshotai/kimi-k3
(verified real on OpenRouter: 1M ctx, $3/M prompt — flagship pricing, marked
EXPLICIT-OPT-IN in model_info; never added to any default chain). Reasoning
model: budget max_tokens generously (config default 4096).
REJECTED (scope cut): the proposed lumen-model-scout auto-appender that
watches OpenRouter and auto-mutates+reloads LiteLLM config — violates the
fleet no-silent-routing-mutation rule (May 28 silent-fallback incident;
model-watchdog docstring codifies it). New rungs stay human/GREEN-gated.

## Incident during apply (self-caused, resolved)
First apply blindly appended the entry to file END — landed after
general_settings, malformed YAML, gateway DOWN ~2 min. Reverted from backup,
verified restore (HTTP 200), re-applied correctly: python insert INSIDE
model_list + yaml.safe_load validation BEFORE restart. LESSON: never append
to litellm_config.yaml; insert before litellm_settings and parse-validate
pre-restart. Backups: .bak.20260810_023745 (pre-v1), .bak.20260810_023958_pre_kimi_v2.

## Validation
kimi-k3 probe HTTP 200 (responds w/ reasoning_content); deepseek-v4-flash
intact HTTP 200; 29 models in parsed model_list. State fact ingested +
state-regen run.

## Rollback
cp litellm_config.yaml.bak.20260810_023958_pre_kimi_v2 litellm_config.yaml && docker restart litellm
