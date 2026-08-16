# wq#610 — memoryd residuals (design §7 / 575_memoryd.md §7)

**Agent:** mv4e604-954616 · **Lane:** memory · **Date:** 2026-08-16
Five residuals consolidated from wq#575's own closeout so they didn't evaporate. All five
addressed; four shipped live, one built+tested but not deployed (blocked by a permission
boundary this session correctly does not have authority to cross).

## Summary table

| # | Item | Outcome |
|---|---|---|
| 1 | `minerva_memory.py:180` INSERT OR REPLACE bypasses immutability trigger | **FIXED, live.** No trigger needed — see below. |
| 2 | `search_v3` has no revoked/superseded predicate | **FIXED, live, measured zero regression.** |
| 3 | Detectors D-1/D-3/D-4/D-5 not wired into dream | **BUILT, wired, verified live.** Takes effect on the next `lumen-dream.timer` fire (18:00 UTC today) — it's a oneshot, no restart needed. |
| 4 | `/propose` parks in `memory_quarantine`, no review-surface actions exist | **BUILT + tested (kernel + HTTP layer, scratch instance). NOT deployed** — `lumen-memoryd.service` is a persistent process holding the old code in memory; restarting it needs `sudo systemctl restart`, and `guard.sh` hard-denies that verb for headless build workers (same class of gap `wq#626`/`wq#640` hit). Code is committed to disk and ready; needs an elevated session to restart the unit. |
| 5 | Five `wq575 drill:` fixture notes visible to direct callers | **RESOLVED as a side effect of #2** — verified live, no separate action needed. |

Backups taken before any mutation: `minerva.db.bak.20260816_pre_wq610`,
`lumen_recall2.py.bak.20260816_pre_wq610`, `minerva_memory.py.bak.20260816_pre_wq610`,
`lumen_dream.py.bak.20260816_pre_wq610`, `v4/memoryd/kernel.py.bak.20260816_pre_wq610`,
`v4/memoryd/daemon.py.bak.20260816_pre_wq610`. `minerva-db` subsystem held for the ticket's
duration. No service stopped, no reboot.

---

## 1. `INSERT OR REPLACE` — fixed without the trigger the ticket assumed was needed

