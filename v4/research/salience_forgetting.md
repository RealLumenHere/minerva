# wq#571 [mv4-P0c] — Usage-weighted salience + principled forgetting (research + AUDIT)
_2026-08-14 PT · agent mv4-w571-b9deec · READ-ONLY on production stores · feeds #576 (EARNED) and #578 (GOVERNED)_
_Primary input: minerva/v4/research/SOTA_2026-08.md RQ4 + RQ7. Every claim below re-verified live this session (fq F1) — the ticket's own stated audit was NOT taken on trust, and one half of it turned out to be materially incomplete._

## 0. HEADLINE — what the audit changed
The ticket says: *"state_facts half_life wired, NO note-layer usage tracking."*
Verified verdict: **half-right, and the wired half is weaker than the phrasing implies.**
1. `half_life_days` exists and is **written**, but is **DORMANT AT RECALL** — nothing in the retrieval path reads it. Its only consumer is a single line in `lumen_state.py` that decorates `current` output. Recall ranking has never seen a decayed confidence.
2. Population is near-empty: **22 of 872 facts carry a half-life, and every one of them is `predicate=price`.** Zero fleet/infra facts decay. The mechanism is real but effectively unexercised.
3. Note-layer usage tracking: **CONFIRMED ABSENT** (positive-control-verified sweep).
4. Undocumented precedent found: `skills.last_used_at` + `times_used` + `success_rate` **already exist and are written** by `minerva_skills.py`. That is a working two-counter-shaped usage record on a sibling table — a design template already in-house, and a live testbed for the Memory-Worth math before it touches notes.

## 1. AUDIT EVIDENCE (queries + outputs, inline)

### 1a. Positive control FIRST (audit discipline invariant)
No clean sweep is trusted until it goes red on a planted known-positive. Planted `/home/lumen/minerva/_wq571_control_probe.py` containing a fake `half_life_days` consumer calling `effective_confidence`, re-ran both sweeps, then removed it.
```
$ grep -rln "half_life" --include="*.py" --include="*.sh" /home/lumen/minerva /home/lumen/bin /home/lumen/agents /home/lumen/agent
/home/lumen/minerva/lumen_state_gen.py
/home/lumen/minerva/lumen_state.py
/home/lumen/minerva/_wq571_control_probe.py      <-- DECOY FOUND (control RED, as required)
$ grep -rn "effective_confidence" --include="*.py" ...
lumen_state.py:102 (definition) · lumen_state.py:257 · _wq571_control_probe.py:3   <-- DECOY FOUND
$ rm _wq571_control_probe.py  ->  DECOY REMOVED (re-stat: No such file)
```
**Corpus swept: 130 first-party .py files** (5,123 raw, minus `site-packages`/`.venv` — the naive count is 97% vendored library noise and would have produced a false-positive `last_used` sweep; see 1c).

### 1b. half_life: wired, populated thinly, DORMANT at recall
```
$ sqlite3 minerva.db ".schema state_facts"
... half_life_days REAL, last_confirmed_at TEXT, fact_class TEXT, render_tier TEXT);
$ grep -n "half_life" lumen_recall2.py
(no output)                        <-- RECALL DOES NOT CONSUME IT
$ grep -rn "effective_confidence" (first-party)
lumen_state.py:102  def effective_confidence(stored, half_life_days, valid_from, now, last_confirmed_at)
lumen_state.py:257  d['eff_confidence'] = effective_confidence(...)     <-- the ONLY consumer
$ sqlite3 minerva.db "select count(*), sum(half_life_days is not null), sum(valid_to is null) from state_facts;"
872|22|660
$ sqlite3 minerva.db "select predicate,count(*),avg(half_life_days) from state_facts where half_life_days is not null group by predicate;"
price|22|30.0                      <-- 100% of decaying facts are 'price'
```
Decay function (lumen_state.py:102-111) is **exponential base-0.5 on age since `last_confirmed_at` or `valid_from`**: `stored * 0.5**(age/half_life)`. Defaults come from a hardcoded `VOLATILE_HALFLIFE` map — `price/deal_quality/market_price/cost:30d · listing/stock:7d · auction_state/auction_end:3d`. **Every entry is Surface-Pro-resale-shaped vocabulary; no fleet predicate appears.** That is why 22/872: the map never fires on infra facts.
**Implication for #576:** there is no "existing decay to integrate with" at recall. The integration surface is a decorated field in one CLI path. Treat half_life as a *sibling signal to reconcile*, not a foundation to build on.

