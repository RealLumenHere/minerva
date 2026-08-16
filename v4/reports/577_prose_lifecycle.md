# wq#577 [mv4-P5] — Prose lifecycle: claude_memory.md section demotion pipeline

_Agent mv4-drain-2d5787 · 2026-08-15 · minerva.db backed up before the migration
(`minerva.db.bak.20260815_081143.pre_migrate`, via `lumen_migrate.py up`)._

## What shipped

- **migrations/0010_wq577_prose_demotions.sql** — additive `prose_demotions` table
  (archive-not-erase ledger, mirrors wq#576's `note_archives` shape). Applied via
  `lumen_migrate.py up` (auto-backup + transaction). `applied=11 pending=0`.
- **lumen_prose_lifecycle.py** — the pipeline. `propose()` (read-only, writes a JSON
  plan under `dream/`), `apply_plan()` (operator-invoked, moves section text
  claude_memory.md → context/claude_memory_archive.md, archive-not-erase, full
  pre-image backups, auto post-apply reindex when paths match production), `restore()`
  (paired reversibility from a saved pre-image snapshot).
- **lumen_dream.py** — wired `propose()` into `run()` (the existing weekly
  `lumen-dream.timer`). Read-only; **never** calls `apply_plan()` — claude_memory.md
  stays proposal-only, per standing law, unconditionally (not flag-gated darkness,
  a hard invariant).
- **lumen_federate.py** — added `context/claude_memory_archive.md` as a federated
  source (`source_kind='prose-archive'`) so demoted sections have somewhere to
  remain retrievable, per the ticket's P1 dependency. **Gated dark** — see below.

## Signals implemented (age + superseded-by-store + zero recall hits, per the ticket)

| Signal | Source | Real on today's corpus? |
|---|---|---|
| `age_days` | date parsed from section title | **Yes** — 34 sections >90d, 18 pass every floor |
| `fed_hits==0` | `note_hits` joined to `federated_chunks` by section, **excluding bench/guard/selftest callers** (see bug below) | Yes but weakly discriminating: federated tier is <24h old, `MEMORY_V4_FEDERATED` defaults off, so hits are near-universally 0 today regardless of a section's real value |
| `stale_by_store` | `lumen_dream.stale_prose_scan` mapped line→section | **0 findings today** (verified live) — nothing in the prose currently contradicts the bi-temporal store |
| `note_cov>=0.95` | cosine vs `archival_notes`, same population `lumen_federate.dedup()` already flags as "wq#577 demotion candidates" | **0 pairs today** (verified via the same-day `dream/federated_dedup_20260815_0229.md`) — the prose corpus has not yet been distilled into notes |
| `self_superseded` | non-newest member of `tag_sections.py`'s `DATED_CORE_GROUPS` (e.g. an older "CURRENT STATE OVERRIDES" block) | Real, 1 hit today |

**Honest premise note** (age + verbatim from the ticket, restated because it matters):
two of the three named signals are correctly wired against real tables but return
**zero** on today's corpus — the same shape of finding wq#576 reported for its own
forgetting proposer ("age>180d matches ZERO notes today"). `age` is the signal doing
real work right now. Every candidate still carries its `reasons` list so a demotion
is never silently "it's old" when the file disagrees.

**Live result, real run**: `dream/prose_demotion_plan_2026-08-15.json` (generated
08:16:16Z, pre-poisoning — see bug below) — **18 candidates**, live file **122,434
tok est vs a 60,000 budget (over)**. First candidate: `013_current-state-overrides-
2026-05-13-core`, age 94d, self-superseded by the 2026-06-07 block, note_cov 0.78.

## Standing law honored: claude_memory.md is proposal-only

`apply_plan()` exists, is fully tested (round-trip verified, see below), and is
documented as the operator's CLI. **It was never invoked with `--confirm` against the
real file this session.** `prose_demotions` in production is empty (`select count(*)
... = 0`) — zero real demotions happened. This is deliberate, not incomplete: the law
is unconditional ("nothing auto-edits it"), not a 0-default flag an agent can flip.

## Test discipline (audit-positive-control, scratch-only)

`/tmp/mv4-drain-2d5787/test_prose_lifecycle.py` — never touches the real
claude_memory.md/claude_memory_archive.md/minerva.db (all three monkeypatched to
scratch copies first). Planted a synthetic 200-day-old zero-hit section:
1. **CONTROL RED** — decoy detected as a candidate (`reasons=['age=200d',
   'zero_hits']`), proving the detector actually fires before trusting a clean run.
2. **APPLY** — decoy left the live copy, landed in the archive copy with the banner.
3. **LEDGER** — `prose_demotions` row written, `restored_at` NULL.
4. **RESTORE** — decoy back in the live copy, `restored_at` stamped.
All four checks pass (`ALL CHECKS PASSED`), re-verified after both bugs below were
fixed.

## Two bugs the testing caught (neither shipped uncaught)

**1. Isolation leak in `apply_plan()`'s post-apply reindex.** `lumen_federate.py`
hardcodes its own `MEM`/`DB` constants (no path-injection support). The first
scratch-test run called `apply_plan()` with monkeypatched paths, but its reindex step
imported `lumen_federate` fresh and called *its* paths — which pointed at
**production**. Verified blast radius: `claude_memory.md` itself was untouched
(mtime unchanged); the only effect was 4 additive re-embedded rows in the already-
existing `federated_chunks` table (same idempotent operation any ordinary
`lumen_federate.py index` run performs) — benign, but unintended and now fixed with a
path-equality guard (`lumen_federate.MEM.resolve()==MEM.resolve() and
lumen_federate.DB==db_path`), re-verified to correctly SKIP under the scratch test
and not regress `federated_files.indexed_at` for production.

**2. `fed_hits_by_section()` counted synthetic bench traffic as real usage.** Running
this ticket's own golden-bench A/B (239 queries × 3 arms against production, see
below) logged real `note_hits` rows for whatever federated chunks the bench touched,
via the existing fail-open tap at `search_v3` return. Without caller exclusion this
collapsed the candidate set from 18 to 4 mid-session — a golden set built to exercise
every corner of the corpus looks exactly like heavy real usage if the caller tag is
not filtered. Fixed by reusing `lumen_salience._excl_sql('h.caller')` (the same
`bench,guard,selftest` exclusion `rollup()` already applies) in the LEFT JOIN.
Re-verified: candidate count back to 18. For scale: **18,840 of 19,018** `note_hits`
rows fleet-wide are currently `bench/guard/selftest` — wq#571's research called this
"the majority of the would-be volume," and it just proved itself twice in one
session.

