# Retention-Brief Scorecard — baseline 2026-08-14

_wq#597 · agent mv4-w597-81ba12 · lane memory · dispatch `v4/dispatch/597.md`_
_Instrument: `v4/research/retention_scorer.py` (read-only) · artifact: `v4/reports/597_scorecard.json`_

Frank's mandate: Memory V4 is measured against **availability, correctness, retention, context
economy** — not retrieval latency. This file is the before-number. Every figure below came from a
query run in this session against live stores; nothing is carried from a prior report.

## Recompute

```bash
ssh lumen@100.117.66.102 'bash /home/lumen/minerva/context/claude_context.sh --core > /tmp/core.txt && \
  /home/lumen/bin/lumen-run start --name rscore -- /usr/bin/python3 \
  /home/lumen/minerva/v4/research/retention_scorer.py --core-cache /tmp/core.txt \
  --bench $(ls -t /home/lumen/minerva/eval/results_*.json | head -1) \
  --json /home/lumen/minerva/v4/reports/597_scorecard.json'
```

Positive control (never against prod — a copy, with a planted conflict):

```bash
ssh lumen@100.117.66.102 'cp /home/lumen/minerva/minerva.db /tmp/plant.db && \
  sqlite3 /tmp/plant.db "insert into state_facts (id,subject,predicate,value,valid_from,ingested_at,fact_class) values (\"p1\",\"thothtest\",\"ram\",\"16GB DDR4 ECC installed\",\"2026-08-14\",\"2026-08-14\",\"identity\"),(\"p2\",\"thothtest\",\"hw\",\"Seneca MP i3-1115G4, 32GB DDR4 RAM installed, 512GB NVMe\",\"2026-08-14\",\"2026-08-14\",\"identity\")" && \
  /usr/bin/python3 /home/lumen/minerva/v4/research/retention_scorer.py --control-db /tmp/plant.db --json /tmp/control.json'
```

## Headline

| Axis | Baseline 2026-08-14 | Confidence |
|---|---|---|
| **A availability** — right fact surfaced unprompted | **0/32 = .000 strict**, 22/32 = .688 generous | measured, bracketed |
| **B correctness** — stale/conflicting DETECTED not served | trap@1 **.250**, headline R@1 **.728**/MRR **.793**; **1** live cross-predicate hardware conflict; **37/108** path-anchored facts older than the file they describe | measured |
| **C retention** — no re-derivation across sessions | labelled F7+F12 **7**; self-reported re-derivation **12/1096 = 1.1%** (floor) | **partly not measurable — see C3** |
| **D context economy** — targeted query beats bulk read | boot injection **19,929 tok**, of which **531 tok (2.7%) is live state**; full memory 138,505 tok; topic query 14,711 tok | measured; **read-rate not measurable — see D2** |
| **campaign metric** — fq `caught_by=frank` | **17/55 = 31%** | measured |

## fq campaign metric

`fq stats`, 2026-08-14 22:1xZ: 571 rows total, 55 curated. caught_by: agent 34 · **frank 17** ·
monitor 3 · never 1 → **17/55 = 31%**.

Monthly (curated, by occurred): 03 `1/2` · 04 `1/1` · 05 `0/1` · 06 `2/7` · 07 `5/13` · 08 `8/31`.

