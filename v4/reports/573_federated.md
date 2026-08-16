# wq#573 — Federated recall: claude_memory.md sections + session archives

**Agent:** mv4-w573-cd3643 · **Lane:** memory · **Date:** 2026-08-14/15 (PDT)
**Golden:** `eval/golden_v4_frozen_20260814.jsonl` sha256 `8ba5afb0ce12585606b8dcff5ffb8245af24b0f0f32ccbddf3d4121f1bbf46b7` (0444, identical to live at run time)
**Verdict: index SHIPPED and live · retrieval flag `MEMORY_V4_FEDERATED` stays DEFAULT OFF — measured headline loss.**

---

## 0. Stage table — the whole ticket in one view

| Arm | R@1 | MRR | federated R@1 | trap@1 | abstention | p50 | Verdict |
|---|---|---|---|---|---|---|---|
| **A control** (flag unset) | **.778** | **.836** | **.000** (n=10) | .188 | .500 | 176ms | baseline |
| B federated ON (lex over ctx+content) | .749 | .814 | **.700** | .188 | .500 | 192ms | **REJECT — 5 classes down ≥2pts** |
| C federated ON (lex over content only) | .749 | .816 | .700 | .188 | .500 | 192ms | **REJECT — same** |
| B2 repro of B | .749 | .814 | .700 | .188 | .500 | — | harness determinism check: byte-equal to B |

Corpus motion bracketed: `minerva.db` mtime was **02:22:58Z at the start of arm A, at the
mid-point, and at the end of arm B** — identical, so nothing in the A/B delta is corpus drift.
(It moved once at 02:28:02Z, after arm B2 had already reproduced arm B's headline exactly;
that reproduction is the evidence the motion bought nothing.)

Baseline note (fq F1): arm A was **re-measured this session**, not taken from #574. It reads
.778/.836 against #574's recorded .778/.837 — a .001 MRR of corpus, and the number every
row below gates against.

## 1. What shipped and is live

`/home/lumen/minerva/lumen_federate.py` — read-only indexer, **407 chunks** now in the store:

| kind | chunks | tokens | min/max tok | unembedded | sections |
|---|---|---|---|---|---|
| prose-section | 391 | 121,129 | 9 / 499 | 0 | 245 |
| session-archive | 16 | 2,436 | 30 / 450 | 0 | 14 |

- **Sources are read-only.** Nothing writes `claude_memory.md` or `sessions/`. Prose
  lifecycle/demotion is wq#577.
- **`archival_notes` is untouched.** New tables `federated_chunks` + `federated_files` only.
  A mutation of the note store would have put every V3/V4 measurement back in play.
- **Segmentation authority = the section index.** Sections are re-derived from the *live*
  file with `tag_sections.py`'s exact H2 split + `slugify`, so `section_id` here equals
  `section_id` in `claude_memory_index.md`. The index itself was 52 min stale at build time
  (generated 22:04, file mtime 22:56) and reports 244 sections against the live 245 — same
  rule, fresh bytes, matching ids.
- **Chunking** ~200–500 tok: fenced code blocks are never split (half a command is worse
  than no command), H3+ forces a boundary so a chunk never silently spans two subsections.
  296/407 land in the 200–500 band; the 111 below it are whole short sections, not fragments.
- **Contextual augmentation** (plan §1b, Anthropic contextual retrieval) prepended pre-embed
  and pre-lex: document, section title, date, subsection path, part i/k, section lead line.
- **Incremental**, keyed on file mtime + per-chunk content sha. Proven on a scratch DB rather
  than asserted: no-change re-run → 0 embeds / 0.0s; one-chunk edit → 1 changed, 1 unchanged;
  section deleted → orphan row deleted, no stale chunk left behind.
- **Dedup is proposal-only**: 28 intra-prose near-duplicate pairs ≥0.95, 0 federated↔archival.
  Written to `dream/federated_dedup_20260815_0229.md`. Nothing suppressed, nothing merged.

### Two defects found by the instrument, both fixed
1. **Context prefix carried `section 3/244`.** That makes every chunk's embed text a function
   of the file's *total* section count, so appending one section to `claude_memory.md` would
   re-embed all ~400 chunks, forever. Caught by the scratch-DB incremental test, not by
   reading the code. Total count dropped from the prefix; full re-index run after the fix.
2. **Session archives were unreachable by their own topic.** A session's topic is written down
   *only* in its filename; its internal H2s are boilerplate ("What changed this session"). But
   `_tokens()` keeps hyphenated runs whole, so `vault-switchover-and-handoff-system` was a
   single token no query could match. `session_label()` now splits the slug back into words.

## 2. Retrieval integration

`MEMORY_V4_FEDERATED` (default **0**), folded into `search_v3` as an additive candidate source
under the **adopted #574 weights, unretuned**. `graph` and `entity` are structurally 0.0 for a
chunk — prose carries no `memory_edges`, because the edge extractor runs over notes. Two-pass
scan (blobs first, text for the top `FED_POOL=40` only) so a cold query does not drag ~500KB of
prose across the sqlite boundary. Fail-open: a missing table, a bad blob, or any exception
returns the archival list untouched — prose can never take recall down with it.

Flag OFF reproduces V3+#574 behaviour with every env var unset (arm A is exactly that run).

## 3. Why it lost, measured rather than guessed