## Adopt-or-record: the federated archive source ships DARK

Ticket requirement: "Depends on P1 so demoted sections stay retrievable via federated
index." The mechanism is **built and correct** (verified: indexing
`claude_memory_archive.md` produced 157 chunks, 0 embed failures) but **not live** —
measured regression, gated off.

Golden set: `eval/golden_v4_frozen_20260814.jsonl`, sha256
`8ba5afb0ce12585606b8dcff5ffb8245af24b0f0f32ccbddf3d4121f1bbf46b7` (wq#595
semantics). Isolated A/B, **only variable = presence of the `prose-archive` source**,
both arms `MEMORY_V4_FEDERATED=1`:

| Arm | R@1 | MRR | trap@1 | trap@1 qids |
|---|---|---|---|---|
| federated ON, without archive (`results_wq577_fed_noarchive_20260815_0821.json`) | 0.749 | 0.816 | **0.188** (3/16) | 230, 238, 241 |
| federated ON, with archive (`results_wq577_federated_with_archive_20260815_0819.json`) | 0.749 | 0.815 | **0.250** (4/16) | 230, 238, 241, **243** |

qid243 is `class=abstention`, query "when did lumen memory migrate to pgvector"
(trap substring "pgvector" — MEMORY_V4_PLAN.md §4 non-goals explicitly rules pgvector
out). With the archive indexed, legacy pre-decision pgvector discussion in
`claude_memory_archive.md` newly ranks #1 for this query, flipping a correct
abstention-shaped miss into a trap@1 hit. Per wq#595 law ("trap@1 ANY rise
disqualifies"), this is a real, attributable regression with **zero offsetting
federated-coverage gain** in this bench (federated R@1 was already 0.7 in both arms —
that lift is pre-existing #573 prose-section coverage, not this change).

**Action taken**: gated behind its own flag, `MEMORY_V4_PROSE_ARCHIVE_SOURCE`
(default `0`), **independent of `MEMORY_V4_FEDERATED`** — so turning federated recall
on for other work (e.g. future #574-style tail-attack tuning) does not silently
inherit this regression. `lumen_recall2.py`'s federated scoring has no per-
`source_kind` filter, so the gate lives at index time (the rows are never written)
rather than read time, and `index()` self-heals: a dark run actively purges any
`prose-archive` rows left over from a prior ON period rather than trusting a manual
cleanup step (verified: inserted a stub row, ran `index()`, confirmed purge log line
and zero rows after). Production `federated_chunks` today: `prose-section=395,
session-archive=16`, **zero** `prose-archive` rows — confirmed clean after this
session's experiments.

**Follow-up candidate** (not filed as a ticket — flagging per "no silent caps"): once
demoted content actually needs to be retrievable (i.e., once an operator runs
`apply_plan --confirm` for real), the qid243-class failure should be revisited —
likely by dating/negation-marking the legacy pgvector discussion during a real
demotion pass, or by scoping the archive source to sections demoted *by this
pipeline* rather than the whole pre-existing 197KB archive (which also carries the
old ad-hoc `archive_tail.py`/`archive_runbook.py` dumps, unrelated to wq#577).

## Token budget target for the live file

`live_budget_status()`: chars//4 estimate (same convention `claude_memory_index.md`
already uses), default budget `MEMORY_V4_PROSE_BUDGET_TOK=60000`, override via env.
Current: **122,434 tok est, over budget by ~2x**. Reported in both the dream report
and `propose()`'s plan file; not enforced (informational only, per the ticket's
"token budget target" phrasing — no gate implied).

## Floors (in code, not convention)

- `is_core(title)` (tag_sections.py, includes the `#CORE`-tag + `CORE_SECTION_TITLES`
  set + the dated-supersession demotion) — core sections are never candidates.
- Undated sections are exempt by construction — not enough information to judge age.
- `age_days <= MEMORY_V4_PROSE_FLOOR_DAYS` (default 30) — a fresh section has not had
  a fair chance to be retrieved, same rationale as wq#576's note-layer floor.

## What I did not do

- Did not run `apply_plan --confirm` against the real claude_memory.md (standing law).
- Did not file a follow-up ticket for the qid243 archive-content issue (flagged
  above; filing is the orchestrator's/Frank's call, not mine to add to the queue
  mid-drain).
- Did not touch `archival_notes`, `note_merges`, or any wq#575 governance trigger —
  additive only, verified via `PRAGMA integrity_check` (`ok`) and
  `lumen_migrate.py status` (`applied=11 pending=0`) after the migration.
