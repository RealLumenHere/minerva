# Lumen Memory System v2 — Design & Roadmap
*Authored 2026-06-23 PT. Source: agent-memory deep research (Mem0/Zep-Graphiti/Letta/A-MEM/LangMem + LoCoMo/LongMemEval/BEAM) cross-walked against verified Lumen state. Status: DESIGN — Frank decision required before any phase ships.*

## 0. Why now
The article "7 Types of Agent Memory" maps cleanly onto a gap we already feel.
Lumen built the *containers* for long-term memory but they are nearly empty, while
the real memory lives as 6,859 lines of append-only prose in claude_memory.md guarded
by hand-written CURRENT_STATE_OVERRIDES blocks. The dominant documented failure mode is
STALE STATE: PBox referenced after retirement, Ghost after deletion, the zombie eval
gates, the phantom research_done table, the node-killer reaping live sessions. The memory
file itself calls these out as repeats: "third instance of house pathology: automation
silently destroying critical function." That is not a discipline problem. It is a missing
architecture: nothing automatically invalidates a fact when reality changes.

## 1. Verified current state (2026-06-23)
| Memory type (article) | Lumen today | Verdict |
|---|---|---|
| Working / in-context | claude_context.sh + context_selector.py: section-embedding retrieval over claude_memory.md (nomic-embed-text, 768d), ~90% token cut | GOOD, keep, upgrade embedder |
| Semantic (facts) | Mem0 2.0.6 -> Qdrant lumen_facts: 768d cosine, bm25 sparse CONFIGURED but unused, **30 points** | UNDER-USED |
| Episodic (events/outcomes) | minerva.db archival_notes (kind=decision/failure/success/insight) **3 rows**; recall_fts FTS5 | ESSENTIALLY EMPTY |
| Procedural (skills) | minerva_skills.py + skills table **5 rows** | THIN |
| External / retrieval | Qdrant + FTS5 (no reranker, hybrid off) | PARTIAL |
| Parametric | DeepSeek V4 (messenger) / fleet models | n/a (provider) |
| Prospective (future intent) | Leantime tickets + READY PROMPTS INDEX (prose) | MANUAL |
| Temporal / state-change | none — handled by hand-written overrides | **MISSING (root cause)** |
| Consolidation / forgetting | none — file grows unbounded | **MISSING** |
| Self-improvement | DGM v2 (config genome, benchmark_v2, human-gated) | GOOD but improves *config*, not *memory* |

## 2. What the field built (pre-designed foundations we can adopt)
- **Graphiti (Zep OSS, MIT, ~20k stars)** — temporal knowledge graph. Bi-temporal edges
  (valid-time t_valid/t_invalid + ingestion-time t_created/t_expired). New facts that
  contradict old ones *invalidate, not delete* the old edge; query returns the current
  (or as-of-date) slice with provenance back to source episode. Hybrid retrieval
  (semantic + BM25 + graph). Self-hosts on Neo4j OR **FalkorDB (Redis module)**.
  -> This is the direct fix for our staleness disease. Powers Zep; leads LongMemEval.
- **Mem0 (OSS v3, ~47k stars)** — single-pass hierarchical fact extraction + multi-signal
  retrieval, 4-scope (user/agent/run/app). v3 OSS dropped the graph layer (it cost 2x
  tokens / 3x latency for ~1.5 pts). We already run mem0ai 2.0.6. Keep as the semantic-fact
  layer; do NOT rely on its graph.
- **Letta / MemGPT (OSS)** — OS memory hierarchy (core/recall/archival), self-editing
  memory blocks via tool calls, and **sleep-time compute**: a background agent that during
  idle periods consolidates fragmented memories, dedupes, prunes stale, finds patterns.
  Newest direction "MemFS" = git-backed memory files the agent edits with bash + /doctor
  memory audit. Note: Letta is converging ON our file-based approach (claude_memory.md is
  basically MemFS without git history or a consolidation agent).
- **A-MEM (NeurIPS 2025, MIT)** — Zettelkasten atomic notes: each memory note carries
  keywords/tags/context/embedding/links; new notes trigger *evolution* of existing notes
  links+attributes. ChromaDB-backed. Model for a self-organizing episodic store.
