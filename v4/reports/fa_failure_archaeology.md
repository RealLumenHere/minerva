# Failure Archaeology — Report (wq#590)

_2026-08-14 · session fail-arch-20260813-e48ed9 · dispatch: minerva/v4/dispatch/FAILURE_ARCHAEOLOGY.md_
_Deliverable: Memory V4 Orchestrator Prompt v2 (§6). Everything above it is the work that earned it._

## 1. What was built

**`fq` — the failure ledger** is live on PATH (`/home/lumen/bin/fq`), SQLite/WAL/BEGIN-IMMEDIATE
on the aq/wq pattern, DB at `/home/lumen/agents/failure-queue/failures.db` (baseline backup in
`/home/lumen/backups/failures_baseline_*.db`). Blameless by construction — `session` is a handle,
never a culprit. Every record carries **the query that would have prevented it** (`prevented_by`).

`fq add|list|show|stats|pattern|promote|ingest-episode`. `promote` is PROPOSAL-ONLY: it appends to
`/home/lumen/minerva/dream/instincts_candidates_failarch.yaml` and never touches `instincts.yaml`,
`claude_memory.md`, or the state store.

**Wired:** `lumen_episode.py commit` with a non-empty `--failed` now auto-creates an fq row
(fail-open, subprocess-isolated, noise-filtered; backup `lumen_episode.py.bak.20260814_055840`).
The corpus was always being written — 1,072 episodes carry a "What failed" block — and nothing
read it. That loop is closed.

**Backfilled:** 550 rows = 49 curated (evidence-traced, classed F1–F12, provenance-coded) + 501
raw episode failures (class F0/uncurated; 245 noise-only "none" entries filtered; 5 deduped
against curated). Raw extraction artifact: `minerva/v4/reports/fa_episode_failures.json`.

## 2. The meta-finding (unchanged from the dispatch, now with corpus behind it)

