# wq#604 — LLM contextual augmentation for federated chunks: NOT ADOPTED (recorded loss)

**Agent:** mv4e604-954616 (continuing mv4d604-15d273's build, which died mid-ticket on a
`task_notification` frame right after its indexing job finished — same drain-death class as
mv4-drain2, per orch's RELIABILITY_SPEC diagnosis) · **Lane:** memory · **Date:** 2026-08-15/16
**Golden:** `eval/golden_v4_frozen_20260814.jsonl` sha256
`8ba5afb0ce12585606b8dcff5ffb8245af24b0f0f32ccbddf3d4121f1bbf46b7` (0444, wq#595 semantics)
**Verdict: `--ctx llm` code SHIPPED and correct · NOT adopted as the default context mode —
measured regression on both wq#595 correctness axes, zero fix to #573's original failure.**

---

## 0. Provenance — what I inherited vs what I did

mv4d604-15d273 built the whole `--ctx llm` mechanism (offline LiteLLM batch context generation,
`federated_llm_ctx_cache` table, fail-open-to-header on any completion error) and ran the full
`index --ctx llm --full` batch via `lumen-run` (`lumen-job-wq604-llmctx-1786821804.service`,
journalled: 411 chunks, 410 new LLM contexts, 0 fail, 0 embed_fail, exit 0, 568s). It died waiting
on that job's `task_notification` before running the golden measurement or writing this report.
DB backup from that session verified present: `/home/lumen/backups/minerva.db.bak.20260815_pre_wq604`.
No gitea PR/branch existed for this work (checked: 0 PRs, 1 branch=main on the minerva repo) — it
was sitting uncommitted in the shared tree. Verified the code before trusting it: read
`lumen_federate.py` in full, confirmed schema/cache/fail-open logic match the ticket's spec and
the module's own `#604` comments, then re-verified live behaviour myself (see below) rather than
taking the prior session's numbers on trust.

I took a fresh backup (`minerva.db.bak.20260816_pre_wq604e`) before any further mutation, then ran
the golden measurement this session (fq F1 discipline — no clean sweep trusted without a positive
control, both `recall_bench.py`'s mandatory control-hit/control-miss rows and my own header↔llm
same-session A/B below).

## 1. Isolated A/B — same corpus, only variable = ctx_mode

Both arms `MEMORY_V4_FEDERATED=1`. `MEMORY_V4_PROSE_ARCHIVE_SOURCE` confirmed `0` throughout
(zero `prose-archive` rows in `federated_chunks` at every measurement point — the wq#577
regression is a different, already-gated mechanism). Header and llm arms were run **in the same
session, same `minerva.db` moment**, by re-indexing in place between measurements
(`index --ctx <mode> --full`, no source file touched, no LLM re-spend for the llm arm — 411/411
cache hit on `federated_llm_ctx_cache`) — the only thing that changed between the two runs is
which text got embedded per chunk.

| Arm | R@1 | MRR | trap@1 | abstention pass | federated coverage |
|---|---|---|---|---|---|
| **control** (federated off) | .774 | .834 | .188 (3/16) | .500 (3/6 fail) | n/a |
| **header** (adopted #573 default) | .749 | .815 | .188 (3/16) | .500 (3/6 fail) | prose-section .857 / session-archive .333 |
| **llm** (this ticket) | .749 | .817 | **.250 (4/16)** | **.333 (2/6 fail... pass)** | prose-section .857 / session-archive .333 (identical) |

(Control re-measured fresh this session per fq F1 — reads .774/.834 against #573's .778/.836,
a small corpus-drift delta of the same shape #573 itself reported for its own baseline, not a
scoring or code difference.)

Headline R@1 is **byte-identical** between header and llm (.749 both); MRR moves +.002, inside
noise. Per-class retrieval-headline deltas vs control are **identical between the two arms**
(build −.061, hardware −.048, outage-rca −.049, all others 0) — switching context-generation
method changed *which* individual queries won or lost (12 of 239 rows flipped rank, itemized
below) but left the aggregate displacement #573 already measured and failed on **completely
unchanged**. LLM context does not touch the failure mode that killed the header arm.

Per-query rank churn (12/239, header→llm): qids 34,43,65,71,76,84,102,110,118,162,167,210 — a mix
of +1/−1 rank shuffles, net zero on R@1, confirming the "identical headline, different queries"
read above.

## 2. The two axes that DID move — both wrong direction, both hard disqualifiers

Per wq#595 law: **trap@1 rising is disqualifying on its own; abstention pass-rate falling is
disqualifying on its own.** LLM context does both, independently of the headline wash above.

- **trap@1 .188 → .250**, new trap: **qid243** (abstention class, query "when did lumen memory
  migrate to pgvector", trap substring `pgvector`) — passes under header, fails under llm.
- **abstention pass .500 → .333**, new fail: **qid239** (query "what caused the mt moon b1f
  collapse outage", trap substrings `919-step`/`confinement`) — passes under header, fails under
  llm. (qid243 double-counts here since it is abstention-class *and* trap-bearing; qids
  240/241/243 are the pre-existing floor both arms share, per wq#577's original finding.)

### Root cause, diagnosed not guessed

Both trap chunks (`fed_17bff4ea03718f05`, `fed_92e1c003a5d41733`) contain their trap substring
**in the raw chunk content itself**, confirmed by direct read — these are legitimate topically-
adjacent chunks, not bugs in the indexer. The `federated_llm_ctx_cache` text for both is a genuine
one-sentence *semantic paraphrase* of the chunk ("Diagnosis-first investigation run resolving Mt
Moon navigation failures..." for the confinement chunk), versus header ctx's inert metadata line
("From claude_memory.md, section: 'X'. Dated Y. Subsection: Z."). `embed_text = ctx + content` in
both cases, so the content's trap term is present in the embedding input either way — but the LLM
paraphrase **concentrates and restates the chunk's core topic into the prefix**, which pulls the
dense embedding closer to a generic topical match for a query about that same topic, while the
header prefix adds no topical signal beyond bare metadata. This is the same failure shape wq#577
found with the archive source (richer semantic surface area → higher trap/abstention false-positive
risk), reproduced here via a different mechanism (LLM paraphrase vs. a whole extra corpus).
Verified live (`recall_bench.py --qid 243/239 --engine v3` with `MEMORY_V4_FEDERATED=1`, llm ctx
state) — both traps rank inside top-2, confirmed against saved per-query JSON before and after.

## 3. Adopt rule, line by line (wq#595 law + #573's own rule)

| Condition | Required | Measured (llm vs header, same corpus) | |
|---|---|---|---|
| headline: no class down ≥2pts vs control | pass | same 3 classes down as header (unchanged, not newly failing, but not fixed either) | **carries forward #573's FAIL** |
| trap@1 not up | pass | .188 → .250 | **FAIL** |
| abstention not down | pass | .500 → .333 | **FAIL** |
| federated coverage | reference only | identical to header (.857/.333) | no change either way |
| MRR | informational | +.002 | noise, not a case for adoption on its own |

**Two independent hard fails ⇒ NOT ADOPTED**, on top of inheriting (not fixing) #573's original
failure. This is a strictly worse arm than the already-not-adopted header arm: same displacement,
plus two new correctness regressions the header arm didn't have.

## 4. Disposition (adopt-or-record, wq#576 precedent)

- **Code stays shipped.** `lumen_federate.py --ctx llm` is correct, tested, fail-open on LLM
  errors, and cheap to re-run (cache-hit, zero LLM spend on any future re-index while source bytes
  are unchanged) — it is infrastructure worth keeping even though this measurement didn't clear
  the bar, exactly as `--ctx header` itself would still exist if some future arm beat it.
- **CLI default stays `header`** (unchanged — `--ctx` already defaulted to `header` before this
  ticket; this ticket never had authority to change that default, and the measurement gives no
  reason to).
- **Production `federated_chunks` restored to `ctx_mode=header`** at the end of this session
  (verified: 411/411 rows, 0 `llm`-mode rows remaining) — matches #573's shipped state. Since
  `MEMORY_V4_FEDERATED` stays default `0` (unchanged, #573's own gate), none of this measurement
  work touched production recall at any point.
- **`federated_llm_ctx_cache` left populated** (411 rows) — a sunk-cost asset. Any future revisit
  (e.g. a smarter context prompt, or pairing llm-ctx with the tier-prior fix #573 §6 flagged as
  the more likely fix for the real displacement problem) re-embeds for free without a re-spend.
- No new flag was needed for this recording — the losing arm was never live (index-time only,
  gated by the CLI default, not a runtime flag) so there is nothing to gate dark beyond what
  already exists.

## 5. For future work on this axis

#573 §6 already named the more promising fix: the federated tier's structural scoring
disadvantage (`d+lex` only, vs a note's `+.05·g+.02·ent`), not context quality. This ticket is
the second data point (after header ctx) that **context-prefix engineering alone does not move
the headline number** — both header and llm context produce the *identical* class-level
displacement vs control. A future ticket chasing this axis should prioritize the tier-prior /
fusion-weight investigation #573 flagged as explicitly out of scope for indexer-only work, rather
than a third context-prefix variant.

## 6. State

- Backups (before any mutation this session): `minerva.db.bak.20260816_pre_wq604e` (this
  session), `minerva.db.bak.20260815_pre_wq604` (inherited from mv4d604, verified present).
- Artifacts (gitignored, local only — `eval/` is excluded from the code-only repo):
  `results_wq604_control_20260816_0236.json`, `results_wq604_llmctx_20260816_0237.json`,
  `results_wq604_headerctx_20260816_0239.json`.
- No service stopped, no reboot, no source file (`claude_memory.md`/`sessions/`) mutated, one
  offline non-messenger LiteLLM batch already paid for by the prior session (410 completions,
  `deepseek-v4-flash`, zero further spend this session — 411/411 cache hits on both re-index
  passes). `minerva-db` subsystem held for the duration of this ticket only.
