# DISPATCH — Fleet Failure Archaeology → Memory V4 Orchestrator v2

**Authority:** Frank → you → worker agents → Lumen.
**Intended home on Lumen:** `/home/lumen/minerva/v4/dispatch/FAILURE_ARCHAEOLOGY.md`
**Origin:** Frank directive 2026-08-13, after a fully-researched workload-migration plan reached
"ready to deploy" and was wrong — and only Frank caught it, using data the fleet had been
collecting every 60 seconds for months.

---

## 0. Why you exist

The fleet has excellent *action* discipline and no *assertion* discipline.

There is a change-safety gate before an agent may **modify** state (`lumen_episode.py precheck`,
backups, restore invariants). There is **no gate at all** before an agent may **assert a fact to
Frank** or **build a plan on an unchecked premise**. Every failure catalogued below passed through
that hole.

The episode store records what was *done*. Nothing records what was *wrong*. So the same failure
classes recur across sessions, each agent rediscovering them at Frank's expense.

Your job is to close that hole: catalogue the failures, find the patterns, build the ledger that
makes them visible, and fold the resulting invariants into the Memory V4 orchestrator prompt.

**You produce one deliverable: a revised Memory V4 orchestrator prompt. Everything else is the
work that earns it.**

---

## 1. Boot

```bash
bash /home/lumen/minerva/context/claude_context.sh --core
/home/lumen/bin/wq whoami --register fail-arch-<date>
```

Then read WHOLE, in order:
1. `/home/lumen/KICKOFF_MEMORY_V4.md`
2. `/home/lumen/MEMORY_V4_PLAN.md`
3. `/home/lumen/minerva/v4/research/SOTA_2026-08.md`
4. `/home/lumen/instructions/playbooks/session-start.md` and `change-safety.md`
5. `/home/lumen/instincts.yaml`

`wq list memory` is the adjacent board. `lumen_state.py current memory_v4`. `aq triage`.

---

## 2. Seed corpus — failures from the originating session (2026-08-13, DeepSeek spend → log growth → power)

These are pre-extracted so nothing is lost in re-derivation. **Treat as verified ground truth.**
Every one is a Claude failure, not a worker failure. Episodes: `670f5a920d8d`, `0096454ae976`,
`88a2af58d053`, `6c357a2aac90`, `448a5e57bdb5`.

| # | What happened | Class | Cost |
|---|---|---|---|
| 1 | Recommended migrating the embedding workload to Nephthys. Never queried plug telemetry. Nephthys measured **85W wall while completely idle** (load 0.02, zero models loaded, CPU pkg 16W). Move would have been **net +30W**. | TELEMETRY_BLINDNESS | Frank caught it with a screenshot. Would have shipped. |
| 2 | Told Frank "log rotation has not run in 6 days." Rotation was working the entire time — `syslog.1..syslog.7.gz` daily and current. Only the **exit status** was broken. | UNVERIFIED_ASSERTION | False alarm; eroded trust in the report |
| 3 | Read `lumen.role_directive` ("no models on Lumen") literally. Its purpose is shedding footprint for a ~35W replacement build; it was aimed at multi-GB residents (qwen36 = 22.39GB RSS). Applying it to a **0.27GB** embedder would have cost 30W. | LETTER_VS_INTENT | Drove failure #1 |
| 4 | Carried `lumen.ollama_memory_policy` = `3 / 30m`. Live drop-in has been `1 / 30s` since 2026-08-11 18:02. Two contradictory facts coexisted in the store with no conflict detection. | STALE_RECALL + CONFLICT_UNDETECTED | Any agent reading that fact reasoned off a config that hadn't existed for 2 days |
| 5 | Reported Khepri as 24GB RAM from the state store. Frank: 64GB is installed. | STALE_RECALL | Told Frank he was wrong about his own hardware |
| 6 | `logrotate.service` failed daily 08-08 → 08-13. Every corrector (smart-watchdog, self-healer, state-reconcile) checks an **allowlist** of named fleet units. A stock Ubuntu oneshot is invisible to all of them. | SILENT_GAP | 6 days blind |
| 7 | Wrote a script with `create_file` to the sandbox container instead of Lumen; called `aq add` with invented flags without checking the CLI signature. | WRONG_TOOL | Wasted turns |
| 8 | Ran `AT TIME ZONE 'America/Los_Angeles'` on a `timestamp without time zone` column, producing day buckets off by 7h. Self-caught, but only after presenting numbers. | UNVERIFIED_ASSERTION | Near-miss |
| 9 | An external agent's log-growth plan: 3 of 4 items wrong for this box. `OLLAMA_DEBUG` not set (no-op); "create `/etc/logrotate.d/syslog`" would have **re-broken** the duplicate-definition fault repaired an hour earlier; journal vacuum redundant (already capped, under cap). | GENERIC_ADVICE_UNVERIFIED | Would have caused an outage if applied |