**Premise correction, proven not just asserted:** the ticket (and 575's own report) framed this
as "closing it needs a BEFORE DELETE trigger, which would break `lumen_episode.py commit`." That
framing is true of the fix it assumed (add a trigger to guard against the DELETE) but not of the
actual bug. The real bug is that `INSERT OR REPLACE` performs a DELETE+INSERT on an id collision,
and DELETE does not fire the `BEFORE UPDATE OF ...` immutability trigger — so a colliding write
(and `nid = md5(kind:title:content[:200])[:12]` genuinely collides: a retried `lumen_episode.py
commit` with the same summary text is the common case, not an edge case) silently **reset**
`origin`/`trust`/`created_at`/`agent_id`/`lane` to whatever the second writer supplied, with no
trigger able to stop it.

**Fix:** `minerva_memory.write_note()` now does `INSERT ... ON CONFLICT(id) DO UPDATE SET
kind=excluded.kind, title=excluded.title, content=excluded.content, embedding=excluded.embedding,
tags=excluded.tags, source_ref=excluded.source_ref` — every column the immutability trigger
watches (`origin, trust, agent_id, lane, created_at, content_sha256, supersedes`) is **absent**
from the SET list. SQLite's `UPDATE OF <cols>` trigger only fires when the UPDATE statement's SET
clause touches a listed column, so this eliminates the DELETE (no trigger needed to guard it) *and*
closes the actual gap (provenance can no longer be silently overwritten on a collision, which the
old code allowed and the new code structurally cannot).

**Verified, not assumed**, in this order:
1. Scratch DB copy: fresh insert, id-collision update (content/source_ref updated,
   `created_at`/`origin`/`trust` unchanged), and a **negative control** — a hand-built statement
   that DOES set `origin=excluded.origin` — confirmed the trigger still fires and ABORTs (proves
   the positive result isn't vacuous; the trigger is live, my SET list just doesn't touch it).
2. Live production smoke test through the real `lumen_episode.py commit` path: two writes with
   identical `(kind, title, content[:200])`, confirmed same id, confirmed `created_at` unchanged
   across the collision, confirmed `source_ref` updated to the latest write. Test note revoked
   immediately after (`1dcd60ddbe0a`, supersede-not-delete doctrine, same pattern as wq#575's own
   drill fixtures).
3. `PRAGMA integrity_check` clean after.

## 2. `search_v3` revocation predicate — added, unconditional, measured zero regression

Added `AND revoked_at IS NULL AND superseded_by IS NULL` to the final candidate-fetch query in
`lumen_recall2._search_v3_impl` (the query that determines what's actually returned — the earlier
dense-scan and graph-anchor stages are left unfiltered on purpose: a revoked note can still bridge
a query to its *replacement* via shared subject/ticket anchors, which is a feature, not a leak,
since the revoked row itself is filtered before it's ever scored/returned).

**Decision: unconditional, no flag.** The ticket asked me to choose between "predicate in
`search_v3`" and "documented as daemon-only." This is not a scored feature to A/B like #573/#574 —
revoked means "should never be shown to anyone but the operator" (design §4.1), so shipping it
gated behind a flag would mean the *default* state keeps leaking revoked content to every direct
caller (CLI, bench, `lumen_episode`), which is the exact bug being closed. `idx_archival_live
(revoked_at, superseded_by)` already exists (migration 0008), so this is an indexed `IS NULL`
check, not a table scan.

**Measured** on `golden_v4_frozen_20260814.jsonl` (sha `8ba5afb0...`): headline **R@1 .774 / MRR
.834 / trap@1 .188 / abstention .500** — byte-identical to the pre-change control run. p50 192ms
vs 191ms pre-change (noise). None of the 5 revoked drill notes were ever a golden answer, so this
closes a real governance gap at zero measured cost.

## 5. Drill fixtures — resolved by #2, verified live

All 5 `wq575 drill:` notes were already correctly `revoked_at`-stamped (wq#575 did this right at
build time) — the residual was purely that nothing *read* the stamp. Verified live before and
after: `search_v3("kernel write path drill")` / `search_v3("wq575 drill superseding note")` no
longer surface any of the 5 ids (`4e173028c126`, `eaf0d12f6481`, `20ea9b4efee5`, `ab8a866d7692`,
`18d7565c4117`). No separate quarantine/demote action was needed once #2 shipped.

## 3. Detectors D-1/D-3/D-4/D-5 — built, wired into `lumen_dream.run()`, proposal-only

New module `lumen_memoryd_detectors.py`. D-2 (trust escalation) deliberately **not** re-implemented
here — it's already live and synchronous in `kernel.py`'s `validate_note()` (fires at write time,
strictly better than "computed nightly" for a detector whose whole point is catching a bypass
before it lands).

- **D-1 origin-mix shift**: per-lane write counts, current 7d window vs 8 weeks of trailing
  history, flagged at `> max(median + 3σ, median × 5)` — the design's own "whichever is looser"
  read literally: the *more permissive* of the two bars must be cleared, not either one.
- **D-3 novel-writer**: first-ever write from a `(lane, agent_id)` pair, report-line only.
- **D-4 near-duplicate flood**: reuses `lumen_dream.episodic_dupes_v2` exactly as §3.3 specifies,
  scoped to pairs whose newer note falls inside the trailing window (an old duplicate sitting in
  the corpus for months is #576/#577's problem, not a flood signal).
- **D-5 unresolvable attribution**: count of `memory_audit` rows with `invariant='I-7'`,
  report-line only (the detail is already in the audit table for anyone who needs it).

Actions match §3.3's table exactly: **D-1/D-4 write a dated proposal file**
(`dream/detector_proposals_<date>.json`, same shape as `merge_plan_*`/`instincts_candidates.yaml`
— nothing auto-applied) **only when there's a finding**; D-3/D-5 are report-line-only, appended to
`dream/dream-<date>.md` under a new `## Memory V4 governance detectors (wq#610, design §3.3)`
section.

**Honest limitation, stated rather than hidden:** memoryd went live 2026-08-15 (yesterday). Its
entire kernel-attributed write history at the time of this ticket is 7 rows (6 `lane=memory` drill
writes from `mv4-w575-05322f`, 1 `lane=operator`) and 146 `memory_audit` rows spanning under 17
hours. D-1's "8-week trailing median" and D-3's "first ever" are running against essentially one
drill session's worth of data — built correct-for-scale, not padded to look more meaningful than
the data supports (same honesty §3.3 itself uses: "gets sharper as the lane dimension populates").

**Verified live**, full `python3 lumen_dream.py` run (read-mostly by the module's own standing
law — nothing here mutates `archival_notes`/`memory_audit`/`memory_quarantine`; confirmed
`embed_retry` touched 0 rows this run, `PRAGMA integrity_check` clean after):
```
- D-2 trust escalation: not run here — live+synchronous in kernel.py validate_note()
- D-1 origin-mix shift: 0 lane(s) flagged
- D-3 novel writers (7d): 2 new (lane, agent_id) pair(s), 2 known total
- D-4 near-duplicate flood: 1 source(s) flagged (>= 3 writes/1d at cosine >= 0.95)
- D-5 unresolvable attribution (I-7 rejects, 7d): 0 of 76 total rejects
- D-1/D-4 proposal file: dream/detector_proposals_2026-08-16.json
```
D-4's one real finding (not a bug demo — genuine signal): 4 near-duplicate `legacy`-lane notes
titled "factory APPLIED plan ... for proposal" within the same minute, cosine .96-.966 — looks like
`minerva_factory.py` writing repetitive per-proposal notes, worth a look but out of this ticket's
scope (surfaced, not chased). `lumen-dream.timer` is a oneshot (next fire 18:00 UTC today,
verified via `systemctl list-timers`) so this wiring takes effect automatically, no restart needed.

## 4. `/propose` review surface — built + tested, deployment blocked by a permission boundary

**Premise correction on the ticket's own framing:** "move to the `proposals` table" turns out to
be the wrong target, verified rather than assumed. `proposals` (1,057 rows, `minerva_proposal.py`)
has a **code-change lifecycle** — `status: pending→accepted→rejected→deployed→reverted`,
`minerva-eval.service` doing "safety checks before deploy" against `proposed_change`. A
memory-write proposal (kind/title/content/tags/source_ref) has no meaningful "deploy" step and
forcing it into that schema would be a worse fit, not a better one — verified by reading
`minerva-proposal.service`'s actual field usage before deciding, not guessed.

The real gap, confirmed by reading `daemon.py`: `GET /quarantine` **lists**, but nothing existed to
**act** — no release, no discard, at the daemon layer or the kernel layer. Design §3.2 says
"Release is an operator action that re-runs the full kernel"; that action didn't exist. This — not
the table choice — is what made the existing surface non-functional as a review surface.

**Built:** `kernel.release()` / `kernel.discard()`, `POST /quarantine/release` /
`POST /quarantine/discard` (operator-only, same gate as `/revoke`). Completes exactly the three
verbs `proposals` would have given an operator (list / approve+apply / reject), scoped to where
the payload already lives, with no schema change to a table that doesn't fit.

**Design correction found by testing, not by inspection:** my first `release()` draft re-validated
every quarantine row against the *original submitter's* identity. That's correct for D-2 hits (the
lane's normal content, minus the forged trust claim, should be judged as if honestly submitted) but
**structurally cannot work** for `propose`-type rows — a `lane='dream'` re-validation always
re-returns `propose` (that lane's contract policy is permanently `propose` for `archival_notes`),
so release would loop forever. Fixed: `table_name='archival_notes:proposal'` rows re-validate as
the **operator** (approving a proposal is the operator taking authorship, matching
`compose_origin`'s `origin='operator'`); `table_name='archival_notes'` (D-2) rows re-validate as
the **original lane** minus the forged field. Caught by the test suite immediately (first run
asserted, got `not_released`/`propose` back) — not discovered after deploy.

**Verified**, three layers:
1. Kernel-level, scratch DB: propose→release (operator identity, `origin`/`trust` land `operator`),
   double-release blocked (`not_held`), propose→discard (no write happens), double-discard blocked,
   release of a nonexistent id (`not_found`), and the D-2 path specifically (release re-validates
   as the *original* `fleet` lane, provenance lands `lane='fleet', trust='derived'`, **not**
   `operator`) — all asserted, all passed.
2. HTTP-level, on an isolated scratch instance (`MEMORYD_PORT=8143 MEMORYD_DB=<scratch copy>`,
   live service on 8139 never touched): unauthenticated release → 403 fail-closed; operator release
   → 200, write lands; double-release → 409; discard → 200, no write; matches the kernel-level
   results exactly.
3. `PRAGMA integrity_check` clean on the live db throughout (the live db was never targeted by any
   of this section's tests — everything ran against `/tmp/mv4e604-954616/scratch_wq610_kernel.db`,
   deleted after).

**Not deployed to the live service, stated plainly:** `lumen-memoryd.service` is a persistent
`ThreadingHTTPServer` process — my new endpoints exist on disk but the running process is still
serving the pre-wq610 `daemon.py`/`kernel.py` from memory until it restarts. Restarting a systemd
unit needs `sudo systemctl restart`, which `/home/lumen/foreman/harness/guard.sh` hard-denies for
this session (`BLOCKED — denied verb (sudo/reboot/shutdown/poweroff/halt/init 0|6)`) — the same
class of gap `wq#626`'s report already named for `lumen-mission-control.service`. I did not attempt
a workaround. **Follow-up for an elevated session (orch/Frank):** `systemctl restart
lumen-memoryd.service`, then `curl 127.0.0.1:8139/healthz` to confirm it comes back, then a live
`GET /quarantine` + one real release/discard round-trip against production data as closeout proof —
mirroring exactly how `wq#626`'s MC deploy step was left for orch.

## State

- Code changed (all on disk, live except memoryd's HTTP layer per §4):
  `minerva_memory.py` (write path), `lumen_recall2.py` (revocation predicate),
  `lumen_dream.py` + new `lumen_memoryd_detectors.py` (detectors), `v4/memoryd/kernel.py` +
  `v4/memoryd/daemon.py` (release/discard — built, awaiting restart).
- Golden bench, final production state: `results_wq610_final_20260816_0303.json` — R@1 .774 /
  MRR .834 / trap@1 .188 / abstention .500, unchanged from every measurement taken during this
  ticket. `golden_sha256 8ba5afb0...`.
- No service stopped, no reboot, no `sudo`. `minerva-db` slock released at ticket close.