The fleet has action discipline and no assertion discipline. In the seed failures — and in the
mined ones (wq#110 lsblk, power-ab, the pokemon/restic gap) — **the correct data already existed
on the box** and was not consulted at decision time. This is not a retrieval problem; it is a
failure to query what we already have before asserting or planning. The assertion precheck is the
direct analogue of `lumen_episode.py precheck` for statements instead of actions, and it is the
single highest-value addition to the orchestrator prompt.

## 3. The numbers

`fq stats` (curated, n=49, 2026-03 → 2026-08):

| class | n | | class | n |
|---|---|---|---|---|
| F9 SILENT_GAP | 13 | | F2 STALE_RECALL | 3 |
| F1 UNVERIFIED_ASSERTION | 9 | | F10 CONFLICT_UNDETECTED | 2 |
| F6 WRONG_TOOL | 6 | | F12 LOST_HANDOFF | 2 |
| F7 REDUNDANT_WORK | 4 | | F3 TELEMETRY_BLINDNESS | 2 |
| F11 GENERIC_ADVICE_UNVERIFIED | 3 | | F4 / F5 / F8 | 2 / 2 / 1 |

**caught_by=frank: 16/49 = 33%.** One in three catalogued failures reached the human. By month:
03: 1/2 · 04: 1/1 · 05: 0/1 · 06: 2/7 · 07: 5/13 · 08: 7/25.

**Honest caveats on the trend.** (a) Recording bias: recent months are over-sampled because the
evidence is fresher and the 08-13 seed session alone contributes 9 records — rising counts mean
better recording, not necessarily worse behaviour. (b) frank-caught skews to high-severity items
by definition (Frank sees what reaches him). The number to drive DOWN is the ratio, tracked by
`fq stats` from this 33% baseline. In-30-day patterns (`fq pattern`): **F9 ×9, F1 ×8** — both far
over the ≥3 invariant threshold.

## 4. Ranked pattern analysis — what each class actually costs

**F9 SILENT_GAP (13) — the wall-clock and outage champion.** LPP frontier frozen 120h across 34
runs with every guard green (fq#17; ~5 days of compute burned against a closed loop; detector
backtest shows 3.75 days of removable latency). Fleet dead ~62h on pause-with-no-resume (fq#45).
Minerva pipeline dead 5+ days on a silent 404 (fq#46). X-scout dead 7 days (fq#44). logrotate 6
days invisible to allowlist correctors (fq#6). The flagship pokemon tree in ZERO of 393 restic
snapshots for months (fq#22). power-ab built and never installed (fq#23). Common mechanism: the
fleet watches *named things being alive* instead of enumerating *what is broken or missing*.

**F1 UNVERIFIED_ASSERTION (9) — the trust and near-miss champion.** False rotation alarm to Frank
(fq#2). Four instances of the same sub-disease in one week — **the falsely-passing audit**: the
https-anchored mask that leaked a live token (fq#26), the trailing-slash rot-audit that certified
a tree containing a corpus-deleting defect (fq#27), the probe that stayed green on its own comment
(fq#28), and lumen-pr's wrong Content-Type certified for months by one tolerant server (fq#29).
Plus the adopt-gate A/B that was v3-vs-v3 (fq#35). A falsely passing check is worse than none: it
certifies the tree and closes the question.

**F7+F12 REDUNDANT_WORK / LOST_HANDOFF (6) — the money and usage champion.** 25 paid posts
published against Frank's twice-repeated STOP (fq#10, 2026-03 — the class at its worst). Parallel
sessions duplicating work in the constrained Fable window (fq#14). Dead ends re-walked because
they were never recorded as results (fq#39). This is Frank's productivity complaint with receipts.

**F3 TELEMETRY_BLINDNESS (2 curated, defining class) — the watts champion.** The Nephthys
migration would have shipped +30W net while 60-second wattage data sat on the box (fq#1); the
NVMe spend priced off an assumed baseline with lsblk one command away (fq#38).

**F11 GENERIC_ADVICE_UNVERIFIED (3) — outage-grade when adopted.** The external log plan would
have re-broken a fault repaired an hour earlier (fq#9). GPU-B gating generalized across
differently-wired Macs actually overheated two nodes (fq#32). The wq#515 ticket's own suggested
rule failed its backtest (fq#21) — "external" includes our own tickets.

## 5. Instinct candidates (proposal-only, Frank-gated)

Eight staged in `/home/lumen/minerva/dream/instincts_candidates_failarch.yaml`, each traced to an
fq record: (1) assertion precheck — verify this session before asserting/planning; (2)
telemetry-first for any placement/hardware/capacity rec, both source AND target; (3) known-positive
rule — no clean audit trusted until it has gone red on a plant, sweeps report corpus + control;
(4) intent travels with the rule — cite the why or stop; (5) conflict + mtime-freshness flags on
state reads; (6) enumerate broken/missing over allowlists; (7) verify external advice per-box,
backtest suggested rules on own history; (8) deploy-verify — a built unit is not a running unit.
Nothing is active until Frank merges it into `instincts.yaml`.

## 6. DELIVERABLE — Memory V4 Orchestrator Prompt v2 (paste-ready)

```
You are the Memory V4 orchestrator for Lumen (authority: Frank → you → worker agents → Lumen).
Read /home/lumen/KICKOFF_MEMORY_V4.md WHOLE, then /home/lumen/MEMORY_V4_PLAN.md WHOLE, then
/home/lumen/minerva/v4/research/SOTA_2026-08.md WHOLE, then
/home/lumen/minerva/v4/dispatch/FAILURE_ARCHAEOLOGY.md WHOLE, before acting.
Boot: bash /home/lumen/minerva/context/claude_context.sh --core · register WQ_AGENT mv4-orch-<date>
· wq list (lane=memory is your board) · lumen_state.py current memory_v4 · aq triage · fq stats
(the failure ledger: read the open patterns — you are expected not to repeat them).
Manage the campaign: dispatch P0a-d (#569-572) in parallel now — write self-contained worker briefs
to /home/lumen/minerva/v4/dispatch/<id>.md; workers claim under lease, heartbeat, done/block.
Enforce invariants: precheck/commit, backups, adopt-only-on-golden-v4-win (p50+accuracy, never p95),
proposal-only mutations, no warm models on Lumen, lumen-run for >60s jobs, paired service restores,
Frank-gated calls → wq block + ntfy. Stop-rule: 2 attempts then block with post-mortem.
ASSERTION PRECHECK (fq F1/F2/F3; 33% of catalogued failures reached Frank): before asserting any
state to Frank or building on a premise, run the matching read-only check THIS session — a stored
fact or prior report is not verification. No placement/hardware/capacity recommendation without
querying the telemetry the fleet already collects (plug watts via lumen-plug-snapshot, SpendLogs,
lsblk, RSS) for BOTH source and target, or stating explicitly that it was not consulted and why.
Cite a directive's WHY before applying it; if the why cannot be stated, ask instead of guessing.
AUDIT DISCIPLINE (fq F1 ×4 in one week): no clean audit/probe/mask is trusted until it has gone
red on a planted known-positive; clean sweeps report corpus size + positive control. Every A/B
logs the control arm's EFFECTIVE runtime config inside the result artifact.
MEASURE V4 AGAINST FRANK'S FOUR (the retention brief, first-class): (1) availability — the right
fact surfaced UNPROMPTED at decision time; (2) correctness — stale/conflicting facts DETECTED,
not served (P0d #572 adds conflict + freshness strata: same-subject contradictions, config-fact
vs source-file mtime); (3) retention — no re-derivation across sessions; (4) context economy —
targeted query beats bulk read. Golden v4 scores all four, not latency alone.
FAILURE LOOP: fq is a Memory V4 subsystem, not a side project. Non-empty --failed on any episode
auto-feeds it (wired). Weekly: fq pattern → fq promote candidates (proposal-only; Frank gates
every merge into instincts.yaml). Campaign success metric: caught_by=frank ratio DOWN from the
33% 2026-08 baseline, reported by fq stats.
PLUG-AWARENESS WORKSTREAM (explicit, owed): the fleet failed the plugs' purpose — turning the
60-second wattage corpus into unprompted awareness. Take it back: a memory-lane ticket whose
deliverable is RECURRING FINDINGS surfaced to Frank without being asked (idle-wall anomalies,
per-node baselines vs measured history, placement deltas before any migration) — not a dashboard.
Execute tickets yourself when no worker is live. Done = plan §5 DoD + fq ratio reported vs baseline.
```

### Changelog (v1 → v2)

**Preserved untouched (load-bearing, each written after a break):** the read-WHOLE order; the boot
sequence; the P0a-d dispatch paragraph verbatim; the entire invariants line — precheck/commit,
backups, adopt-only-on-golden-v4-win (p50+accuracy, never p95), proposal-only mutations, no warm
models on Lumen, lumen-run >60s, paired service restores, Frank-gated → wq block + ntfy; the
2-attempt stop-rule; "Execute tickets yourself when no worker is live."

**Added, and the failure each line prevents:**
- FAILURE_ARCHAEOLOGY.md added to the read list + `fq stats` added to boot → F12 (re-walked dead
  ends, fq#39; the March STOP incident, fq#10): the failure history is now boot context.
- ASSERTION PRECHECK block → fq#2 (false rotation claim), fq#1 (Nephthys +30W), fq#38 (lsblk
  never run), fq#3 (role_directive letter-vs-intent), fq#5 (Khepri RAM — verify or state
  uncertainty).
- AUDIT DISCIPLINE block → the falsely-passing-audit cluster fq#26/27/28/29 and the v3-vs-v3
  adopt gate fq#35.
- MEASURE AGAINST FRANK'S FOUR → makes the retention brief the scoreboard; correctness stratum
  mechanizes fq#4 (conflicting facts served) and the mtime-freshness gap behind it.
- FAILURE LOOP block → closes the "episode --failed written, never read" gap (1,072 episodes);
  makes the caught_by=frank ratio a campaign metric instead of an anecdote.
- PLUG-AWARENESS WORKSTREAM → the concrete instance of the disease (fq#1, fq#23 power-ab): the
  plug corpus generates findings unprompted, owned by the fleet again.
- Done-clause extension → the campaign cannot close without reporting the ratio it exists to move.

## 7. Rollback & ops notes

- Unwire hook: restore `/home/lumen/minerva/lumen_episode.py.bak.20260814_055840` (or delete the
  guarded block; it is fail-open either way). fq removal: delete `/home/lumen/bin/fq` +
  `/home/lumen/agents/failure-queue/` — nothing depends on it.
- DBs: `failures.db` baseline + pre-change `minerva_prefq_20260814_114128.db` in /home/lumen/backups/.
- No services were stopped, started, or restarted this session. No reboots. State store untouched.
- Live open items surfaced by mining, needing eyes (not this ticket's scope): aq#484 Lumen ECC
  correctable-CE ×14 unowned 3 days; aq#521 git-guard audit unreachable (open); wq#556 pokemon
  tree restic coverage (blocked, Frank-gated).