### 1c. Note-layer usage tracking: CONFIRMED ABSENT
```
$ for each table: pragma table_info -> match /hit_count|last_used|use_count|access_count|retrieval/
skills: last_used_at            <-- ONLY hit, and it is NOT the note layer
$ sqlite3 minerva.db ".schema archival_notes"
id, kind, title, content, embedding, tags, source_ref, created_at, origin, trust
                                <-- no usage column of any kind
$ grep -rn "hit_count|last_used|access_count|use_count" (first-party, 130 files)
init_db.py:39            created_at TEXT, last_used_at TEXT          (skills)
minerva_skills.py:82     INSERT ... times_used, success_rate, last_used_at
minerva_skills.py:155    UPDATE skills SET times_used=?, success_rate=?, last_used_at=?
```
**Zero readers, zero writers, zero columns on `archival_notes`.** Ticket claim upheld. Note the near-miss: the unfiltered sweep returned `anyio`/`openai`/`pygments` hits — an auditor who did not exclude vendored code would have reported usage tracking as *present*.

### 1d. Where hits are visible today (the tap points)
Single chokepoint, which is the good news:
```
lumen_episode.recall(query, top_k)            # lumen_episode.py:61 — flag-gated
  └─ MEMORY_V3_RECALL != 0 -> lumen_recall2.search_v3(query, top_k)   # lumen_recall2.py:85, returns scored[:top_k] @197
  └─ except -> breadcrumb + fail-open to minerva_memory.search(tiers=['recall','archival'])
```
First-party callers of the recall path (excluding eval harnesses):
- `lumen_episode.py:111` — `precheck` (`recall(plan, top_k=8)`) — **the highest-volume caller; every agent runs it before every change**
- `lumen_episode.py:184` — `recall` CLI subcommand
- `minerva_proposal.py:119` — proposal dedup/context (`top_k` context pull)
- `memory_embed_guard.py:108` — health probe (**must be excluded from logging** — synthetic query, would inflate counters on a fixed probe note every guard cycle)
- `eval/recall_bench.py:75,77` — **must be excluded** (257 queries × every scheduled night = would dominate the counter distribution within a week and poison salience with golden-set shape)
**Design consequence: log at `search_v3` return with a caller tag, and make exclusion opt-OUT by tag, not opt-in.** The two loudest callers on this box are both synthetic.

### 1e. Corpus + shape (sizing the design)
```
notes=1,161 · content bytes=715,758 (~0.6KB/note avg)
origin:  claude-session 1078 · state-mirror 71 · backfill 6 · convo-learn 6
trust:   operator 1078 · derived 77 · external 6
age:     0-7d 389 · 7-30d 326 · 30-90d 443 · 90d+ 3
facts:   render_tier core 259 · topic 401 (660 open)
proposals: defer 495 · reject 442 · approved 33 · deployed 27 · planned 41 · staged 16 · shipped 1 · blocked 2
note_merges: 5 rows (absorbed_id, keep_id, sim, method, ts) — existing archive-not-erase precedent
```
Note growth ≈ **45/day** measured over 08-05..08-14 (2,111,45,37,58,16,15,58,49). **90d+ = 3 notes**: this corpus is young. Nothing is old enough to forget yet — which sets the honest expectation for #576 below.