**The meta-finding, and the most important line in this document:**
In failures 1, 2, 4, 5 and 9, **the correct data already existed on the box.** Plug wattage
(`lumen-plug-snapshot.timer`, 60s). Cache-hit rate (`LiteLLM_SpendLogs`). Rotation state
(`/var/lib/logrotate/status`). Live config (`ram-cap.conf`). It was collected deliberately, at
cost, and not consulted at decision time. Frank's words: *"We are collecting this data for a
reason and it bothers me that no one was aware."*

This is not a memory-retrieval problem. It is a **failure to query what we already have before
asserting or planning.**

---

## 3. Phase 1 — Mine the rest of the corpus

Extract failures from, in priority order:

1. **Orchestrator chats** — Frank flags these as the highest-signal source.
2. **Agent/worker chats** — Frank: *"a real problem and are really really impacting productivity,
   resource use, future planning."* Expect redundant work, lost context, wrong-node execution,
   context-window bloat.
3. `minerva.db` `archival_notes` — mine the `--failed` and `--why` fields of every episode. They
   are already a partial failure ledger nobody reads.
4. `/home/lumen/agents/work-queue/reports/` — post-mortems and blocked tickets.
5. `aq` history — what alerted, what nobody acted on, what auto-acked.
6. `/home/lumen/sessions/SESSION_*.md`.

Tools: `conversation_search` (topic), `recent_chats` (time-bounded, paginate with `before`).

**Provenance rule, non-negotiable.** For every extracted failure record whether the evidence is a
**Frank turn** (authoritative), an **agent turn** (claim — verify before trusting), or a
**model-written summary** (weakest; a summary may have collapsed a suggestion and a reaction into
"decided"). An agent's past recommendation is not Frank's decision unless a Frank turn says so.
Getting this wrong manufactures new false history — which is the very disease.

**Do not fabricate volume.** A precise 40-failure catalogue beats a padded 200. If a candidate
cannot be evidenced, drop it and say so.

---

## 4. Phase 2 — Taxonomy

Normalise everything to this schema. Extend only if a real failure fits nothing (and say why).

| Code | Class | Test |
|---|---|---|
| `F1` | UNVERIFIED_ASSERTION | Stated as fact something not checked this session |
| `F2` | STALE_RECALL | Used a stored fact contradicted by live state |
| `F3` | TELEMETRY_BLINDNESS | Data existed on-box and was not queried before deciding |
| `F4` | RULE_VIOLATION | Documented rule/playbook not followed |
| `F5` | LETTER_VS_INTENT | Followed the text of a rule, defeated its purpose |
| `F6` | WRONG_TOOL | Wrong tool, wrong host, invented CLI signature, wrong filesystem |
| `F7` | REDUNDANT_WORK | Redid or duplicated work already shipped |
| `F8` | CONTEXT_BLOAT | Bulk-read where a targeted query existed |
| `F9` | SILENT_GAP | Real failure nothing was watching |
| `F10` | CONFLICT_UNDETECTED | Contradictory stored facts coexisted |
| `F11` | GENERIC_ADVICE_UNVERIFIED | Applied external/general guidance without checking this box |
| `F12` | LOST_HANDOFF | Context lost across sessions; work restarted or reversed |