The dispatch cites 16/49 = 33%; the corpus moved under me during the session (+6 curated rows from
#572/#595 work). Both are the same measurement at different times — the number to drive down is
the ratio, and today's read is **31%**.

---

## A — AVAILABILITY: was the fact surfaced *unprompted*, at decision time?

**Proxy.** The 55 curated fq records are labelled ground truth: each carries `prevented_by`, the
query that would have prevented the failure. Classify that query by the on-box source it reads,
then ask whether the session-start injection (`claude_context.sh --core`) already carries that
source **as a live value**.

**Denominator discipline.** 9 of 55 records are preventable only by a *process rule* (host guard,
atomic claim, plant-a-known-positive) and 14 could not be classified to a source. No memory system
prevents "run the host guard", so those 23 are excluded: the availability denominator is the
**32 records preventable by an on-box data lookup**.

**Grading.** `full` = the value an agent needs is in the block. `partial` = the block carries a
narrower thing sharing the name. Scoring partial as full would be the falsely-passing audit
(fq#26–29) applied to my own instrument, so both ends are reported.

| Source the failure needed | records | carried at boot | grade |
|---|---|---|---|
| queue state (wq lane / fq patterns) | 14 | aq **open count only** | partial |
| episode store | 6 | top-6 for the fixed query "session start core context" | partial |
| repo/git/restic state | 5 | — | **absent** |
| live config file (drop-in values, mtime) | 5 | — | **absent** |
| runtime probe (curl/DOM/PSI/redis) | 4 | vault HA + one public HTTP code | partial |
| systemd state | 2 | **4-unit allowlist**, not failed-unit enumeration | partial |
| hardware probe (lsblk/free/smartctl) | 2 | — | **absent** |
| state store values | 2 | — | **absent** |
| SpendLogs | 2 | — | **absent** |
| plug telemetry (wattage) | 1 | — | **absent** |

**AVAILABILITY@BOOT = 0/32 = .000 (strict) · 22/32 = .688 (generous).**

Not one source is carried at full grade. Every "carried" source is carried in a narrower form than
the failures needed: the aq **count** is not wq/fq state, the 4-unit allowlist is the exact fq F9
SILENT_GAP mechanism (logrotate was invisible for 6 days to precisely this shape of check), and the
episode retrieval runs against a fixed generic string rather than the work the session is about to
do. Every source is *mentioned in prose* somewhere in the injection — the tool names are all there.
Treating a tool's name in a changelog as "the fact was available" is the fq F1 error, so mention is
scored separately from carriage and contributes nothing.

**Catch latency** (all 55 curated, the second availability view — how late the truth arrived):

| surfaced by a mechanism | discovered late by an agent | reached Frank | never caught |
|---|---|---|---|
| **3** | **34** | **17** | **1** |

3/55 = 5.5% of catalogued failures were caught by something the fleet built to catch them. The
canonical instance is fq#1: Nephthys measured 85W wall at idle by `lumen-plug-snapshot.timer` every
60 seconds for months, and the migration recommendation was written without reading it. That
wattage is still not in the boot injection today.

---

## B — CORRECTNESS: stale and conflicting facts DETECTED, not served

### B1 Golden v4 (the #572/#595 instrument, invoked not forked)

Re-run this session: `recall_bench.py --label wq597 --golden eval/golden_v4_frozen_20260814.jsonl`
→ `eval/results_wq597_20260814_2220.json`, scoring semantics `wq595`, golden sha256 `8ba5af…`,
`golden_differs_from_live=false`, controls `control-hit` rank 1 / `control-miss` null → **control_ok**.

| metric | value | n |
|---|---|---|
| headline R@1 (retrieval-only, trap penalty applied) | **.728** | 239 |
| headline MRR | **.793** | 239 |
| **trap@1** — stale note served at rank 1 | **.250** | 16 trap rows |
| abstention pass-rate | .500 | 6 |
| federated coverage R@1 (#573 target) | .000 | 10 |
| contradiction-resolution R@1 | .600 | 5 |
| knowledge-update R@1 | .333 | 6 |

trap@1 = .250 is the correctness number that matters for Frank's axis: in one of four
trap-bearing queries the store still puts the stale note first. #595 owns and alarms it.

### B2 Live conflict census over `state_facts` (666 open facts)

| check | result |
|---|---|
| same subject+predicate, contradictory open values | **0 — structurally impossible** |
| cross-predicate hardware contradictions | **1** |
| IP contradictions (lan/tailscale families) | 0 |
| config-facts older than the file they describe | **37 / 108** path-anchored (21 of them regular files, 16 directories) |
| open facts naming a path not present on this host | 22 |
| never confirmed since ingest | config 49/400 · identity 47/134 · status 3/25 · narrative 3/107 |
| age of open facts | <7d 177 · 7–30d 300 · 30–90d 184 · >90d 3 |

**The dup-open check is 0 by construction, not by health.** `idx_sf_one_open` is a UNIQUE index on
`(subject, predicate) WHERE valid_to IS NULL`. Intra-predicate conflict *cannot* exist in this
store. Anyone reporting "0 conflicts" from that query is reporting the schema, not the state. The
real conflict surface is **cross-predicate** and **store-vs-file**, which is what the other two rows
measure.

**The one live conflict — `khepri`:**

- `khepri.hw` (open): "…RAM: 24GB DDR3 ECC installed (22Gi usable); 64GB kit is ON HAND but NOT INSTALLED"
- `khepri.ram` (open): "64GB DDR3 ECC INSTALLED — Frank confirmed 2026-08-13"

This is fq#5 still live: the store contains the record that told Frank he was wrong about his own
hardware, coexisting with his correction, under a different predicate. Khepri is powered down
(`ping 100.120.16.78` fails; state: POWERED DOWN 2026-07-16), so it **cannot be verified live this
session** — Frank's 2026-08-13 confirmation is the authority. Correction is Frank-gated and
proposal-only here:

```bash
# PROPOSAL — not executed by wq#597
ssh lumen@100.117.66.102 'python3 /home/lumen/minerva/lumen_state.py ingest khepri hw \
  "Mac Pro 6,1: E5-2697 v2 (12C/24T), 64GB DDR3 ECC INSTALLED (Frank 2026-08-13), 256GB SM0256G \
SSUBX blade, NIC enp12s0 MAC 00:3e:e1:c5:75:94, Ubuntu 26.04" --valid-from 2026-08-13 \
  --source "wq#597 conflict census; supersedes 24GB" && /home/lumen/bin/state-regen'
```

### B3 Positive control — the census went RED before it was believed

A clean sweep is not trusted until it has failed on a plant (fq F1 ×4). Two conflicting facts were
planted on a **copy** of `minerva.db` (`thothtest.ram` 16GB vs `thothtest.hw` "32GB DDR4 RAM
installed, 512GB NVMe"); the census detected both. Corpus 668 open facts on the control copy, 666
on prod. Prod is untouched.

The plant also **found a defect in my own detector**: the first version vetoed any GB value with
storage vocabulary within ±45 chars, so the planted 32GB was killed by the "512GB NVMe" later in
the same sentence, and a fact-level "kit / on hand" veto had silently dropped the *known* khepri
positive. Both were fixed and the run repeated. That is the whole argument for the rule: the sweep
was clean, and clean was wrong.

---

## C — RETENTION: no re-derivation across sessions

### C1 Labelled ground truth

fq curated F7 REDUNDANT_WORK (4) + F12 LOST_HANDOFF (3) = **7 records**, 3 of which reached Frank:

| fq | date | class | caught | what |
|---|---|---|---|---|
| 10 | 2026-03-09 | F12 | frank | 25 posts published against a twice-repeated STOP from prior sessions |
| 13 | 2026-06-16 | F7 | agent | two writer sessions created duplicate /log pages, each removing the other's work |
| 39 | 2026-07-01 | F12 | agent | multiple sessions re-walked the mcpw session-affinity path after it was a proven dead end |
| 15 | 2026-07-09 | F7 | agent | claim race: two agents claimed the same simpill ticket |
| 14 | 2026-07-11 | F7 | frank | systemic duplicate work across parallel sessions in the Fable window |
| 43 | 2026-08-13 | F7 | agent | agent ID collision: two workers under one identity corrupted claim attribution |
| 562 | 2026-08-14 | F12 | frank | #572 worker ended with no `wq done`, no episode, no report |

7/55 = 13% of the catalogued corpus is re-derivation or lost handoff.

### C2 Self-reported re-derivation over the episode corpus

1,096 episodes carry a structured post-mortem. Scanning bodies for explicit admissions
("already done/shipped/answered", "duplicate work", "re-derive", "redid", "dead end re-walked"):

**12 / 1096 = 1.1%.** Earliest 2026-07-03, most recent 2026-07-14; clustered in the
simpill/interview-prep parallel-agent window, which is the same window fq#14/#15 label.

This is a **floor, not a rate**. It counts only re-derivation an agent *noticed and wrote down*.
Silent re-derivation — the expensive kind, where the session simply never knows the question was
answered in June — is invisible to it by construction.

### C3 What was tried and rejected — title-similarity clustering

Clustering episodes by title-token Jaccard (≥0.5, ≥1 day apart) returned 4 clusters / 28 "redundant
sessions" / a 2.6% rate. **That number is discarded.** Inspection shows it measures the wrong thing
in both directions:

- **False positives**: every cluster is templated *serial* work — `simpill uplift #2..#5` (one util
  per session, by design, per SOP), `lumen-corrector auto-fixed …` runs, `factory APPLIED plan …`
  route-adds. High title overlap, zero redundancy. This is throughput, and counting it as waste
  would have manufactured a metric that improves when the fleet stops shipping.
- **False negatives**: it catches **none** of the 7 labelled F7/F12 cases. Real re-derivation
  re-answers a *question* from a differently-worded session; the titles share no vocabulary.

Reported in the artifact as a negative control (`C_retention.title_jaccard_experiment`), not as a
score. A defensible "not measurable yet" beats a fabricated metric.

### C4 Cost frame

`LiteLLM_SpendLogs`: 127,604 calls, 2026-07-10 → 2026-08-14 (36 days), **$38.59 total, $1.07/day**,
mean $0.000302/call. Re-derivation cannot be attributed inside that today — spend rows carry no
question identity, and the expensive re-derivation (interactive Claude sessions) rides the Max
subscription and never appears in SpendLogs at all. Any dollar figure for axis C would be invented.
The honest cost unit today is **sessions**: 7 labelled incidents, of which fq#39 alone spans
multiple sessions re-walking a closed dead end, and fq#10 cost 25 unwanted published posts.

### C5 What would make retention measurable (proposal-only)

1. **Precheck telemetry.** `lumen_episode.py precheck` already exists and is the designed guard; nothing
   records whether it ran, what it returned, or whether the agent proceeded anyway. Log
   `(session, plan_text, verdict, n_hits, top_hit_id, proceeded)` to a table. Retention then has a
   real denominator: *of sessions that started work matching a prior episode ≥ threshold, how many
   consulted it first* — the direct analogue of trap@1 for retention.
2. **Question-level keys.** Have `wq done` / episode commit record the *question answered*, not only
   the work done. Repeat detection over questions is tractable; over titles it is not (C3).
3. **Dead-end ledger.** fq#39's mechanism: a result that closes a path is currently written as prose
   in one session's report. `fq` can carry a `dead_end` flag so a later session's precheck hits it.

---

## D — CONTEXT ECONOMY: does a targeted query beat a bulk read?

Tokenizer: `tiktoken cl100k_base`. Measured this session.

| artifact | bytes | tokens | vs full |
|---|---|---|---|
| `claude_memory.md` (full read) | 487,381 | **138,505** | 1.0× |
| `claude_context.sh --core` (boot injection) | 65,204 | **19,929** | **6.9× cheaper** |
| `claude_context.sh "<topic>"` (topic query, 10.4s) | — | **14,711** | 9.4× cheaper |
| `claude_memory_index.md` | 31,626 | 11,182 | 12.4× cheaper |

**Two findings, both against the fleet's own assumptions.**

1. **The boot injection costs 10× its documented budget.** `claude_context.sh` advertises
   `--core # core sections only (~2K tok)`. It is **19,929 tokens**. The standing token-reduction
   order in Frank's preferences quotes ~2k at session start and 3–4k for topic queries; both are
   off by roughly an order of magnitude, and every session in the fleet pays the difference.
2. **Almost none of that budget is current fact.** Composition of the 19,929:

   | segment | tokens | share |
   |---|---|---|
   | selector-retrieved prose sections | 13,142 | 66% |
   | HANDOFF.md | 5,773 | 29% |
   | INSTINCTS | 481 | 2.4% |
   | **VERIFY NOW (live state)** | **531** | **2.7%** |

   The block that carries live state — the thing axis A shows is missing — is 2.7% of the injection.
   The fleet is spending 97% of its boot budget on prose and 2.7% on what is true right now. That is
   Frank's "excessive context window sizes" and "not having information readily available" as a
   single number: they are the same defect.

A topic query saves only **26%** against the core injection (14,711 vs 19,929), so "targeted query
beats bulk read" is true against the *full file* (9.4×) but nearly a wash against *core* today.

### D2 How often bulk reads actually happen — NOT MEASURABLE TODAY

`fq` carries **1** F8 CONTEXT_BLOAT record. 35 episodes mention `claude_memory.md`; **0** match a
bulk-read pattern. Neither is a rate: nothing logs reads of `claude_memory.md` — it is a plain file,
read by `cat`, `view`, or an MCP call, none of which leave a trace. The 1 and the 0 are artifacts of
recording, not behaviour, and quoting either as "bulk reads are rare" would be exactly the fq F1
error this scorecard exists to count.

**Instrumentation that would make it a rate (proposal-only):**
- `auditd -w /home/lumen/claude_memory.md -p r -k memread`, or a wrapper that logs
  `(ts, pid, comm, bytes)` per read; either gives reads/day and read-size distribution directly.
- Log `context_selector.py` invocations with query + delivered tokens (it already computes
  `total_delivered_tokens`, and discards it) → cost per session and the core-vs-topic split become
  a time series instead of one measurement.
- Both are additive, read-only, and no service restart is needed for the auditd rule.

---

## Limits of this scorecard

- **A** is scored against `--core` output captured at 22:20Z today; the injection is dynamic
  (selector output varies), so the strict/generous bracket is more durable than either endpoint.
- **A**'s denominator excludes 14 unclassifiable records; a better `prevented_by` taxonomy at `fq add`
  time would shrink that.
- **B**'s mtime check flags 16 directories whose mtime moves for unrelated reasons — the 21
  regular-file rows are the trustworthy subset; "flagged for re-verification" ≠ "wrong".
- **C** has no honest rate, only a floor and a labelled count. See C5.
- **D2** has no rate at all. See the instrumentation list.
- The fq corpus is recording-biased toward recent months (the FA report's own caveat): rising counts
  mean better recording, not necessarily worse behaviour. The **ratio** is the metric, not the count.
