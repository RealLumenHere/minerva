# wq#575 [mv4-P3] — lumen-memoryd + Mem0/Qdrant FREEZE

_Session `mv4-w575-05322f` · 2026-08-15 PDT · lane `memory` · slock `minerva-db` held throughout._
_Spec: `v4/research/memory_service_design.md` (#570). Companion: `575_mem0_diff.md`._

## Verdict

Shipped: freeze, schema, kernel, daemon, drills, reader cutover, diff evidence. Golden v4 shows
**no regression** (it moved up). Retirement of Mem0/Qdrant is filed for Frank, not taken here.

## 1. FREEZE — live 02:57Z, verified 03:03Z

`lumen_mem0.add_fact()` is a fail-soft no-op returning a `FROZEN` sentinel and logging to
`/home/lumen/logs/mem0_freeze.log`; `seed_facts.main()` refuses up front; the selftest is now a
read selftest. **No env escape hatch** — a flag a lane can set is not a freeze. Thaw = restore
`lumen_mem0.py.bak.20260815_025626_pre_wq575_freeze`.

Write-exclusivity is established by **capability, not intention** (§0.4's lesson): a sweep of
14,430 production `.py` files found Mem0 referenced in exactly 3 files, all under `/home/lumen/mem0`.
Positive control planted and found, then removed. Dify — the only other Qdrant-shaped consumer on
the box — is on weaviate and not running.

Battery `v4/probes/freeze_verify_575.py` — **8/8 PASS**: writes fail soft (no raise), reads still
serve on all four paths (`search_facts`, router `fetch_context`, the real
`lumen_agent_v4._memory_context` import path, `a5_common.memory_context`), a caller loop survives
5 write attempts, `lumen_facts` point_count 30 → 30.

Red control `freeze_redcontrol_575.py` — **VALID**: with an "unfrozen" `add_fact` planted, F1 goes
red and the reads stay green.

Freeze point archived: Qdrant snapshots of both collections + a 30-point payload dump +
`SHA256SUMS.txt` in `/home/lumen/backups/wq575/qdrant/`. Container left **running** — reads need it.

### Two disclosures

1. **The first red control cleared `FROZEN` and reached `seed_facts` for real.** Nothing was
   written — the patched `add_fact` absorbed every call — and it is *proven*: the collection is
   byte-identical to the freeze-point dump (30/30 points, payload hashes equal). The control was
   rewritten to leave `FROZEN` set. It had also clobbered the genuine battery artefact by binding
   `OUT` at import time; fixed, battery re-run clean.
2. **Mem0's client creates payload indexes on connect.** The freeze covers *data* writes, not the
   client's index bootstrap, so `Created index for run_id in collection lumen_facts` appears in
   read-path logs. Re-verified after: 30 points, payloads byte-identical. Worth stating rather
   than claiming a totality the freeze does not have.

## 2. SCHEMA — migration 0008, applied 03:07:03Z

`migrations/0008_wq575_memoryd_governance.sql` via the house runner, in the quiet window between
`minerva-eval` (03:00Z) and `minerva-factory` (03:35Z). Dry-run on a **copy** first, including all
four trigger negative-controls, before prod. `integrity_check ok`, 1197 notes / 906 facts unchanged.

8 additive columns + 3 indexes + immutability trigger + `memory_audit` (append-only, 2 triggers) +
`memory_quarantine`.

**Deviation from the design, deliberate:** §1.1 sketches `schema_version INTEGER DEFAULT 1`, but
`ADD COLUMN` with a default backfills all 1,197 legacy rows as governed-by-a-kernel-that-did-not-exist.
§2.1 states the opposite intent. Nullable wins; the kernel stamps 1 explicitly.

**Residual not closed:** `minerva_memory.py:180` still issues `INSERT OR REPLACE`, which SQLite
implements as DELETE+INSERT and which therefore **does not fire** the BEFORE UPDATE trigger. Closing
it needs a BEFORE DELETE trigger, which would break `lumen_episode.py commit` for every lane. Kernel
writes are safe (I-3 bans the statement); the legacy library path is not. Filed, not wedged in.

## 3. KERNEL + DAEMON

`v4/memoryd/kernel.py` (pure library, no LLM, no weights) + `daemon.py` on **127.0.0.1:8139**
(re-verified free at build time). Contract `v4/schema/memory_contract_v1.yaml`, sha reported by
`/healthz`.

**No models in the daemon — measured, not assumed:** a full `search_v3` through the import path
peaks at **54 MB RSS**; embedding is an out-of-process HTTP call to Ollama. `MemoryMax=1G` makes a
violation fail loudly.

Identity is server-derived and a lane cannot spell its own name: the lane is read **out of the wq
ticket the agent demonstrably holds** (`tasks.lane` — the design says "queue.db" without naming the
table; it is `tasks`, not `tickets`). Trust is derived, never read from the body.

## 4. DRILLS — 20/20, four of them planted red

`v4/probes/drills_575.py`. Highlights: kill-9 atomicity held across 3 forced kills (0 notes without
an audit row, 0 audit rows without a note) **and the checker was proven able to fail** by planting an
orphan note; append-only and immutability triggers ABORT while the embedding-repair path stays
ALLOWED; forged `trust: operator` from a lane is quarantined with a D-2 audit row and 0 leaked rows;
rate limit 429s with no partial writes; a held write lock yields 503 + `Retry-After`, and the same
call **succeeds** with the lock released (red control); revocation hides the row from a lane while
keeping it on disk and the consumer walk finds the reading lane; `/recall` reproduces `search_v3`
**byte-for-byte** on 5 queries.

Two drills were wrong on the first run and were fixed rather than excused:
- **D5** ran after D10 and got a 429, not a 503 — D10 had spent the lane's write budget, so the
  request never reached the DB. Re-ordered behind a restart (the rate window is in-memory by design).
- **D17** passed for the wrong reason: with no `source_ref` it died at identity resolution and never
  reached the operator gate. With a resolvable identity it now returns the real
  `revocation is operator-only (§4.2)`.

## 5. READER CUTOVER — live, flag-gated

`agent/memoryd_client.py` (fail-open on everything) + `MEMORY_V4_MEMORYD` in `agent/.env`.
Both readers migrated with the 6k cap (wq#138) and the `run_in_executor` offload (wq#467) untouched.
The stale `:591` comment claiming the hook warmed "the Memory V3 retrieval stack" is corrected.
Rollback is `MEMORY_V4_MEMORYD=0` + restart, and never depends on the daemon being healthy.

**The shadow run caught a silent fallback and that is the useful part of this section.** The first
comparison showed byte-identical output on 6/6 prompts, which reads like parity and was actually
*memoryd returning nothing every time* — the client fell back to the legacy router. Root cause: reads
required a held wq claim, and a plain agent process has none. The probe was rewritten to attribute the
serving path, and the daemon gained a read-only `unattributed` identity (reads are the less dangerous
half, §2.5; writes still demand attribution). Re-run: **6/6 served by memoryd**, distinct content,
cap honoured, median 209 ms vs 107 ms legacy.

The cutover then hit the same class of gap in production: `lumen-agent` is a long-running unit, not a
wq session, so it 403'd and fell open to legacy. Fixed with a 0600 **service token** granting a
read-only identity (same one-file rotation pattern as the Gitea credential, wq#529); writes with it
are refused. Confirmed live by audit seq 103: `lane=unattributed agent_id=service:lumen-agent`,
4 hits, on the agent's own warm-up.

## 6. GOLDEN V4 — no regression

`results_wq575_post_20260815_0327.json`, frozen golden `8ba5afb0…` (sha matches, `golden_differs_from_live: false`).

| metric | baseline of record (wq#595) | this run |
|---|---|---|
| R@1 (retrieval-only, n=239) | 0.728 | **0.778** |
| MRR | 0.793 | **0.836** |
| trap@1 (any rise disqualifies) | 0.250 | **0.188** |
| abstention | 0.500 | 0.500 |
| federated R@1 | 0.000 | 0.000 |

Read this as **no regression, not as a win I earned**: `/recall` proxies `search_v3` byte-for-byte
(drill D11), so nothing in this ticket changed retrieval. `minerva.db` moved under the bench (this
ticket's notes, plus #573's federated work), which is corpus drift — exactly what wq#595's
comparability rule warns about. The claim being made is only that the governance layer cost nothing.

## 7. OPEN / FOLLOW-UP

1. **Retirement of Mem0/Qdrant** — filed for Frank with the evidence (`575_mem0_diff.md`). Not mine.
2. **`INSERT OR REPLACE` in `minerva_memory.py:180`** slips past the immutability trigger (§2 above).
3. **Revoked rows are hidden by memoryd, not by `search_v3`.** `lumen_recall2.py` contains no
   `revoked_at`/`superseded_by` predicate, so any direct caller — CLI probes, the bench, `lumen_episode`
   — still sees revoked notes. The lanes are covered because they go through the daemon; nothing else is.
4. **Detectors D-1/D-3/D-4/D-5 are not wired into the dream run.** D-2 (trust escalation) is live and
   fires today. The rest are specified in §3.3 and unbuilt — the "quarantine fed by dream anomaly scan"
   pipe still has a printf at the upstream end.
5. `/propose` currently parks proposals in `memory_quarantine` with `table_name='archival_notes:proposal'`
   rather than the `proposals` table. Functional and audited, but it is not the review surface §5 intends.

Nothing was left stopped. `lumen-memoryd` and `lumen-agent` both active and enabled at session end.