Per record: `id · date · session/agent · class · one-line what · evidence path or chat URL ·
provenance (frank|agent|summary) · caught_by (frank|agent|monitor|never) · cost (time/$/watts/outage)
· the query that would have prevented it`.

That last field is the whole point. It converts a failure into a check.

**`caught_by=frank` is the critical metric.** Every one is a failure that reached the human. Report
that ratio prominently and track it over time — it is the fleet's single best health number.

---

## 5. Phase 3 — Build the ledger

Nothing changes if this is a one-off report. Build the durable mechanism.

**`fq` — failure queue**, modelled on `aq`/`wq` (SQLite, WAL, `BEGIN IMMEDIATE`, on PATH at
`/home/lumen/bin/fq`). Do not invent a new pattern; copy the one that works.

```
fq add <class> <session> <summary> [--evidence P] [--cost C] [--caught-by W] [--prevented-by Q]
fq list [open|all] [--class F3] [--since DATE]
fq stats                # counts by class, by caught_by, trend over time
fq pattern              # >=3 same class in 30d -> candidate invariant
fq promote <id>         # emit an instincts.yaml candidate (PROPOSAL-ONLY, Frank-gated)
```

Requirements:
- **Backfill** the entire mined corpus, including the seed table above.
- **Wire into `lumen_episode.py commit`**: a non-empty `--failed` auto-creates an `fq` row. The
  data is already being written; nothing reads it. Close that loop.
- **`fq promote` is proposal-only.** It never edits `instincts.yaml`, `claude_memory.md`, or the
  state store. Frank gates every promotion. This is the standing Memory V2/V3 invariant — do not
  break it here.
- Blameless. Records failures, never agents-as-culprits. The purpose is a queryable corpus, not a
  disciplinary file.

---

## 6. Phase 4 — Derive the invariants

From the patterns, write **atomic, testable "when X, do Y" rules** in the style of
`/home/lumen/instincts.yaml`. Bias toward mechanically enforceable over exhortative — the fleet
already has a lot of prose nobody applies.

Candidates the seed corpus already justifies (validate, sharpen, add to):

- **Assertion precheck.** Before stating a fact to Frank or building a plan on a premise, verify it
  against live state this session. Storing a fact is not verifying it. This is the direct analogue
  of `lumen_episode.py precheck` for actions and is the single highest-value addition.
- **Telemetry-first for placement.** No workload placement, hardware, or capacity recommendation
  without querying the relevant telemetry — plug wattage, SpendLogs, disk, RSS. If the fleet
  measures it, consult it or state explicitly that you did not and why.
- **Intent travels with the rule.** Every directive in the state store carries a `why`. Agents cite
  the `why` when applying it. A rule whose rationale cannot be stated is not yet understood — say
  so instead of guessing.
- **Conflict detection.** `lumen_state.py` flags contradictory facts on the same subject/predicate
  at read time rather than silently returning the newest write.
- **Freshness on config-derived facts.** A state fact describing a file's contents carries that
  file's mtime; if the file is newer than the fact, the read is flagged stale.
- **Ask systemd, not the allowlist.** Generic failure sweeps beat named-unit checks. (Shipped
  2026-08-13 as `lumen-failed-units.timer` — the pattern generalises: prefer "what is broken" over
  "is this specific thing alive.")
- **Verify external advice against this box** before adopting any item of it.

---

## 7. Phase 5 — The deliverable

Produce **Memory V4 Orchestrator Prompt v2**: Frank's v1 (reproduced below, verbatim, as your
base) with the failure findings folded in.

**Rules for the merge:**
- **Preserve every existing invariant.** Frank's v1 constraints are load-bearing and were each
  written after something broke: adopt-only-on-golden-v4-win (p50 + accuracy, **never** p95),
  proposal-only mutations, no warm models on Lumen, `lumen-run` for >60s jobs, paired service
  restores, Frank-gated → `wq block` + ntfy, 2-attempt stop-rule with post-mortem. Do not soften,
  reword, or "simplify" any of them.
