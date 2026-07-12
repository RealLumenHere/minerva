# Infra Proposal Feasibility Triage — #96–#103 (Leantime proj 12)
_2026-07-04, Claude. Grounded in live architecture verification, not proposal prose._

## Verified ground truth (read-only sweep)
- pgvector EXISTS: `lumen-pgvector` (pgvector/pgvector:pg16) container up; also Qdrant (mem0-qdrant :6333).
- Offline consolidation EXISTS: `lumen-dream.timer` (enabled) + `lumen-research-index.timer` (enabled, re-embed/reindex).
- Prompt engine = module `/home/lumen/agent/prompt_engine.py` (NOT a systemd svc; NO `/etc/lumen/prompt_engine.conf`). A hybrid context selector already runs (claude_context.sh, budget=10000).
- Embeddings via ollama: nomic-embed-text + bge-m3 (both pulled). `lumen-embed` service = inactive.
- Fleet orchestration EXISTS: lumen-agent + cerebras/sambanova/mistral/openrouter writers under systemd + pause/resume timers.
- All 8 are Minerva auto-generated (tag minerva-shipped = promoted to ticket via proposals-UI after 2026-07-01 loader bug; NOT built).

## Dispositions
| # | Proposal | Fit | Disposition |
|---|----------|-----|-------------|
| 96 | Data-Curation (arXiv RSS→LiteLLM→embed→pgvector) | Partial (overlaps lumen-research-index) | SPEC — only if it fills the arXiv-ingest gap; must define delta vs research-index |
| 97 | ResearchClawBench eval | Unverifiable | REJECT/HOLD — "ResearchClawBench" has no findable provenance; do not build against a phantom benchmark |
| 98 | Mixed-Retrieval RAG eval | Concept sound, spec vague | SPEC (low pri) — "the paper"/metric undefined; define metric before coupling minerva-eval gate to it |
| 99 | Offline Memory Consolidation | REDUNDANT | REJECT — lumen-dream + lumen-research-index already do nightly consolidation/re-embed/prune. Fold any missing prune step into those, don't add a 3rd service |
| 100 | Async Agent Coordination | Overlaps fleet | REJECT/HOLD — duplicates existing systemd fleet orchestration; in-memory queue = state lost on crash, adds fragility vs current durable units |
| 101 | Context Gating for Prompt Engine | Partial (premise wrong) | SPEC — real value candidate, but config path is fictional and a selector already exists; must scope as an enhancement to /home/lumen/agent/prompt_engine.py integrated with the existing hybrid selector |
| 102 | Agent Playground (clone xevrion-v2, systemd) | Security risk | REJECT — untrusted 3rd-party code as systemd svc on the PRIMARY host; low value (benchmarking). Vet provenance first (Frank call) |
| 103 | Article Research Agent (clone IlyaGrebenschikov, systemd) | Security risk + unfindable repo | REJECT — repo not findable; untrusted code as systemd svc; overlaps existing research pipeline |

## Summary for Frank (accept/reject is your call — proposals not auto-closed)
- 3 SPEC (buildable if you greenlight, scoped to reality): #96, #98, #101
- 5 REJECT/HOLD: #97 (phantom bench), #99 (dup), #100 (dup/fragile), #102 (untrusted code), #103 (untrusted+unfindable)
- Common failure mode across the batch: proposals assume services/paths that don't exist or duplicate ones that do. Recommend tightening the proposal generator's grounding against LIVE_FLEET_STATE before the next batch.