Coverage worked. `first_hit_tier_counts` proves the answers came from the **new tier**, not from
note-leakage (the #572 defect class): control `{archival: 223}` → federated `{archival: 219,
federated: 11}`. Federated R@1 .000 → .700, and only qid253 misses the top-10 entirely, so
9/10 federated rows are answerable now. prose-section .000 → **.857** (6/7 at rank 1).

The cost is displacement, and it is spread across every headline class:

| class | n | R@1 control | R@1 federated | Δ |
|---|---|---|---|---|
| build | 33 | .909 | .848 | **−.061** |
| outage-rca | 61 | .770 | .721 | **−.049** |
| hardware | 21 | .762 | .714 | **−.048** |
| decision | 31 | .839 | .806 | **−.033** |
| procedure | 37 | .649 | .622 | **−.027** |
| gotcha | 44 | .818 | .841 | +.023 |
| prose-section | 7 | .000 | .857 | +.857 |
| session-archive | 3 | .000 | .333 | +.333 |

Exactly 8 headline queries lost rank 1 and 8 federated queries gained it. **The 8 losers still
resolve to `tier: archival`** — the correct note was not replaced, it was pushed down one slot
by a prose chunk that landed above it.

Two hypotheses were tested, not assumed:

- **Lexical inflation from the prefix.** Our `lex` is fraction-of-query-tokens-matched with no
  idf term, so a prefix repeating the document label and section title hands free matches on
  house vocabulary to all ~400 chunks at once. Arm C scores `lex` over the raw chunk instead.
  **Falsified:** R@1 .749 either way, MRR +.002. `MEMORY_V4_FED_LEXCTX` defaults to 0 on that
  marginal edge, and the finding is that the displacement is *topical*, not lexical noise.
- **Are the displacers wrong?** Inspected: Q43 ("lumen ram upgrade to 64gb details") is beaten
  by the LUMEN STORAGE TOPOLOGY section; Q97 (Gatus SSH-AUTH probe) by the prose account of the
  same t85 work. These are on-topic, and for a reader arguably useful. But golden v4 is
  known-item against a *specific note*, and this ticket does not get to re-judge the golden set.
  Recorded, not argued.

## 4. Adopt rule, line by line

| Condition | Required | Measured | |
|---|---|---|---|
| federated_coverage strictly up | yes (target ≥8/10) | .000 → .700 (7/10 @1, 9/10 @10) | up, **below target** |
| headline: no class down ≥2pts | pass | 5 classes down .027–.061 | **FAIL** |
| trap@1 not up | pass | .188 → .188 | pass |
| abstention not down | pass | .500 → .500 | pass |
| p50 at the contended number (#596 §4.2) | budget | 176 → 192ms | pass (moot) |

**One hard fail ⇒ NOT ADOPTED.** Flag default stays `0` in code. `sched_baseline.json` was
**not** re-seeded (fq#592: that decision is the orchestrator's, not the builder's), and the
drift alarm is unaffected because it compares the retrieval-only headline, which the flag-off
default leaves untouched.

## 5. Tier controls (fq F1 — no clean sweep is trusted until it goes RED)

`eval/wq573/tier_controls.py`, exit code wired to the verdict. Artifact:
`v4/reports/573_tier_controls.json`.

| control | OFF | ON | |
|---|---|---|---|
| P1 prose-section ("where is the nordlynx token stored") | miss | **rank 1, tier=federated** | GREEN |
| P2 session-archive ("how was the vault failback to maat primary done") | miss | miss | **RED** |
| N1 planted miss (string exists in no source file) | miss | miss | GREEN |

**P2 is RED and is being reported RED.** It is qid253, the one federated row still outside the
top-10. Diagnosed rather than patched around: the chunk carrying `vw_maat_20260701_152552` sits
at **dense rank 4 of 407 inside the tier** — it is generated, scored, and simply out-competed by
six archival vault notes that each carry g=.833 and ent=1.0 bonuses a prose chunk structurally
cannot have. That is the same asymmetry as §3, not a separate bug. Two attempts on this stage
are spent (the `session_label` fix moved it into the pool but not into the top-10), so per the
stop-rule it is recorded as a loss rather than chased with a third.

## 6. Recorded for follow-up — NOT done here

- **Tier prior / structural asymmetry (#602-adjacent).** A federated chunk is scored on d+lex
  alone while a note gets up to `.05·g + .02·ent` on top. Correcting that is a fusion-weight
  change, which this dispatch explicitly forbids me to make. It is the single most likely route
  to a federated arm that clears the class rule, and it should be measured with the weight
  search that owns weights, not bolted on here.
- **LLM contextual augmentation.** The literal Anthropic recipe generates per-chunk context with
  an LLM (measured −35/−49/−67% top-20 failure). The `--ctx` choices are deliberately limited to
  `header`/`none` today: the header context is free, and it had to be the arm that loses before
  a LiteLLM batch over ~400 chunks is worth spending. It has now lost on the headline but *won*
  the coverage axis outright, so the follow-up is worth filing.
- **Prose lifecycle (wq#577)** consumes this index: 28 intra-prose duplicate pairs are demotion
  candidates already sitting in the dream report, and every demoted section stays retrievable
  here by construction.

## 7. State

- Index **live and incremental**; re-running `lumen_federate.py index` costs 0 embeds / 0.0s
  until a source file's mtime moves. Initial batch: 407 embeds in 105s via `lumen-run`.
- Flag default OFF ⇒ production recall is byte-for-byte the #574-adopted behaviour.
- Backups (md5-verified): `lumen_recall2.py.bak.20260814_pre_wq573`,
  `/home/lumen/backups/minerva.db.bak.20260814_pre_wq573`.
- No service stopped, no reboot, no source file mutated, no Anthropic API spend,
  no `sched_baseline.json` re-seed. `minerva-db` slock released for #575.
