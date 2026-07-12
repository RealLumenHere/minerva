# Scratch Retention Policy — v1 (2026-04-22)

## Observed state
- 32,781 files / 322MB in /home/lumen/scratch (99.8% <30 days old — this is active volume, not stale cruft)
- 22,111 files at /home/lumen/scratch/research/ top level (raw fleet outputs, one per prompt × fleet-worker duplicates)
- 7,214 files in drafts/ (Ghost blog drafts, many published-and-forgotten)
- 9 syntheses + 5 briefs in research/synthesis/ + research/briefs/ (human-curated, must keep)
- 1,137 files in research/quarantine/ (flagged bad outputs)

## The actual problem
No index. No way to say 'did we already research X' without linear scan. Volume grows ~1,000 files/day while prompt-engine keeps queue above 20.

## Three-tier classification

### Tier 1: KEEP FOREVER (VIP)
- /home/lumen/scratch/research/synthesis/*          (Claude-curated distillations)
- /home/lumen/scratch/research/briefs/*             (Frank-directed research briefs)
- /home/lumen/scratch/research/!aa_*                (urgent prompts — hand-promoted)
- /home/lumen/scratch/research/000_VIP_*            (VIP campaign outputs)
- /home/lumen/scratch/strategy/*                    (strategic positioning docs)
- /home/lumen/scratch/SESSION_HANDOFF_*             (session boundaries)
- /home/lumen/scratch/CAMPAIGN_*                    (campaign synthesis deliverables)
- /home/lumen/scratch/WINFORGE_*                    (WinForge project assets)
- /home/lumen/scratch/samsung_helpers/*             (deployed Samsung dotfiles)
- /home/lumen/scratch/reports/*                     (quarterly reports)
- /home/lumen/scratch/reddit_submissions/*          (published submissions)

Selection heuristic: anything that a human or Claude referenced AFTER creation
(i.e. reachable from claude_memory.md, minerva DB, or another synthesis). Anything
with manual prefix promotion (!aa_, 000_VIP_).

### Tier 2: KEEP 30 DAYS (NORMAL)
- /home/lumen/scratch/research/pe_*                 (fleet prompt outputs — the 22K files)
- /home/lumen/scratch/research/aa_pe_*              (promoted prompts, non-urgent)
- /home/lumen/scratch/research_prompts_done/*       (processed queue items)
- /home/lumen/scratch/deep_research_output/*        (raw deep-research artifacts)
- /home/lumen/scratch/github_findings/*             (scout output)

Rationale: these are working artifacts. Claude may refer back to a fleet output
for a week or two while synthesizing. Beyond 30 days, if the output wasn't
referenced or didn't feed a synthesis, it's noise.

### Tier 3: KEEP 7 DAYS (EPHEMERAL)
- /home/lumen/scratch/drafts/*                      (Ghost blog drafts — once published, the published version on Ghost is source of truth)
- /home/lumen/scratch/research/quarantine/*         (already flagged as bad)
- /home/lumen/scratch/working/*                     (scratch-scratch)

## Implementation (next session)

1. Before ANY deletion, build index of what currently exists in each tier
   (Minerva's postgres-mcp instance, once live, hosts this inventory)
2. Run a NON-DESTRUCTIVE pass that only PRINTS what would be deleted. Have
   Frank eyeball a sample before first real run.
3. Retention sweep is a weekly timer (Sun 04:00 UTC, well outside research
   hours). Never runs interactively.
4. Every deletion logs file path + reason + tier to
   /home/lumen/minerva/retention/logs/YYYY-MM-DD.jsonl
5. SAFETY RAIL: sweep aborts if it would delete more than 3000 files in one
   run — probably a miscategorized directory, get a human to check.

## Exceptions / don't touch
- /home/lumen/scratch/research_prompts/*  — the LIVE queue. Prompt-engine
  reads this. Never delete files here; prompt-engine handles its own consumption.
- Anything with mtime < 24h — too new to know if it's referenced yet.

## Not in scope for v1
- Semantic 'was this file referenced?' check. v1 is path-prefix + age.
- Compression of deleted files. Disk is not scarce (51% used). If it becomes
  scarce, add gzip archive to /home/lumen/scratch/archive/ before delete.
- Cross-linking to Minerva DB. Once postgres-mcp is heavily used, build a
  'scratch_files' table and join retention decisions to whether a file's
  path appears in any Minerva proposal / synthesis / brief.

## First-run estimate
With 32,781 files total and 99.8% <30 days, the first retention sweep would
delete essentially nothing (a handful of drafts older than 7 days, maybe 50
fleet outputs older than 30 days). This matches the observation that the
problem isn't DEAD weight — it's LIVE volume without discoverability.

The retention policy is preventive: once in place, research/ stops growing
unboundedly. The real discoverability fix is postgres-mcp + an indexer, which
is now unblocked (postgres-mcp is live as of 2026-04-22).
