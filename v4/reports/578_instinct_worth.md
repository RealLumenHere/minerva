# wq#578 [mv4-P6] — Instinct effectiveness loop (GOVERNED)

_Agent mv4-drain-2d5787 · 2026-08-15 · minerva.db backed up before the migration
(`minerva.db.bak.20260815_082832.pre_migrate`, via `lumen_migrate.py up`)._

## What shipped

- **migrations/0011_wq578_instinct_worth.sql** — additive `instinct_worth` (rollup)
  + `instinct_worth_evidence` (audit trail: which episode supported which
  instinct's adhere/violate count, and how). Applied via `lumen_migrate.py up`
  (auto-backup + transaction). `applied=12 pending=0`.
- **lumen_instinct_worth.py** — the pipeline. Reuses wq#576's Memory Worth
  machinery (`lumen_salience.worth_stats`, the same Beta-Bernoulli estimator
  `note_salience` uses) and its outcome labels (`note_hit_outcomes`, populated by
  every `lumen_episode.commit()` since #576 shipped) — does **not** reimplement
  either, per the ticket's explicit sequencing note ("#578 depends on #576's
  counters existing; it should not re-implement them").
- **lumen_dream.py** — wired `propose()` into `run()`, positioned **after**
  `write_instincts()` since it annotates the same output file rather than owning
  its own.

## Design: for each of the 16 accepted instincts

```
instinct_worth = Pr[success | instinct ADHERED]     (Beta-Bernoulli posterior mean)
mirror         = Pr[success | instinct VIOLATED]
delta          = worth_adhere - worth_violate        -- THE signal, not the level
```

**Adherence detection is mechanical, never an LLM judge** (same discipline that
keeps golden v4 exact-match), per the ticket's two named proxies:

- **VIOLATION** — an fq ledger row (`/home/lumen/agents/failure-queue/failures.db`,
  wq#590, 609 rows) whose `prevented_by` text token-overlaps (Jaccard ≥ 0.35) an
  instinct's rule text. An fq row exists because a failure happened, so a match is
  evidence the matched instinct was **not** followed. The episode is recovered
  from the row's `evidence` field (`episode:<hex-id>`).
- **RELEVANCE → ADHERE** — an episode whose content token-**contains** ≥50% of
  the instinct's own vocabulary (asymmetric containment, not symmetric Jaccard —
  a short rule's words mostly present in a long episode is signal; Jaccard
  against a whole episode would be near-zero for every instinct regardless of
  relevance, purely from length mismatch). Relevant-but-not-violated = adhered.

**Dependency-aware support scoring** (the ticket cites this exact GovMem phrase):
episode population for BOTH classes is bounded to `note_hit_outcomes` rows with a
**resolved** outcome (`success`/`failure`, never `pending`) — an episode with
unknown outcome cannot support `Pr[success|...]` for anything, and counting it
anyway would silently inflate the reported sample size past what the worth
calculation actually used.

## A real bug this caught, before it shipped

First implementation intersected `viol_eps` from `fq_violations()` (which scans
`evidence` for *any* referenced episode id) against relevance's already-bounded
set only for `adhere`, not for `violate`. Two instincts showed `n_violate: 1` while
`worth_violate` stayed at the exact 0.5 prior — the one fq match existed, but its
referenced episode had no resolved outcome yet, so it contributed 0 to succ/fail
while still being counted in `n_violate`. A rule could have shown "1 violation on
record" while the number backing `worth_violate` was uninformative. Fixed by
intersecting `viol_eps` with `outcome_by_ep.keys()` before counting — verified via
the scratch positive-control test both before (bug reproduced) and after (fixed,
re-verified with a real resolved-outcome violation still detected correctly).

## Test discipline (audit-positive-control, scratch-only)

`/tmp/mv4-drain-2d5787/test_instinct_worth.py` — monkeypatches `DB`/`FQ_DB`/
`DREAM_DIR` to scratch copies before any function runs; never touches production
`minerva.db`, `failures.db`, or `instincts.yaml`. Plants:
1. A synthetic episode citing an instinct's rule text verbatim, outcome=`success`
   → must register as ADHERE, `succ_adhere` ≥ 1.
2. A synthetic fq row with `prevented_by` = the same rule text, `evidence` pointing
   at a second synthetic episode with outcome=`failure` → must register as
   VIOLATE, `fail_violate` ≥ 1.

Both fire correctly (`CONTROL RED->GREEN OK`), `persist()` writes the expected row
counts to both tables, and `propose()`'s output file contains a well-formed
`effectiveness:` block citing the target instinct. Re-verified after the bug fix
above. Production `instinct_worth`/`instinct_worth_evidence` confirmed to have
zero decoy rows post-test (`PRAGMA integrity_check` → `ok`).

## Live result, real run

`dream/instincts_candidates.yaml` now carries an `effectiveness:` section for all
16 instincts, alongside the pre-existing `candidates:`/`weak_singletons:` blocks
`write_instincts()` owns. **Honest premise note**: on today's corpus, `note_hit_
outcomes` has **9 rows total, 4 resolved** (2 pre-existing `failure`, 1 newly
resolved `success` — my own wq#577 episode, resolved live via `_wq_verdict` once
`wq done 577` landed, a nice end-to-end confirmation that the outcome joiner
actually works — plus 1 more `failure`). With only 4 outcome-labeled episodes and
a 50%-containment relevance bar, **zero instincts clear `sufficient_n` (≥3) today**
— every proposal reads "insufficient data, no proposal." This is the same shape
of finding wq#576 reported for its own forgetting proposer ("age>180d matches
ZERO notes today") and wq#577 reported for two of its three signals: the
machinery is real, tested end-to-end with synthetic positive controls, and wired
into the weekly timer to accumulate real signal as more episodes get outcome-
labeled — but there is not yet enough real data to responsibly propose a
confidence change on any of the 16 rules, and the output says so plainly rather
than manufacturing a number.

## Standing law honored: GovMem 0/133, Frank-gated accept unchanged

`propose()` never writes `/home/lumen/instincts.yaml`. It writes descriptive text
(`note: "..."`) into `dream/instincts_candidates.yaml` for Frank to read — the
same file `write_instincts()` already produces weekly, same proposal-only status.
No auto-promotion path exists anywhere in this module.

## What I did not do

- Did not lower the 50%-relevance-containment or 0.35-violation-Jaccard
  thresholds to manufacture non-zero candidates on 4 data points — see honest
  premise note above.
- Did not touch `/home/lumen/instincts.yaml`, `archival_notes`, `note_merges`, or
  any wq#575/#576 table structure — additive only (`PRAGMA integrity_check` →
  `ok`, `lumen_migrate.py status` → `applied=12 pending=0`).
