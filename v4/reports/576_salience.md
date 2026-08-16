# wq#576 [mv4-P4] — Usage-aware salience + forgetting proposals (SHIPPED 2026-08-15)

Agent `mv4-w576-2be351` · lane `memory` · deps #571/#572 satisfied · slock `minerva-db`.
Design of record: `v4/research/salience_forgetting.md` (#571) §7 build list, verbatim intent.
Scoring semantics: `v4/reports/595_scoring.md` (wq#595). Artifacts:
`576_logcost.json`, `576_salience_arms.json`, `eval/test_wq576_salience.py`.

## 0. Headline

Hit logging is **live, caller-tagged and fail-open**; rollup + 90-day prune run in dream;
the outcome joiner is wired at episode commit; floors are enforced in code with a unit
check; the forgetting proposer is **built and dark**, proved RED→GREEN on fixtures.

**The salience boost was measured and LOST. `MEMORY_V4_SALIENCE` stays 0.** All three
weight arms fell below control on aggregate MRR and dropped two classes by ≥2pts. That is
recorded as the outcome, not worked around (#573 precedent).

Three things the research doc got wrong are corrected in §5 — including a latency estimate
that was an order of magnitude optimistic and a confidence-bound direction that is
backwards in its own worked example.

## 1. Migration 0009 — applied, with trigger negative controls

`migrations/0009_wq576_salience.sql`, applied to prod **2026-08-15T05:34:58Z**.

- **Quiet window used: 05:34–06:30Z** — after `minerva-eval` (03:00Z) and `minerva-factory`
  (03:35Z) had both run, before `minerva-classifier` (06:30Z). No minerva.db writer timer
  fires in that gap; the `minerva-db` slock excluded other lanes.
- Backups: `sqlite3 .backup` → `/home/lumen/backups/wq576/minerva.db.pre0009.20260815_053458`
  plus the runner's own `minerva.db.bak.20260815_053458.pre_migrate`.
- **Dry-run on a COPY first** (`/tmp/mv4-w576-2be351/dryrun_0009.sh`), with the 0008 pattern's
  trigger negative controls taken **before and after** the migration on that copy:

| control | pre-0009 | post-0009 |
|---|---|---|
| `UPDATE archival_notes SET origin=…` | ABORT `provenance is immutable (wq#575 §2.2)` | ABORT, identical |
| `UPDATE memory_audit …` | — | ABORT `memory_audit is append-only (wq#575 §3.1)` |
| `DELETE FROM memory_audit` | — | ABORT, same invariant |

  All three 0008 triggers survive 0009 and still fire. Post-migration on the copy:
  `integrity_check ok`, notes 1223 → 1223, facts 914 → 914. Same on prod after apply.
- Objects created (all **additive**; no column and no trigger touches `archival_notes`):
  `note_hits`, `note_salience`, `note_hit_outcomes`, `note_archives`, `salience_meta`
  (`retriever_epoch=2`, `hit_retention_days=90`), 4 indexes.

## 2. The tap — where it sits, and the exclusion proof

`lumen_recall2.search_v3` is now a thin wrapper over `_search_v3_impl`. The tap sits at the
**wrapper's** return, which is deliberate: `search_v3` has two exits, and the one a degraded
box actually takes is the fail-open-to-v2 path at `:412`. Tapping inside the implementation
would have logged only the healthy path.

Caller tags are derived by walking the stack (`lumen_salience.caller_tag()`), with
`MEMORY_V4_CALLER` as an explicit override. Stack inference over an opt-in argument, for two
reasons: it tags memoryd traffic without editing the daemon, and a new caller cannot forget
to pass it.

**Exclusion is opt-OUT by tag and filtered AT ROLLUP, never at the tap** (#571 §2), so the
exclusion is auditable rather than invisible. Proven with real callers, not stand-ins —
a real `recall_bench.py --qid 1` and a real `memory_embed_guard.probe_v3()`:

```
notes hit ONLY by bench/guard : 13   → of those in note_salience: 0    (must be 0)
notes hit ONLY by real callers: 16   → of those in note_salience: 16   (must equal 16)
```

memoryd verification (drill D11 concern — the daemon proxies `search_v3` byte-for-byte):
one `/recall` through `127.0.0.1:8139` produced 5 `note_hits` rows tagged **`memoryd`**, not
`direct`. Tag correct on the first try; **provenance was not** — see §5.3.

Measured volume so far (this session, one day of a heavily-benched box):

| caller | rows | note |
|---|---|---|
| bench | 11,110 | my 4 golden arms + 2 logcost runs; **excluded at rollup** |
| precheck | 24 | the real fleet-wide driver |
| direct | 10 | module CLI |
| memoryd | 8 | daemon-served |
| guard | 5 | health probe, **excluded** |

Storage: `dbstat` reports `note_hits` at **83.3 bytes/row including indexes** — close to
#571's ~90-byte estimate. The nightly `recall_bench` alone will contribute ~2,390 rows/night
(239 queries × top_k 10), all excluded. #571's claim that exclusion is "the majority of the
would-be volume" is confirmed and then some: on measured traffic it is **99.6%** of it.

## 3. Logging cost — MEASURED (#571 §8 asked for this explicitly)

`eval/bench_wq576_logcost.py` → `v4/reports/576_logcost.json`. 40 queries from the frozen
golden, arms back-to-back in one process, db mtime bracketed, per-arm effective config
embedded in the artifact.

| arm | e2e p50 | e2e p95 | write p50 | write p95 | write max |
|---|---|---|---|---|---|
| A logging OFF | 171.7 ms | 188.9 | — | — | — |
| B logging ON | 177.2 ms | 219.3 | **7.2 ms** | 10.8 | 13.0 |
| C logging ON, contended | 694.9 ms | 1439.5 | 9.0 ms | 63.6 | 188.0 |
| D logging OFF, contended | 746.4 ms | 2247.7 | — | — | — |

**Verdict: PASS.** Uncontended p50 delta **+5.6 ms** (this run; +10.5 ms on the first run),
against a 400 ms budget — under 3%.

Two honest corrections:

1. **#571 §8 guessed "an executemany of 8 rows on a WAL db should be sub-millisecond".
   It is ~7–9 ms, an order of magnitude more.** I hypothesised commit fsync and tried
   `PRAGMA synchronous=NORMAL` on the logging connection: it bought ~1.3 ms (8.55 → 7.22 ms),
   so the cost is **not** fsync-dominated. The PRAGMA is kept anyway — a droppable 90-day
   forensic log should not buy full durability at recall time — but it is not the explanation
   and is not presented as a fix.
2. **Arm D exists because without it, arm C prices the contender rather than the tap.**
   With it, the contended comparison is revealed as noise: C−D was **+258 ms** on the first
   run and **−51 ms** on the second (logging arm *faster*). What the contended arms actually
   show is that a 200-write/s competitor on minerva.db puts recall at 627–746 ms **with
   logging off**, i.e. contention breaks the p50 budget on its own. Do not read C as a cost
   of this ticket.

## 4. Rollup, prune, outcome joiner

- `lumen_dream.run()` now calls `lumen_salience.rollup()` then `prune()`. Live run:
  `rollup: 33 note(s) scored, prune: 0 raw hit row(s) dropped past retention`.
- Rollup excludes, in SQL: synthetic callers, `retriever_epoch != 2`, and
  **`archival_notes.revoked_at IS NOT NULL`** — the five revoked wq575 drill fixtures (#610)
  can never accrue worth. Fixture-verified both ways.
- `retriever_epoch` is stamped on every row at write time. **Epoch is 2 as of now** (#574
  changed retrieval this week); counters earned under a different retriever measure a
  different function, so the rollup filters on it rather than blending epochs.
- Worth: `worth = (succ+1)/(succ+fail+2)` (Laplace — an unseen note sits at 0.5, not 0) with
  `worth_lcb` = Beta 5th percentile, computed by a stdlib regularized incomplete beta +
  bisection (no scipy on a box whose python env is shared with the lanes). Verified against
  closed-form values: `I₀.₅(1,1)=0.5`, `I₀.₃(2,5)=0.579825`, `ppf(1,1,0.05)=0.05`.
  Used as a **proposal prior only** — the convergence guarantee assumes stationary retrieval
  and ours provably is not (#571 §5).
- Outcome joiner at `lumen_episode.commit()`: back-fills `episode_id` onto this session's
  `note_hits` rows and writes `note_hit_outcomes`. `--failed` non-empty (noise-filtered) →
  `failure` immediately; otherwise `pending`, resolved at rollup from the wq ticket status
  (`blocked` → failure, `done` → success). Fail-open, exactly like the fq wire-in above it.

**First live label, and a caveat it exposes.** This ticket's own episode commit produced the
first row: `6f1f514e2178 | mv4-w576-2be351 | failure`, and 36 `note_hits` rows were
back-filled with that episode id. The label is *correct by the rule* — `--failed` was
non-empty, because the commit honestly records the dream row-factory bug from §9 — but the
session shipped. So a session that self-reports an honest mid-session failure and then fixes
it scores the same as one that failed outright. That is exactly the co-occurrence-not-
causation weakness #571 §3 warns about, now observed rather than predicted: it argues for
keeping `worth` a **proposal prior only**, and against ever letting a low score act on its
own. A finer label (e.g. failure only when the ticket ends `blocked`) is a candidate
refinement for #578, deliberately not invented here.

## 5. Corrections to the research doc (#571) — stated, not silently applied

### 5.1 The forgetting confidence bound is the wrong way round
#571 §3: *"all forgetting proposals rank on the LOWER CONFIDENCE BOUND, never the mean —
that is what stops a note with 1 failure and 0 successes (mean 0.33) from outranking one
with 40 failures and 5 successes (mean 0.12) for deletion."*

Measured with the shipped estimator:

| note | mean | LCB (5th pct) | UCB (95th pct) |
|---|---|---|---|
| 0 succ / 1 fail | 0.333 | **0.0253** | 0.776 |
| 5 succ / 40 fail | 0.128 | **0.0584** | 0.238 |

Lower LCB = more deletable, so the LCB ranks the **unproven** note as more deletable —
the opposite of the doc's stated intent. "Confidence must earn the demotion" is expressed by
the **upper** bound: even optimistically, is this note bad? Only 5/40 clears that.

I did **not** silently flip the gate. `worth_lcb` ships as specified in §2's schema; the
proposer carries `worth_ucb` alongside every candidate for the operator, and the code says
why. Recommended follow-up ticket: a second candidate class, *retrieved-and-consistently-
unhelpful*, ranked on `worth_ucb` — which is where the two-counter signal would actually do
work.

### 5.2 §4b's worth clause is vacuous as written
The rule is `age>180d AND hits==0 AND worth_lcb<0.35 AND …`. With `hits==0` there is no
outcome data, so worth sits at the prior and `lcb=0.05 < 0.35` **always** passes. The
conjunction is driven entirely by age + disuse; the worth term never bites, because the only
notes it could discriminate are ones with hits, which the `hits==0` clause has already
excluded. Built as specified (the brief defers to §7), flagged here rather than redesigned.

### 5.3 memoryd traffic had no session provenance
The tag was right on the first try (`memoryd`, not `direct`), but `session_id` came back
**empty**: the daemon is a long-running service with no `WQ_AGENT`, serving many lanes from
one process. Left alone, every daemon-served hit — which after wq#575 is *all* agent-lane
traffic — would be unjoinable to an episode, and §3's outcome join keys on exactly that
field. Closed with a `contextvars.ContextVar` (per-thread, safe under `ThreadingHTTPServer`;
an env var would race across concurrent requests) set in `daemon._recall` from the
server-derived `ident.agent_id`. Verified: `memoryd | mv4-w576-2be351`. Identity is still
derived server-side — a lane still cannot spell its own name.

### 5.4 The skills two-counter is NOT liftable (#571 §0.4 / §8 asked)
Audited before trusting it, per the brief:
- It is a **rolling mean with one counter**, not two counters. `success_rate` is updated as
  `((sr*tu)+outcome)/(tu+1)`; there is no confidence separation, so 1/1 and 40/40 both read
  1.0 and a 0/1 skill reads 0.0 — the exact prior-less punishment #571 §3 warns against.
- `add_skill` uses `INSERT OR REPLACE` (`minerva_skills.py:82`) which **resets
  `times_used`/`success_rate` to 0 and `last_used_at` to NULL** — re-adding a skill silently
  destroys its history. Same class as the `INSERT OR REPLACE` provenance loss at
  `minerva_memory.py:146,180`.
- The update is read-modify-write across two separate connections
  (`minerva_skills.py:148` then `:155`) with no transaction — lost update under concurrency.
- Population: 5 skills, 3 with `times_used > 0`.

**Verdict: rebuild, do not lift.** `note_salience` stores raw `succ`/`fail` integers so the
posterior is recoverable and confidence is expressible.

## 6. Salience boost — golden v4 arms. A LOSS, honestly recorded.

`eval/arms_wq576_salience.py` → `v4/reports/576_salience_arms.json`. House pattern: frozen
golden `golden_v4_frozen_20260814.jsonl` sha256 `8ba5afb0ce12585606b8dcff5ffb8245af24b0f0f32ccbddf3d4121f1bbf46b7`
pinned in every arm, effective runtime config embedded per arm by `recall_bench` itself,
mandatory positive controls, four arms back-to-back in one window, db mtime bracketed.
Arms run with `MEMORY_V4_CALLER=bench` so they cannot score themselves into the counters.

| arm | R@1 | MRR | trap@1 | abstention | verdict |
|---|---|---|---|---|---|
| **ctrl** `SALIENCE=0` | **.778** | **.836** | .188 | .500 | control (GREEN) |
| `W_USE=0.02` | .770 | .832 | .188 | .500 | **REJECT** |
| `W_USE=0.05` | .770 | .831 | .188 | .500 | **REJECT** |
| `W_USE=0.02 W_WORTH=0.02` | .770 | .832 | .188 | .500 | **REJECT** |

Gate: aggregate MRR up AND no class down ≥2pts AND trap@1 not up AND abstention not down.
Every arm failed on **all three** of: MRR below control, `gotcha` R@1 .818→.795,
`procedure` R@1 .649→.622. trap@1 and abstention were unmoved in every arm.

**`MEMORY_V4_SALIENCE` default stays 0.** The flag, `W_USE` and `W_WORTH` all ship at their
no-op values; with the flag off `boost_map()` does not even open a query, so the fused
expression is byte-identical to pre-576 V3 (unit-checked, not assumed).

Caveats that must travel with these numbers:
- **Counter coverage at run time was 2.04%** (25 of 1,223 notes) and the oldest hit was two
  hours old. This is a fair test of *today's data*, not of the idea. A re-run is worth doing
  once `note_hits` holds a few weeks of real precheck traffic.
- All three arms produced **identical R@1 and near-identical MRR** despite a 2.5× spread in
  `W_USE`. That is the signature of the boost moving the same one or two queries regardless
  of weight — consistent with 25 notes carrying counters.
- Control R@1 .778 / MRR .836 does **not** match the baseline of record (.728/.793, wq#595).
  The corpus grew (1,223 notes) and minerva.db moved during the window. **Arms are compared
  to my own control arm inside the same window**, never to the stored baseline —
  cross-corpus comparison is exactly what wq#595's comparability gate exists to refuse.
- `sched_baseline.json` was **not** reseeded by me (fq#592 — orchestrator's call).

## 7. Forgetting proposer — BUILT DARK

Per #571 §4b. `MEMORY_V4_FORGET_PROPOSE=0`; the dream run prints
`forgetting proposer: DARK` and does not compute candidates. Manual inspection:
`python3 lumen_salience.py propose`.

Proved RED→GREEN on a scratch DB (`eval/test_wq576_salience.py`, **19/19 pass**):

- **RED baseline:** on the real corpus the proposer returns **0 candidates**. Nothing is old
  enough to forget — which is the whole reason it stays dark.
- **GREEN:** with fixtures planted on a `.backup` copy, it fires on exactly the two that
  should qualify (400d derived, 300d external, both unretrieved).
- Floors, per fixture: 400d **operator** → spared; 400d **state-mirror** → spared; 5d derived
  → spared; 400d derived **but retrieved once** → spared. Revoked note → never proposed and
  absent from `note_salience`.
- Archive-not-erase: dry-run apply writes nothing; confirmed apply records `note_archives`
  rows and **all 7 fixture notes remain in `archival_notes`**.

Demotion is inert on the read path by default: `MEMORY_V4_ARCHIVE_FILTER=0`. Wiring the
filter on is a later ticket's adopt decision, on a measured win.

## 8. Floors — in code, not convention (#571 §4c)

`lumen_salience.floor_reason()` is the single gate. A floor-protected note may still be
**boosted**; it may never be **demoted**, and no counter can tune that downward.

| floor | rationale |
|---|---|
| `trust='operator'` | 1,134 of 1,223 notes live today; this store is operator-authored ground truth |
| `origin='state-mirror'` | 71 notes mirroring `render_tier='core'` facts — the note-layer proxy for core tier |
| `age <= 30 days` | 762 notes; a fresh note has not had a fair chance to be retrieved |

Verified live this session against the current corpus (1,223 notes), not carried over from
#571's counts. Coverage of the floors is **~93%** of the corpus by construction — correct for
a store that is overwhelmingly operator ground truth rather than scraped derivative. And
`age > 180d` matches **0** notes today, which is the arithmetic reason §7 stays dark.

This is the anti-"I told you yesterday" line. Unit-checked per fixture in §7; the check fails
the suite if any floor stops holding.

## 9. What I broke and fixed in this session (the honest bit)

Running the real `lumen_dream` job — rather than reading it — caught a live bug the unit
tests could not: `lumen_dream` opens its connection with the default **tuple** row factory,
my rollup indexes rows by name, and the fail-soft wrapper turned the `TypeError` into a
report line reading `rollup/prune FAILED: TypeError`. **A fail-soft path made a broken
integration render as a tidy report line** — the same class as wq#575's fail-open client
that made a dead integration look like byte-identical parity.

Fixed with a borrow-and-restore context manager (`_rowmode`): the caller's `row_factory` is
set to `sqlite3.Row` for the duration and handed back exactly as found, because dream keeps
using that connection afterwards. Regression-tested against a bare tuple-factory connection:
rollup 33, prune 0, candidates 0, `row_factory` restored to `None`, tuple access still works
for the caller. Second dream run GREEN.

## 10. State of play / for the next ticket

- Live: hit logging (all callers), rollup + 90d prune in dream, outcome joiner, floors.
- Off by default: `MEMORY_V4_SALIENCE=0` (measured loss), `MEMORY_V4_FORGET_PROPOSE=0`
  (nothing to forget), `MEMORY_V4_ARCHIVE_FILTER=0` (demotion inert).
- `half_life_days` left **dormant** at recall, as instructed — no decay was wired into the
  hot path as a side effect of this ticket.
- **#578 can proceed**: the two-counter machinery (`succ`/`fail` + Beta posterior + LCB/UCB)
  is in `lumen_salience.worth_stats()` and should be reused, not re-implemented.
- Re-run the §6 arms once `note_hits` carries a few weeks of real precheck traffic; today's
  2% coverage is the reason to distrust a null result, not a reason to adopt anyway.
- Open follow-ups worth tickets: (a) `worth_ucb`-ranked "retrieved-and-unhelpful" candidate
  class (§5.1); (b) `minerva_skills` counter rebuild (§5.4); (c) recall-side consumption of
  `note_archives` behind a measured adopt.

## 11. Files

| path | change |
|---|---|
| `minerva/migrations/0009_wq576_salience.sql` | NEW, applied |
| `minerva/lumen_salience.py` | NEW (656 ln) |
| `minerva/lumen_recall2.py` | tap wrapper + boost hook · `.bak.20260815_pre_wq576` |
| `minerva/lumen_episode.py` | outcome joiner at commit · `.bak.20260815_pre_wq576` |
| `minerva/lumen_dream.py` | rollup/prune/report · `.bak.20260815_pre_wq576` |
| `minerva/v4/memoryd/daemon.py` | session provenance · `.bak.20260815_pre_wq576` |
| `minerva/eval/test_wq576_salience.py` | NEW, 19 checks |
| `minerva/eval/bench_wq576_logcost.py` | NEW, 4 arms |
| `minerva/eval/arms_wq576_salience.py` | NEW, golden v4 arms + gate |
| `v4/reports/576_logcost.json`, `576_salience_arms.json` | NEW artifacts |

No service left stopped. `lumen-memoryd` restarted twice (tap pickup, then §5.3) — active +
enabled, `/healthz` ok with an unchanged contract sha, `/recall` returning results. No
reboots, no Anthropic API spend, no warm models left resident.