## 2. HIT-LOGGING SCHEMA (design only; build is #576)
Additive table, never a column on `archival_notes` (keeps the hot embedding table untouched and makes the log independently truncatable/backupable):
```sql
CREATE TABLE IF NOT EXISTS note_hits (
  note_id    TEXT NOT NULL,             -- archival_notes.id (no FK: log must survive merges)
  ts         TEXT NOT NULL,             -- UTC ISO
  rank       INTEGER NOT NULL,          -- position in the returned list (1..top_k)
  caller     TEXT NOT NULL,             -- 'precheck'|'recall-cli'|'proposal'|'agent-lane:<id>'|'bench'|'guard'
  query_hash TEXT,                      -- sha1(query)[:12] — dedup/repeat detection, NOT the query text
  session_id TEXT,                      -- WQ_AGENT when present: per-lane provenance for P3
  episode_id TEXT                       -- NULL at write; back-filled by the outcome joiner (§3)
);
CREATE INDEX idx_note_hits_note ON note_hits(note_id, ts);
CREATE INDEX idx_note_hits_ep   ON note_hits(episode_id);
-- rollup, recomputed by dream; never hand-edited
CREATE TABLE IF NOT EXISTS note_salience (
  note_id TEXT PRIMARY KEY, hits INTEGER DEFAULT 0, last_hit_at TEXT,
  succ INTEGER DEFAULT 0, fail INTEGER DEFAULT 0,   -- Memory Worth two counters
  worth REAL, worth_lcb REAL, computed_at TEXT
);
```
**Write path:** one `executemany` of ≤`top_k` rows at `search_v3` return, inside a `try/except: pass` — *recall must never die because logging did* (the existing `_breadcrumb_v3_failure` docstring already sets this precedent in-tree). WAL is already on. `query_hash` not `query`: hashing keeps operator prose and credentials out of a table that will be read by lane agents under P3.

**Write-amplification estimate (measured inputs, stated assumptions):**
- Rows per call = top_k (6 for precheck, 10 for CLI/bench).
- Precheck volume is the driver. Proxy: 45 notes/day committed, and prechecks outnumber commits (many prechecks precede one commit; ~3-5× is the observed working pattern across lanes) → **~150-250 recall calls/day**, call it 250 worst case.
- 250 calls × 8 rows ≈ **2,000 rows/day ≈ 60k/month**. At ~90 bytes/row → **~5.4 MB/month**, ~180 KB/day.
- Against minerva.db's current scale (1,161 notes / 716 KB of content), a year of unpruned logging (~65 MB, 730k rows) is the single largest table in the file. **Therefore: 90-day retention on `note_hits`, with counters rolled into `note_salience` before pruning** (the rollup is the durable artifact; the raw log is a 90-day forensic window). Nightly prune inside the existing dream run.
- Excluded callers (`bench`, `guard`) remove ~257 rows/night + ~1,440 probe rows/day — i.e. **exclusion is not hygiene, it is the majority of the would-be volume.** Log the tag anyway and filter at rollup, so exclusion is auditable rather than invisible.

