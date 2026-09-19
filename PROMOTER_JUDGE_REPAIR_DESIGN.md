# Design: Minerva promoter (deploy_ready -> wq) + verify-judge repair

Session orch-minerva-promoter-6f1912, 2026-09-08. Route fable-5-1@high.
Frank brief: deploy_ready proposals never become wq tickets (ticket='-'); ~23
proposals fail the judge on claim-extraction. Judge stays on the free/local rung.

## 1. Root causes (evidence grade in brackets)

### 1a. Promoter gap [A: read from code + DB]
Pipeline: accept -> minerva-eval -> deploy_ready -> minerva-factory (4h timer).
`minerva_factory.cmd_run` consumes every deploy_ready row at once: GREEN
auto-applies, YELLOW -> `staged` (waits on an .approved file nobody writes),
RED -> `planned`. No step anywhere creates a wq ticket. Class: a pipeline stage
whose terminal states have no consumer. Today 45 planned + 66 staged proposals
are parked, incl. the 7 CVE upgrades from wq#1383 (Gitea CVE-2026-66877,
netdata CVE-2026-83603, Grafana 2 CVEs, 3x Langfuse, Actual).

### 1b. Judge: phantom fallback rung [A: config + litellm log]
`VERIFY_JUDGE_MODEL_2=mistral-small` in minerva-proposal.service.d — no such
rung in litellm_config.yaml. Every fallback call is a 4xx; the fallback has
never worked since it was set. Class: an env-named rung that nothing validates
against the gateway's model list.

### 1c. Judge: primary ReadTimeout in the service path [B: log correlation]
Since 2026-09-04, 55% of verify calls fail closed `judge_err:ReadTimeout`
(129/325). Out-of-band the same prompt on deepseek-v4-flash returns in 1.4-2.2s
cache-miss (measured 10x), so this is not model latency. Reproduction is being
captured on the 06:45 UTC run (litellm tap). Regardless of cause the gate has
no per-rung timeout knob and no telemetry of judge latency; both are added so
the next occurrence is diagnosable from the log alone.

### 1d. Context truncation, third occurrence of the wq#1383 class [A]
`FACTPACK_CAP=15500` but the digest is 17,096 chars. `build_context()` slices
`(digest + CLOSED_WORLD + facts)[:CAP]`, so the digest tail (fleet roles, GAPS,
policy) is cut AND every retrieved live fact is dropped. The wq#1383 assertion
covers VERIFY_JUDGE_CTX_CHARS vs the digest CAP but not this cap.
Class: three size constants that must stay ordered (digest CAP <= factpack
budget <= judge window) with no single owner.

### 1e. Grounding: digest never names fleet-stack products [A: rationales]
Post-1383 failures on fleet-stack items: "context does not mention Renovate /
renovate-gitea timer / Home Assistant on Bes / n8n / ESPHome / Dify". The digest
carries `langgenius/dify-api:1.14.2` and `bes.role=...Home Assistant` but never
a line saying the fleet RUNS product X. wq#1386 names this.

### 1f. Claim extraction [A: input_text rows]
`extract_claims` splits on sentence end only. current_state is written as
compound sentences: "Gitea runs on Anubis, but no version is listed in RUNNING
VERSIONS" / "X runs (per the advisory) ...; the fleet update pipeline exists
but ...". One ungrounded clause sinks a supported one, and meta-references
("per RUNNING VERSIONS in LUMEN CURRENT STATE", "per the advisory context",
"the classifier indicates") are judged as if they were claims about the system.

## 2. Changes

### 2a. minerva_promote.py (new)
- Additive column `proposals.wq_ticket INTEGER NULL` (DB backed up first).
- `promote(proposal)`: idempotent on (1) the column, (2) any wq task whose
  detail contains `minerva_proposal=<id>` (recovers if the column is lost),
  (3) wq's normalized-title duplicate refusal.
- Eligible: status in (deploy_ready, planned, staged); green-applied rows are
  not promoted (they are already done).
- Priority: CVE/GHSA/advisory in title/proposed_change/source intel title
  -> pri 1; else yellow -> 2, red/unknown -> 3. Lane: fleet.
- Ticket detail = grounding block: proposal id + md path, source intel
  title/url/source, current_state, proposed_change, rollback, verify scores +
  judge rationale, factory plan id/lane/lane_reasons/change_type/targets/
  apply_cmd/rollback_cmd, and how to close.
- Hook: `minerva_factory.cmd_run` and `cmd_replan` call promote for
  yellow/red after planning. Standalone `minerva_promote.py run [N]` backfills
  parked rows. Fail-open: a promoter error never breaks the factory run.
  Kill switch MINERVA_PROMOTE=0.
- Proposal status is NOT changed by promotion (factory semantics stay); the
  ticket id is the link.

### 2b. minerva_verify.py
- `extract_claims`: strip meta-reference parentheticals/phrases, then split on
  `;` and on `, but ` / `, however ` when both halves have >= 4 words.
- `VERIFY_JUDGE_TIMEOUT` env (default 120) replaces the hardcoded 40.
- Per-sample judge latency logged and stored in verification_log.judge_ms
  (additive column).
- Import-time rung sanity: JUDGE_MODEL / JUDGE_MODEL_2 checked against
  GET /v1/models; a missing rung logs CONFIG BUG and is dropped as fallback.
- Fallback runs 1 sample, not JUDGE_SAMPLES.

### 2c. minerva_factpack.py
- Digest is never truncated. Facts fill (judge window - digest - CLOSED_WORLD).
  Assertion extended: digest CAP <= factpack budget <= judge window.

### 2d. lumen_proposal_digest.py
- New FLEET STACK section after RUNNING VERSIONS: one line per fleet_stack
  entry: `<Product> (<owner/repo>) runs here: <where> [installed: <version>]`.
  Version resolved from container tags / state_facts / binaries / timers.
  Digest CAP raised in lockstep with the judge window (assertion-covered).

### 2e. Rung (Frank constraint: free/local, no paid spend)
- Primary judge -> qwen36-local (Isis llama-server; measured 10-11s with
  cached prefix, 44s cold; correct labels on the probe). Fallback: none that
  is free and alive — deepseek-v4-flash is paid, claude-max-sonnet is dead
  (nothing on :8091), cerebras is 402, gemini-flash is an exhausted 20/day
  tier, mistral-small does not exist. Fail-closed as today.
- Cost: ~25 proposals x 3 samples x ~12s = ~15 min per 4h run on Isis, which
  also serves LPP. JUDGE_SAMPLES env-tunable.

## 3. Test-to-failure
(i) promoter backfill twice -> second run creates 0 tickets; null the column
    on one row and re-run -> recovered from the detail marker, no dupe.
(ii) 797978e9 (Gitea CVE-2026-66877) lands pri 1 lane fleet.
(iii) replay the 09-03..09-07 fleet-stack + judge_err set through the new
    verify (dry, no DB write, no alert); report pass rate before/after; every
    residual failure classified grounded-vs-gate from its rationale.

## 4. Rollback
.bak.<ts> sibling for every edited file; minerva.db backup; the env drop-in
change is one file; promoter hook disabled with MINERVA_PROMOTE=0.
