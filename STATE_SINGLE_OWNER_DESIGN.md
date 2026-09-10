# wq#1700 — state_facts: one owner predicate per fact, lifecycle closure, predicate gate

Source: foreman/research/MINERVA_PIPELINE_MAP.md §3.2, §3.3(3), §4.3, §5 #4.
Supersedes the undeployed wq#1632 artifact (foreman ops/wq1632-lumen-state-lifecycle), see "Why not wq#1632".

## Root cause (evidence grade: measured on live minerva.db 2026-09-10)

Supersession is keyed on `(subject, predicate)`. That is correct for one predicate. It cannot see
two predicates that assert the same thing, or a subject-level change that makes other predicates false.
Three live instances of that one class:

| instance | live rows | mechanism |
|---|---|---|
| synonym predicates | `nephthys.ts_ip` + `nephthys.tailscale_ip`, `gameboy.ts_ip` + `.tailscale_ip`, `bes.hw` + `bes.hardware` (different values), `nephthys.role_status` + `.status`, `s26-ultra.lan-ip` | two owners for one fact; a correction lands on one, the other stays open |
| lifecycle siblings | `nephthys.status = DECOMMISSIONED 2026-08-22` while `serving_model, embed_model, litellm_aliases, awareness_pipeline, mcp_failover, dc_handle_store, ollama_version` are still open | a subject-level change has no link to the operational facts it invalidates |
| ungoverned new axis | `thoth.tailnet_routing = FIXED` (09-08) landed beside `thoth.status = OFFLINE` instead of superseding it (status later hand-corrected by wq#1666) | any writer can invent an axis; nothing makes it say so |

Disproof attempt: is this a bi-temporal bug instead (wrong valid windows)? No. Every pair above has
correct windows for its own key; the history of each key is consistent. The defect is that the key set
does not name the fact.

## Class analysis

Class: **a fact has more than one open owner row, or an owner row that a sibling change already falsified.**
Where else it lives: inventory YAML vs state_facts (double authority, wq#1631, other repo) and prose copies
(wq#1637). This change ends it *inside state_facts*, which every other projection reads.

What makes it impossible rather than fixed once:

1. **Alias canonicalisation in `ingest()`.** `state_registry.json:aliases` maps a synonym to its one owner
   predicate. An aliased write IS a write to the owner, so normal supersession applies. `check` fails if
   an alias row is ever open.
2. **Reserved `lifecycle` predicate**, closed enum `provisioning active degraded offline decommissioned retired`.
   Ingesting `decommissioned|retired` closes every open *operational* predicate on the subject in the same
   transaction (`operational_predicates` / `operational_patterns` in the registry: data, not code).
   Identity/hardware stay open. Closure writes `closed_by = <lifecycle fact id>` and never touches the
   closed row's `source_ref`.
3. **Post-closure refusal.** An operational write to a subject whose open lifecycle is closing is refused
   (`LifecycleClosedError`) unless it is historical (valid_from before the lifecycle fact). Re-commissioning
   = ingest `lifecycle active` first. The contradiction cannot be re-created by a late writer.
4. **Predicate gate.** A predicate is accepted if it is in the registry, already has rows in state_facts
   (the store owns its own vocabulary, no second copy), or the subject is an open namespace
   (`lumen-persona`: LPP's one-predicate-per-slot design, wq#1191; `lpp-probe-persona`: the wq#1191
   probe's throwaway-DB subject. Found by running that probe against this change: 12/12 FAIL before the
   entry, 12/12 PASS after). Otherwise refused with the nearest
   existing names unless `--new-predicate "<why>"`, which records who/why/when in
   `state_predicate_additions`. New axes become deliberate and auditable.
5. **`lumen_state.py check`** — invariant scan, exit 1 on any violation: open alias rows, open operational
   rows under a closing lifecycle, lifecycle value outside the enum, >1 open row per key.

Supersession also now records `closed_by` (lineage for every closed row written from here on).

## Options considered

| option | verdict |
|---|---|
| A. change the unique key to an "axis" column (subject, axis) | rejected: rewrites 1225 rows' identity, every reader keys on predicate, recall/golden set exposure |
| B. separate gated tool beside lumen_state.ingest (the wq#1632 artifact) | rejected: a second write path is itself the double-authority class; CLI, reconciler, LPP bypass it |
| C. gate + alias + lifecycle inside the one `ingest()`, registry as reviewed data, migration once | **chosen** |
| D. LLM contradiction judge | rejected: MEMORY_V2_DESIGN chose deterministic supersession; zero-cost, testable |

## Why not wq#1632 (closed done 07:00Z, never deployed)

Read against the live schema it would have: inserted rows with NULL `id` (state_facts.id is TEXT, not
rowid; `lastrowid` is not the id); overwritten `source_ref` on every closed row (provenance loss);
mapped lifecycle by substring anywhere in status/role prose (run against live rows it marks apep, lpp,
scribe `retired`, aten `decommissioned` and thoth `offline`, all five active, and would close the
operational facts of the first four); assumed `nephthys.lifecycle` exists (0 lifecycle rows live); skipped the out-of-order guard,
`render_tier`, `half_life_days`. Its design intent (lifecycle enum, closure, gate) is kept here.

## Migration (tool: `lumen_state_single_owner_migrate.py`, dry-run default, idempotent)

1. `sqlite3 .backup` copy of minerva.db to /home/lumen/backups/wq1700/.
2. Schema: `lumen_state._migrate()` adds nullable `state_facts.closed_by` and creates
   `state_predicate_additions` (same self-heal pattern as last_confirmed_at / fact_class).
3. Aliases: for each OPEN alias row: owner open -> close the alias row (`closed_by = owner id`, owner wins
   regardless of value); owner absent -> ingest value under the owner keeping valid_from/source_ref, then
   close the alias row. Closed historical alias rows untouched.
4. Lifecycle: reviewed per-subject list, each pinned to the expected prefix of the current `status`
   value. If the prose changed since review, skip and report. Ingested through `ingest()` so closure runs
   on the one code path.
   - decommissioned: nephthys, pbox, tapo-p12
   - retired: leantime, leantime_research_bridge, mem0, llama-gemma4-mtp, llama33-local, lumen-agent
   - NOT backfilled: mirror (store says decommissioned 08-28; Frank's current preferences block lists it
     in use for RTD ISP flashing, Frank's statement outranks), wadjet (retired from a ROLE, box kept with
     WoL armed; node lifecycle never stated).
5. `check` before/after; report JSON to the ticket.

## Test to failure

tests/test_lumen_state_single_owner.py on a temp DB: alias write supersedes owner; alias row never opens;
unknown predicate refused w/ hint; `new_predicate` records addition and then passes; namespace passes;
lifecycle enum enforced; decommission closes operational only, keeps identity, sets closed_by, keeps
source_ref, never makes valid_to < valid_from; operational write after decommission refused; historical
write before decommission allowed; re-commission then operational write allowed; out-of-order guard intact;
reconfirm intact; `check` detects each violation class on a hand-corrupted DB; migration idempotent
(second apply = zero changes). Existing suites (staleness, coverage, reconcile) green. Live: dry-run on a
copy of prod, apply on a copy, `check` clean, then prod.

## Rollback

Code: revert the PR + `lumen-pr ffprimary`. Data: every change is a close or insert; restore the
.backup copy if needed. `closed_by` / `state_predicate_additions` are additive and inert to old code.