## 3. MEMORY WORTH — two-counter design (2604.12007), localized
Paper: two counters per memory (success/failure co-occurrence) converge a.s. to `p+(m) = Pr[success | m retrieved]` under stationary retrieval; Beta-Bernoulli posterior mean; dual counts encode confidence.
**Our estimator:** `worth = (succ + α) / (succ + fail + α + β)` with `α=β=1` (Laplace/uniform prior → an unseen note sits at 0.5, not 0 — a never-retrieved note must not be punished for the retriever's blind spots). Report **`worth_lcb`** = Beta posterior 5th percentile alongside the mean; **all forgetting proposals rank on the LOWER CONFIDENCE BOUND, never the mean** — that is what stops a note with 1 failure and 0 successes (mean 0.33) from outranking one with 40 failures and 5 successes (mean 0.12) for deletion. Confidence must earn the demotion.

**Defining "success" on OUR data — this is the load-bearing localization, and it is where the paper's assumptions meet a fleet that does not have per-turn reward.** Available outcome signals, in descending trustworthiness:
| Signal | Source (verified present) | Comment |
|---|---|---|
| Episode `--failed` content | `lumen_episode.py commit --failed` | **Honest-failure field is culturally enforced** (this session's own commit carries one). Best label we own. |
| fq ledger rows | `/home/lumen/agents/failure-queue/failures.db` (wq#590, live) | Explicit failure records w/ `caught_by`/`prevented_by` — a *second*, independently-written failure channel. |
| wq outcome | `tasks.status` done vs blocked | Coarse but unambiguous, per ticket. |
| Proposal disposition | `proposals.status` (approved/deployed vs reject) | 442 rejects vs 33 approved — usable but heavily imbalanced. |
**Join:** `note_hits` rows within a session (`session_id`) are attributed to that session's terminal episode; `episode_id` back-filled at `lumen_episode.py commit` time. `success` := episode committed with an empty/absent `--failed` AND its wq ticket reached `done`. `failure` := `--failed` non-empty OR ticket `blocked` OR an fq row opened in that window.
**Honest caveat, stated up front:** this is **co-occurrence, not causation** — a note retrieved during a failed session did not necessarily cause the failure, and the paper's convergence guarantee says nothing about whether our label is the right one. It is a *prior for proposals*, never evidence for deletion. Treat a low `worth_lcb` as "ask the operator", full stop.

## 4. SALIENCE BOOST + FORGETTING PROPOSALS
### 4a. Additive boost (retrieval side)
Fuse as an **additive, non-filtering** channel — same discipline the SOTA report demands for the entity channel, for the same reason (a multiplicative or filtering usage term makes cold-start notes permanently unreachable, which is a self-fulfilling prophecy: never retrieved → never used → never boosted → never retrieved):
```
score_final = score_fused + w_recency*recency + w_use*g(hits, last_hit_at) + w_worth*(worth - 0.5)
g = log1p(hits) normalized to [0,1]; worth term is SIGNED and centered at the prior, so an unproven note contributes exactly 0
```
`w_use`, `w_worth` start at **0** behind `MEMORY_V4_SALIENCE` (default off) and are tuned on golden v4's 255 labels — **adopt per class, MRR up AND no class −2pts**, exactly the #574 rule. Expect the honest possibility that usage-boost *hurts* the abstention/knowledge-update strata by reinforcing whatever was popular, including stale content; qids 230/238/241/243 are already live trap@1 failures of that shape.

### 4b. Forgetting = PROPOSALS, and archive-not-erase
Nothing auto-deletes; nothing auto-edits. Output is a dream proposal file the operator applies, mirroring the existing merge-plan flow (`write_merge_plan` → `apply_merge_plan(confirm=False)` at lumen_dream.py:180/192).
Candidate rule (deliberately conservative given §1e's young corpus): `age > 180d AND hits == 0 AND worth_lcb < 0.35 AND trust != 'operator' AND render_tier != 'core'`.
Action is **demote to an `archived` tier / `archival_notes.kind` marker — never DELETE.** `note_merges` (5 rows, `absorbed_id`/`keep_id`/`sim`/`method`/`ts`) is the in-house precedent for exactly this: supersede-not-erase, with the absorbed id still resolvable. #573's federated tier gives demoted prose a place to remain retrievable.
**Reality check from the audit: `age>180d` matches ZERO notes today (90d+ = 3).** Forgetting is not a live problem on this corpus and building an aggressive pruner now would be solving a problem we do not have, against labels we have not yet validated. **Recommendation: ship logging + rollup in #576, ship the boost behind a flag, and let the forgetting proposer stay dark until there is ≥6 months of hit history to justify a threshold.** State it in the ticket so a later agent does not read the dormant proposer as an unfinished deliverable.

### 4c. SALIENCE FLOOR (the "I told you yesterday" failure)
Mem0's own eviction writeup concedes forgetting is unsolved at tooling level and prescribes a floor. Ours, non-negotiable in #576:
- **`render_tier='core'` facts (259 live) and `trust='operator'` notes (1,078 of 1,161) are floor-protected: never proposed for archival regardless of worth or hits.** That is 93% of the note corpus exempt by construction — correct, because this store is overwhelmingly operator-authored ground truth, not scraped derivative.
- Recency floor: anything ≤30d is exempt (715 notes) — a fresh note has not had a fair chance to be retrieved.
- A floor-protected item may still be *boosted*; it may never be *demoted*.

## 5. STATIONARITY + RECOMPUTE CADENCE
The convergence proof assumes stationary retrieval. Ours is provably non-stationary: the retriever changes under us (#573 federation adds a whole tier, #574 rerank reorders everything), and the workload shifts by lane. Consequences:
- **Recompute `note_salience` nightly in dream** (rollup is cheap: one GROUP BY over a ≤90-day log).
- **On any adopted retrieval change (#573/#574), stamp a `retriever_epoch` and either reset or down-weight counters accumulated under the previous epoch** — counters earned against a different retriever are measuring a different function. Suggest keeping raw `note_hits` (they carry ts, so epoch is derivable) and recomputing worth per-epoch rather than destroying history.
- Recompute cadence for `worth` on a **28-day trailing window** in addition to lifetime, and surface both — a note that was worth-1.0 in June and unretrieved since is a different object from one earning its keep now.
- **Reconciliation with half_life (§1b):** two decay mechanisms must not silently compound. Proposal: `half_life` stays the **fact-layer** confidence decay (its actual current job, on `price`-class volatile predicates); usage-salience is the **note-layer** signal. Do not apply both to one object. If facts ever need usage weighting, they get their own counters — do not overload `half_life_days`. Worth flagging that half_life's `VOLATILE_HALFLIFE` map is a resale-vocabulary artifact and arguably wants a fleet-predicate pass of its own (out of scope here; candidate ticket).

## 6. SAME MACHINERY → INSTINCT EFFECTIVENESS (#578, GOVERNED)
The two-counter object is instinct-shaped without modification: for each instinct in `/home/lumen/instincts.yaml` (16 curated, accepted 08-14), count sessions where it was **adhered to** vs **violated**, joined to the same episode-outcome labels from §3.
- `instinct_worth = Pr[success | instinct adhered]`, with the mirror `Pr[success | violated]` — **the delta is the signal**, not the level. An instinct with 0.9 success on adherence means nothing if violation also scores 0.9; it is not doing work.
- Adherence detection is the hard part and must **not** be an LLM judge (same discipline that keeps golden v4 exact-match). Mechanical proxies only: instinct→probe/guard mapping where one exists, fq `prevented_by`/`caught_by` fields (already populated by wq#590), and explicit episode `--why` references.
- Output: **confidence-update PROPOSALS** into the existing instinct-candidate flow (`lumen_dream.write_instincts`, already proposal-only). **Frank-gated accept is unchanged** — GovMem's 0/133 safe-auto-promotion result is the whole argument, and it is quoted in the SOTA report for exactly this reason.
- Sequencing: #578 depends on #576's counters existing; it should not re-implement them.

## 7. WHAT #576 SHOULD ACTUALLY BUILD (ordered, with risk)
1. `note_hits` + `note_salience` tables + logging tap at `search_v3` return, caller-tagged, fail-open, bench/guard filtered at rollup. **Risk ~zero, additive, no read-path change.** Ship this alone if time is short — it is the prerequisite for everything and it accrues data while the rest is debated.
2. Nightly rollup + 90-day prune in dream; `worth`/`worth_lcb` computed, `retriever_epoch` stamped.
3. Outcome joiner: back-fill `episode_id` at commit; label from `--failed`/fq/wq status.
4. Boost behind `MEMORY_V4_SALIENCE=0` default; tune on golden v4; **adopt per class only**.
5. Forgetting proposer: **build dark, do not enable** (§4b — the corpus has 3 notes over 90 days old).
6. Floors enforced in code, not convention: core/operator/≤30d exempt.

## 8. CAVEATS + WHAT I DID NOT VERIFY
- Write-amplification is an **estimate from a proxy** (note-commit rate), not a measured recall QPS — there is no recall counter today, which is precisely the gap this ticket closes. First week of real `note_hits` data supersedes my number; do not treat 2,000 rows/day as measured.
- "Prechecks outnumber commits 3-5×" is an observed working pattern, not an instrumented figure.
- Memory Worth's convergence guarantee is inherited from the paper under assumptions we violate (§5); the estimator is used as a proposal prior only.
- I did not measure the latency cost of the logging write (design-only ticket, READ-ONLY on prod). #576 must measure it against the p50<400ms budget before adopting — an `executemany` of 8 rows on a WAL db should be sub-millisecond, but "should be" is not a measurement.
- `skills` two-counter precedent (§0.4) was found opportunistically; I did not audit whether `times_used`/`success_rate` are *correctly* maintained, only that they are written. Worth a look in #576 — if that machinery is already sound it may be liftable rather than rebuilt.