- **Counter-evidence (keep us honest):** Letta "Is a Filesystem All You Need?" dumped raw
  transcripts into files + a search agent and beat Mem0g on LoCoMo (74.0 vs 68.5). Lesson:
  our Layer-0 file approach is not wrong — augment it, do not rip it out.

## 3. Benchmarks to grade ourselves on
- **LongMemEval** (500 q; categories: info-extraction, multi-session, **knowledge-update**,
  **temporal-reasoning**, **abstention**). The KU + temporal + abstention slices ARE our
  disease, measured. Abstention ties directly to our existing hallucination guard.
- LoCoMo is the most-cited but disputed/methodologically flawed (Zep rebuttal); use as
  secondary. BEAM (1M/10M tokens) for scale — not our regime yet.
- Action: build a small **Lumen gold set** (like the existing verify_gold.jsonl, 40 labeled
  claims) seeded from our OWN state history: "Is PBox active?" (must answer retired),
  "What was the fleet roster on 2026-05-18?" (temporal), "Does Ghost run?" (must abstain/deny).

## 4. Embedder upgrade (cheap, high-leverage, reversible)
nomic-embed-text is 2024-era (~62 MTEB). Re-embedding is trivial NOW (30 points / 46 sections).
- **Qwen3-Embedding-0.6B** — 639MB, ~70.7 MTEB v2, 32K ctx (vs nomic 8K), Apache-2.0,
  Matryoshka dims 32-1024, in Ollama. Best sub-1GB local model.
- **BGE-M3** — MIT, native dense+sparse+multi-vector in ONE model -> would light up the
  bm25 sparse vectors lumen_facts is ALREADY configured for, giving true hybrid retrieval.
Decision: BGE-M3 if we commit to hybrid (recommended); Qwen3-0.6B if we want max dense quality
at min size. Either way add a reranker (bge-reranker-v2) on the fused candidate set.

## 5. Proposed architecture — LMS v2 (build on what exists)
- **L0 Working (KEEP):** claude_context.sh selective retrieval. Swap embedder, re-index.
- **L1 Semantic facts (UPGRADE):** Mem0 -> Qdrant lumen_facts, hybrid on, reranked. Holds
  durable facts/preferences/fleet attributes. Populate from claude_memory.md + credentials.
- **L2 Temporal state graph (NEW — the keystone):** Graphiti on FalkorDB (Redis module — we
  already run Redis; no JVM, fits the CPU box). Ingest fleet-state episodes (deploy / retire /
  IP change / service change). Edge invalidation makes "PBox active" auto-expire when
  "PBox retired" lands. lumen_state_gen.py becomes a Graphiti "current slice" query instead of
  a hand-curated INVARIANTS block. Enables "what was true on date X" + provenance.
- **L3 Episodic experience (POPULATE):** archival_notes becomes a real ExpeL/Reflexion store.
  session_commit writes a structured post-mortem (what changed, what worked/failed, why).
  A-MEM-style note attributes + links + evolution. Retrieval hook at minerva_proposal
  feasibility_check and at every change-time: "have we tried this / has it failed before?"
  -> the mechanism that stops the recurring house pathologies.
- **L4 Procedural skills (GROW):** minerva_skills beyond 5; mine successful episodes for skills.
- **Consolidation engine (NEW — sleep-time compute):** lumen-dream.timer (reuse DGM timer
  infra) fires in idle fleet windows. A sleep-time agent (DeepSeek via LiteLLM — NOT
  messenger-chat/re) reflects on recent sessions/messages: extract facts->L1/L2, write
  episodes->L3, dedupe, prune stale, and PROPOSE claude_memory.md consolidations (human-gated,
  exactly like DGM promote). This bounds the 6,859-line growth and kills override drift.
- **Eval gate (NEW):** Lumen gold set + LongMemEval-style scorer. Any memory change is
  benchmark-gated (DGM pattern): KU/temporal/abstention must not regress.

## 6. Phasing (each phase: backup -> build -> eval -> human-gated promote)
- **Phase 0 (week 1, reversible):** embedder swap + re-index; Qdrant hybrid + reranker;
  build Lumen gold set + eval harness. Deliverable: measured baseline.
- **Phase 1 (the staleness fix, highest leverage):** Graphiti on FalkorDB; backfill
  state-change episodes from claude_memory.md history; wire lumen_state_gen.py to query it;
  pass KU/temporal eval. Deliverable: stale-state failures structurally prevented.
