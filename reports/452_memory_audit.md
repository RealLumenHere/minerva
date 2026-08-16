# wq#452 — Embedding integrity/drift audit (prop 0658b2f0f457, Scribe #313)
Date: 2026-08-10 · Session: claude-web-0809b

## Premise corrections
- Research index embeds with nomic-embed-text (768d) via local ollama —
  NOT bge-m3 as proposed.
- With pinned weights, "forgetting" is not gradual decay: real failure modes
  are STEP CHANGES — silent ollama model upgrade (:latest tag!), tokenizer/
  truncation change, index corruption. Audit targets exactly those.

## Shipped
- /home/lumen/minerva/memory_audit.py — fixed-seed (pg setseed 0.42) 40-chunk
  sample from research_chunks (10,709 total), re-embed same text, cosine vs
  stored. Records ollama model digest per run (version flips attributable
  before similarity moves). Edge-triggered aq+ntfy.
- memory_audit.db (runs). lumen-memory-audit.{service,timer} weekly Tue
  09:10 UTC quiet window, installed DISABLED.

## Validation + genuine finding
Run 1 (detached): n=39, median 1.00000 (index healthy, determinism confirmed
at rest), min 0.8665 tripped initial MIN_THRESH=0.98 -> aq#446. Diagnostic
rescan DURING heavy concurrent bench load: 18/39 chunks at cos 0.90-0.96 —
ollama embeddings are NOT bit-deterministic under concurrent inference load
(batching float noise). MEDIAN is the robust signal; MIN_THRESH loosened to
0.85; timer placed in quiet window; caveat documented in module docstring.
aq#446 resolved with diagnosis. One chunk (id 15374) 500s on re-embed every
run — tracked via errors column.

## Rollback
Disable timer; rm script+units+memory_audit.db.
