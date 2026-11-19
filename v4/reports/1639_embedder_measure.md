# wq#1639 - one embedder: nomic-embed-text vs bge-m3 for section retrieval

Run: 2026-10-12 (sweep-s). Method: scratch copies only (`/home/lumen/scratch/sweep-s/w1639/ab.py`, `results.json`); no write to live `context/sections.json`, `minerva.db`, or `search_v3`.

## Premise check (the ticket setup no longer exists)
- The ticket was written against 279 sections / 29 core (11K tok). After wq#3647 (claude_memory.md generated from PostgreSQL fleet_memory) `context/sections.json` holds **7 sections, 0 core**, all PG-generated. The "29 core + 20 sampled" probe set is gone.
- `claude_context.sh --core` still calls `context_selector.py`, so the section embedder (bge-m3) is still on the boot path.
- The federated index is no longer dead: `federated_files`=4 (indexed 2026-10-12 07:46Z), `federated_chunks`=68; `MEMORY_V4_FEDERATED` still unset.

## What was measured
Arm A = bge-m3 (current, 1024d). Arm B = nomic-embed-text (768d). Same protocol as `tag_sections.py` (embed `"{title}. {content[:2000]}"`; fresh bge-m3 vectors matched the stored ones, cos min 1.0000). Probes = `eval_sets/memory_gold.jsonl` (22 items); an item is scorable only if its expected-fact string exists in a live section: **13 of 22** (9 reference facts that are gone from the regenerated memory: ku_pbox, ku_ghost, ku_gpu, ku_pause, cur_quota_store, cur_embedder, abs_sobek, abs_v100, cur_mac_gotcha). Scoring uses the selector's own `_hybrid_rank` (dense + BM25, RRF).

| | bge-m3 | nomic |
|---|---|---|
| hybrid R@1 / R@3 / MRR | 0.769 / 1.000 / 0.885 | 0.846 / 1.000 / 0.897 |
| dense-only R@1 / R@3 / MRR | 0.769 / 1.000 / 0.872 | 0.538 / 1.000 / 0.718 |
| relevant section eligible at min_sim 0.35 | 1.000 | 1.000 |
| avg sections eligible (of 7) | 6.6 | 7.0 |
| query embed p50 | 36 ms | 20 ms |
| section embed p50 | 1214 ms | 326 ms |
| trap@1 | not scorable (0 stale-trap strings left in live sections) | same |

## Reading
- With 7 sections and a 20K-token budget, nearly every section is selected whichever embedder ranks them, so the choice barely changes what is injected. The R@1 gap (0.769 vs 0.846) is one query out of 13; R@3 is 1.0 for both. This is a tie, not a win.
- Dense-only strongly favors bge-m3 (0.769 vs 0.538); nomic's hybrid result is carried by BM25. If the selector ever runs dense-only or with many more sections, bge-m3 is the safer section embedder.
- nomic embeds sections ~3.7x faster, and converging on it means one fewer resident model.

## Not done / why
- Arm (a) as specified (search_v3 reading sections directly, scored on golden_v4_frozen sha 8ba5afb0... headline + trap@1) needs a search_v3 code change plus indexing the sections into the archival/federated store. That is a build, not a measurement, and was not attempted (stabilization freeze; Phase 6 Minerva rebuild pending). Under the wq#595 adopt rule no embedder can be adopted without that golden_v4 run and a trap@1 check, so **no adoption is recommended from this data**.
- Probe coverage is thin (n=13) and the corpus is the regenerated 7-section file, so these numbers do not transfer to the old 279-section index.

## Recommendation
Hold. Re-run this A/B (same script) after the Phase 6 rebuild settles what the section/federated store looks like; if the store grows past ~30 sections or search_v3 absorbs sections, run the golden_v4 + trap@1 arm then.