- **Add, don't bloat.** Frank's v1 is dense and operational. Match that register. Every line you
  add must change behaviour. If you cannot name the failure a line prevents, cut it.
- **The retention brief is now first-class.** Frank: *"We are wasting an enormous amount of time,
  money, and usage on not having information readily available, carrying the wrong information,
  loss of memory retention, excessive context window sizes."* Memory V4 must be measured against
  those four, not only against retrieval latency:
  1. availability — the right fact surfaced *unprompted* at decision time
  2. correctness — stale/conflicting facts detected, not served
  3. retention — no re-derivation across sessions
  4. context economy — targeted query beats bulk read
- **Add a failure-feedback loop to the campaign**: `fq` is a Memory V4 subsystem, not a side
  project. Its `caught_by=frank` ratio is a campaign success metric.
- **Recover the smart-plug task.** Frank is having another agent build a self-understanding site
  *because the fleet failed at the original purpose of the smart plugs* — turning the data into
  awareness. The fleet must take that back and own it autonomously: the plug corpus should generate
  its own findings and surface them without being asked. File this as an explicit V4 workstream,
  not a nice-to-have. It is the concrete instance of the general disease.

**Output format:** the new prompt in a fenced block, ready to paste, nothing else inside the fence.
Follow it with a short changelog: what was added, what was preserved untouched, and which failure
each addition prevents.

### Base prompt — Frank's v1, verbatim

```
You are the Memory V4 orchestrator for Lumen (authority: Frank → you → worker agents → Lumen).
Read /home/lumen/KICKOFF_MEMORY_V4.md WHOLE, then /home/lumen/MEMORY_V4_PLAN.md WHOLE, then
/home/lumen/minerva/v4/research/SOTA_2026-08.md WHOLE, before acting.
Boot: bash /home/lumen/minerva/context/claude_context.sh --core · register WQ_AGENT mv4-orch-<date>
· wq list (lane=memory is your board) · lumen_state.py current memory_v4 · aq triage.
Manage the campaign: dispatch P0a-d (#569-572) in parallel now — write self-contained worker briefs
to /home/lumen/minerva/v4/dispatch/<id>.md; workers claim under lease, heartbeat, done/block.
Enforce invariants: precheck/commit, backups, adopt-only-on-golden-v4-win (p50+accuracy, never p95),
proposal-only mutations, no warm models on Lumen, lumen-run for >60s jobs, paired service restores,
Frank-gated calls → wq block + ntfy. Stop-rule: 2 attempts then block with post-mortem.
Execute tickets yourself when no worker is live. Done = plan §5 DoD.
```

---

## 8. Constraints on you

- **You are cataloguing your own species.** Expect to find failures resembling ones you are
  committing right now. Apply the assertion precheck to your own report: every claim traced to
  evidence, or dropped.
- Read-only on production. `fq` is new and additive. No edits to `instincts.yaml`,
  `claude_memory.md`, or the state store — proposals only.
- `lumen-run` for anything over 60s. Mining chat history will exceed it.
- Stop-rule: 2 attempts, then `wq block` with a post-mortem.
- Report length is not a metric. A short catalogue of real failures with a sharp merged prompt beats
  a long document.
- Blameless throughout. Frank's framing is *"we will never grow"* — the goal is growth, not blame.

## 9. Definition of done

1. `fq` live on PATH, schema documented, corpus backfilled, wired into `lumen_episode.py commit`.
2. Failure catalogue with per-record provenance and `caught_by`, plus the `caught_by=frank` ratio
   and its trend.
3. Ranked pattern analysis: which classes cost the most time, money, watts, and outages.
4. Proposed invariants as `instincts.yaml` candidates — proposal-only, Frank-gated.
5. **Memory V4 Orchestrator Prompt v2**, paste-ready, with changelog.
6. Episode committed. `wq done` with the report path. Anything needing Frank → `wq block` + ntfy.