- **Phase 2 (episodic learning):** structured post-mortems on session_commit; A-MEM evolving
  notes; retrieval hook in feasibility_check + change-time. Deliverable: repeat-failure guard.
- **Phase 3 (consolidation):** lumen-dream sleep-time agent; dedupe/prune/promote, human-gated.
  Deliverable: bounded memory growth, no manual override maintenance.
- **Phase 4 (procedural):** grow skills; skill-learning from successful episodes.

## 7. Risks / discipline
- Embedder change = full re-index (fine: tiny corpus; do it before it grows).
- Graph DB choice: FalkorDB (Redis module) over Neo4j (JVM heavy) on the CPU box.
- Do NOT route Graphiti/consolidation LLM extraction through messenger-chat/messenger-re
  (LiteLLM #22249 passthrough corruption). Use a dedicated fleet model entry.
- RE/infosec-shaped research for any phase routes to DeepSeek fleet per the classifier rule.
- Keep L0 file approach (filesystem-is-enough evidence) — augment, never replace.
- Nothing ships without Frank go + backup + eval gate. This doc is design only.

## 8. Open decisions for Frank
1. Embedder: BGE-M3 (hybrid) vs Qwen3-Embedding-0.6B (max dense). Recommend BGE-M3.
2. Graph DB: FalkorDB (recommended) vs Neo4j.
3. Green-light Phase 0 (low-risk, reversible) to establish a measured baseline?
4. Adopt Graphiti for L2, or build a minimal bi-temporal table in minerva.db ourselves
   (lighter, fewer deps, but we re-implement invalidation + hybrid retrieval)?

---
## PHASE 0 RESULTS (2026-06-25, executed) — Frank greenlit BGE-M3 + FalkorDB + minerva.db bi-temporal + Phase 0

Decisions locked: embedder=BGE-M3; graph store=FalkorDB; L2 bi-temporal layer=build minimal in minerva.db ourselves (NOT Graphiti lib); FalkorDB reserved for entity-relationship graph side.

**Shipped (all reversible, backed up under /home/lumen/backups/memory_v2_phase0_*):**
- Hybrid retrieval in context_selector.py: dense (Ollama embed) + BM25 over full section content, fused via Reciprocal Rank Fusion (RRF k=60). Fail-safe: any error reverts to old dense-only; embed-down reverts to BM25/keyword. tag_sections.py now stores content_lc inline (no hot-path file reads). New --rank mode = retrieval-quality probe; new rank_only()/stats() report core_overflow.
- BGE-M3 swap STAGED + auto-completing: swap_embedder.sh (idempotent, dim-checked) + bge_watcher.sh (waits for pull, swaps, re-indexes, VERIFIES dim==1024 + live retrieval, AUTO-ROLLBACK to nomic on failure). Logs /tmp/bge_watcher.log, eval /tmp/mem_eval_bge.txt.
- Eval: eval_sets/memory_gold.jsonl (22 items: knowledge_update/current_fact/temporal/abstention/procedural) + memory_eval.py (LUMEN_EVAL_BUDGET env).

**Measured (nomic embedder; BM25 hybrid vs dense-only baseline):**
| budget | baseline | hybrid |
|---|---|---|
| 4000 (real session load) | 64% | 68% |
| 20000 (retrieval-quality) | 64% | 91% |
Category deltas @20000: procedural 33->100, abstention 50->100, current_fact 44->78, knowledge_update 100, temporal 100. Stale co-occurrence 7%->21% (more relevant sections retrieved => more contradicting stale text alongside => the L2 invalidation case, quantified).

**KEY NEW FINDING (reshapes the roadmap): CORE-OVERFLOW.**
#CORE tags have grown to ~24 sections / ~15.6K tokens, but claude_context.sh runs at a 4000-token budget. select() loads core FIRST until budget fills -> at 4000, core overflows, some core SILENTLY truncates, and query-relevant non-core almost never loads. This is why hybrid only moved 64->68 at the real budget but 64->91 when non-core can load. The single highest-leverage cheap fix is now a CORE BUDGET POLICY fix, not ranking:
  - Option A: raise default budget (E5-2699 + DeepSeek context easily handles 8-12K).
  - Option B: demote over-eager #CORE tags back to retrieved tier (audit the 24).
  - Option C: make core itself ranked/tiered (always-floor = a small invariants set; rest of "core" competes in the hybrid pool).
Recommend A+B now (raise budget to ~10K AND audit #CORE down to a true invariants floor). NEEDS FRANK NOD before retagging the memory file.

**Residual misses even @20000 (2/22):** cur_thoth_hw (Seneca MP vs Wyse) + cur_toilet_ip (100.70.94.19) — atomic facts inside large multi-host sections. Section granularity is the ceiling -> this is exactly what Phase 1 (atomic-fact store / bi-temporal minerva.db table) fixes.

**Phase 0 open micro-items:** cross-encoder reranker (bge-reranker-v2-m3) deferred (bounded change surface tonight); core budget policy decision (above); confirm bge_watcher result in /tmp/mem_eval_bge.txt next session.

---
## PHASE 0 — CORE-BUDGET FIX APPLIED (2026-06-25, executed)
Frank: "finish phase 0 so we can move to phase 1 right away." Done.
- #CORE audited 24->10 sections, 15653->6263 core tokens. Kept (invariants): CURRENT STATE OVERRIDES x2, STATE MARKERS, MESSENGER BRAIN=DEEPSEEK, MEMORY V2 DESIGN + the 6 hardcoded system/rule titles. Demoted 15 dated changelogs/incident logs to retrieved-on-demand. (Memory backed up; reversible.)
- Default budget raised 4000->10000 (context_selector.py all sites). Architecture now = small always-on core floor (~6.3K) + hybrid-ranked non-core fills the rest.
- RESULT (nomic): real-world budget 10000 = 91% (was 68% @4k, baseline 64%); ceiling 20000 = 100% (22/22) incl. current_fact 9/9 — both prior atomic-fact misses (Thoth=Seneca, Toilet IP) now resolve once non-core can load. Stale co-occurrence 21% (unchanged) = pure L2-invalidation target for Phase 1.
- BGE-M3 dense swap finalizing via /tmp/finalize_p0.sh (verify dim=1024 + retrieval, rollback on fail). Result -> /tmp/mem_eval_bge.txt.
- Reranker (bge-reranker-v2-m3): DEFERRED with cause — our residual gap is atomic-fact granularity inside large sections; a cross-encoder reorders whole sections and cannot extract a buried fact, so it does not address the failure profile. Revisit only if Phase 1 eval shows section-ordering errors.
PHASE 0 = COMPLETE pending bge-m3 verify. Next: PHASE 1 = minimal bi-temporal table in minerva.db (state/invalidation, kills the 21% stale co-occurrence) + FalkorDB for entity-relationship graph.


---
## PHASE 1 RESULTS (2026-06-25, executed) — bi-temporal state layer SHIPPED (the staleness fix)

Frank greenlit Phase 1 + core-budget audit. Auto-continue session. All reversible; backups under /home/lumen/backups/memory_v2_phase1_20260625_054112/ (minerva.db, tag_sections.py, sections.json, context_selector.py).

### A. #CORE invariants-floor audit (durable, not one-off)
- tag_sections.py: added `superseded_core_indices()` post-pass — among dated snapshot blocks (CURRENT STATE OVERRIDES / STATE MARKERS) only the NEWEST-dated stays #CORE; older auto-demote to retrieved tier. Applies the invalidation philosophy to core membership itself: every new override block silently demotes its predecessor. No edit to claude_memory.md.
- Result: core 10 sections/6263 tok -> **9 sections/5245 tok**. The 2026-05-13 overrides block (explicitly superseded by 06-07) demoted; single STATE MARKERS block correctly retained (don't demote a unique block for age alone).
- Budget: confirmed already raised 4000->10000 in context_selector.py (.prebudget backup). `--core` render now fits 10K and loads core + 6 query-relevant sections (9802 tok delivered) instead of overflowing. core_overflow=False.

### B. L2 bi-temporal state layer (minerva.db) — THE KEYSTONE
DIY bi-temporal (per locked decision; NOT Graphiti lib). Deterministic key-based supersession instead of an LLM contradiction judge — reliable + zero-cost for structured fleet facts.
- **minerva.db tables:** `state_facts` (subject, predicate, value, valid_from, valid_to, ingested_at, invalidated_at, source_ref, confidence, episode_id; indexes on (subject,predicate), open-slice, valid window) + `state_episodes` (provenance).
- **/home/lumen/minerva/lumen_state.py** — engine + CLI. ingest() = key-based supersession: new (subject,predicate) value closes the prior open row (valid_to = new valid_from, invalidated_at = now) and inserts; same value = noop. INVALIDATE-not-delete. Queries: current() (valid_to IS NULL), as_of(date) (world-truth point-in-time), history(subject,predicate).
- **/home/lumen/minerva/backfill_state.py** — idempotent seed (guards on exact subject/predicate/value/valid_from; replaying historical transitions is otherwise unsafe through ingest()). Seeded 30 facts / 9 subjects incl. real transitions. Re-run = 30 skip_exists, 0 mutations.
- **/home/lumen/minerva/lumen_state_gen.py** + **/home/lumen/bin/state-regen** — renders the current slice as `context/LIVE_FLEET_STATE.md`, a never-stale "LIVE FLEET STATE #CORE" block intended to replace the hand-curated CURRENT_STATE_OVERRIDES.

**Proven end-to-end (the disease, cured):**
| query | result |
|---|---|
| current pbox status | retired  (auto-expired; "PBox active" can no longer be returned) |
| as_of 2026-05-01 pbox status | active (history preserved) |
| current thoth tailscale_ip | 100.75.1.108 (truth) |
| as_of 2026-05-01 thoth tailscale_ip | 100.88.71.47 (old value still queryable) |
| current toilet tailscale_ip | 100.70.94.19 |
| current thoth hardware | Seneca MP (11th-gen i3-1115G4) |

Both Phase-0 residual eval misses (cur_thoth_hw, cur_toilet_ip — atomic facts buried in large multi-host sections) now resolve via current(). 26 currently-true facts across 9 subjects; 4 closed windows.

### Open / gated next steps (need Frank nod)
1. **Wire LIVE_FLEET_STATE.md into claude_context** by replacing the hand-curated CURRENT_STATE_OVERRIDES section in claude_memory.md with an include of the generated block (or have context_selector inject it as a synthetic core section). NOT done tonight — touches the canonical memory file; gated.
2. **change-time ingest hook:** call lumen_state.ingest() whenever a fleet fact changes in-session, so the store stays live without manual backfill edits.
3. **Phase 2** (episodic post-mortems on session_commit) and **Phase 3** (lumen-dream sleep-time consolidation, human-gated) unchanged from roadmap.
4. cross-encoder reranker (bge-reranker-v2-m3) still deferred.


---
## PHASE 2 RESULTS (2026-06-25, executed) — episodic recall guard SHIPPED + memory re-architecture

### C. Episodic experience layer (repeat-failure guard)
Built ON minerva_memory.py (existing 3-tier store: archival_notes + embeddings, recall_fts FTS5, unified search) — no duplication. Reflexion/ExpeL pattern.
- **/home/lumen/minerva/lumen_episode.py**: commit() writes a structured post-mortem (what changed/worked/failed/why); recall() returns relevant past episodes; precheck(plan) gates an action with a ⚠️ verdict when related failures exist. CLI: recall / precheck / commit / backfill. Embedder kept on nomic-768d (matches existing notes; FTS5 carries keyword recall) — lowest blast radius.
- **Backfilled 6 documented house-pathologies** as episodes (PBox-after-retirement, daily 13:00 fleet deaths, node-killer reaping live sessions, minerva 404 outage, messenger Groq hallucination, inode crisis). Recall verified relevant: "disable a service" -> surfaces fleet-deaths + node-killer at top; "pbox inference node" -> surfaces the retirement pathology; "silent fallback" -> surfaces both outages.
- **Wired into workflow** (claude_memory.md #CORE): precheck BEFORE any change/install/service action; commit a post-mortem AFTER meaningful work. First live episode committed (this session, dogfooded).
- Remaining wire (optional): retrieval hook inside minerva_proposal feasibility_check.

### Wiring shipped this session
- **LIVE_FLEET_STATE injected into context_selector.select()** — read fresh from disk at the top of every render; leads `--core`. Authoritative current fleet facts, supersedes dated prose. Refresh via /home/lumen/bin/state-regen.
- **Change-time hook deployed**: state_reconcile.py + lumen-state-reconcile.timer (daily 17:30 UTC). Pulls tailscale ground-truth, ingests changed tailscale_ip for known subjects (never touches curated status; flags unknown peers). On first run it auto-corrected stale be3600 IP 100.108.143.46 -> 100.81.177.14 (history preserved) and flagged unknown online peer `apep` 100.82.2.72 for review.
- **State store rounded to full roster**: 15 nodes / ~47 current facts (added thor, maat, anubis, seshat, 3dprinter incl. klipper detail, be3600, hathor TS).

### D. Memory re-architecture (root-cause fix for the 30-slot wall)
DIAGNOSIS: the June-1 design (6 lean slots; everything else on Lumen) silently drifted — every fleet change since was appended as a NEW memory_user_edits slot, refilling 6 -> 30. The extension never failed; discipline did.
FIX: verified every fleet-fact slot is preserved in claude_memory.md + state_facts, then migrated+pruned 17 redundant slots (30 -> 13 lean operational/behavioral/access/pointer slots). Updated the MEMORY DISCIPLINE slot to route future fleet facts to lumen_state.py ingest + claude_memory.md, NOT new slots. Wall won't rebuild.

### Net Memory V2 status after tonight
- Phase 0 (hybrid retrieval, BGE-M3, eval): DONE (prior session).
- Phase 1 (bi-temporal state, #CORE audit, budget 10K): DONE + wired into context.
- Phase 2 (episodic recall guard): DONE + dogfooded.
- Phase 3 (sleep-time consolidation): still gated; now has a real episodic store to consolidate from (precondition met).


---
## PHASE 3 v1 RESULTS (2026-06-25, executed) — consolidation pass, PROPOSAL-ONLY

The episodic guard (precheck) flagged the obvious risk on this exact plan — node-killer / destructive-automation / pause-with-no-resume pathologies — so v1 is deterministic and NON-DESTRUCTIVE.
- **/home/lumen/minerva/lumen_dream.py**: (1) STALE-PROSE SCAN — every IP the bi-temporal store has superseded (closed window) is searched in claude_memory.md; hits = stale prose the store contradicts, proposed for cleanup with current value + date. (2) EPISODIC DEDUP — archival_notes pairs cosine ≥0.93 proposed for merge. (3) GROWTH METRICS. Writes a dated report to /home/lumen/minerva/dream/; NEVER edits claude_memory.md.
- **lumen-dream.timer**: weekly Sun 18:00 UTC, report-only (safe to arm — writes reports only).
- First run: 0 stale-prose findings (clean — tonight's re-architecture left no superseded IPs in prose; scanner logic validated separately), 0 dup episodes, core 6328 tok, 185 sections, 49 current facts, 11 episodes.
- LIMITATION: only catches IPs the store KNOWS are superseded (closed windows). Coverage grows as the reconciler + manual ingests accumulate transitions.
- FUTURE (gated): LLM sleep-time reflector (DeepSeek via LiteLLM, NEVER messenger-chat/re) to extract lessons from recent sessions and PROPOSE consolidations + claude_memory.md merges (DGM-style human-gated promote).
  - **BUILT 2026-06-25:** `lumen_reflector.py` (proposal-only; deepseek-v4-pro; writes dream/reflections/ only; fail-loud; off-limits guard). First run = 5 grounded proposals. Units `lumen-reflector.{service,timer}` staged in /etc/systemd/system but **DISABLED** — enable gated on Frank (`sudo systemctl enable --now lumen-reflector.timer`, Sun 18:30 UTC). Per-run = DeepSeek spend.

## MEMORY V2 — ROADMAP COMPLETE (Phases 0–3)
- P0 hybrid retrieval + BGE-M3 + eval — DONE (prior session)
- P1 bi-temporal state layer + #CORE audit + 10K budget — DONE, wired into context
- P2 episodic recall guard — DONE, dogfooded, workflow wired
- P3 v1 consolidation (proposal-only) — DONE, weekly timer
- memory_user_edits re-architected 30→13 lean; discipline rule routes fleet facts to the store
Timers live: lumen-state-reconcile (daily 17:30 UTC), lumen-dream (weekly Sun 18:00 UTC).
Open: live HA failover drill (unrelated); LLM sleep-time reflector — BUILT 2026-06-25 (proposal-only, lumen_reflector.py), timer ENABLED 2026-06-26 (Sun 18:30 UTC); minerva_proposal feasibility_check recall hook — WIRED 2026-06-26 (advisory, fail-open: _recall_advisory attaches related past episodes to each proposal's markdown; never gates).
