#!/usr/bin/env python3
"""
Scratch retention sweeper. Applies tier-based age rules from POLICY.md.

Modes:
  --dry-run     Print what would be deleted, delete nothing (DEFAULT)
  --apply       Actually delete. Requires explicit flag. Aborts if >MAX_DELETE items.

Safety rails:
- Aborts if it would delete more than MAX_DELETE files (probably a misclassification)
- Skips any file with mtime < 24h (too new to judge)
- Skips anything in VIP paths (path-prefix match)
- Logs every decision to /home/lumen/minerva/retention/logs/YYYY-MM-DD.jsonl
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

SCRATCH = Path("/home/lumen/scratch")
LOGS = Path("/home/lumen/minerva/retention/logs")
LOGS.mkdir(parents=True, exist_ok=True)

MAX_DELETE = 3000

# tier -> TTL in seconds
TTL = {
    "vip": None,            # keep forever
    "normal": 30 * 86400,   # 30 days
    "ephemeral": 7 * 86400, # 7 days
}

NEW_FILE_GRACE = 86400      # 24h

# Rule order matters: first match wins. Prefix checks against path relative to SCRATCH.
RULES = [
    # === Tier 1: KEEP FOREVER (VIP) ===
    ("vip", ("research/synthesis/",)),
    ("vip", ("research/briefs/",)),
    ("vip", ("strategy/",)),
    ("vip", ("samsung_helpers/",)),
    ("vip", ("reports/",)),
    ("vip", ("reddit_submissions/",)),

    # file prefix patterns (parent path is scratch/research or scratch/ directly)
    ("vip", ("research/!aa_",)),
    ("vip", ("research/000_VIP_",)),
    ("vip", ("SESSION_HANDOFF_",)),
    ("vip", ("CAMPAIGN_",)),
    ("vip", ("WINFORGE_",)),
    ("vip", ("PIVOT_RESEARCH_",)),

    # === Tier 3: EPHEMERAL (7 days) — checked BEFORE 'normal' so drafts fall here ===
    ("ephemeral", ("drafts/",)),
    ("ephemeral", ("research/quarantine/",)),
    ("ephemeral", ("working/",)),

    # === Tier 2: NORMAL (30 days) ===
    ("vip", ("research/00_VIP_",)),           # 00_VIP_ is also VIP-promoted (handled before normal)
    ("normal", ("research/pe_",)),
    ("normal", ("research/aa_pe_",)),
    ("normal", ("research/00_PRIORITY_",)),   # PRIORITY-promoted fleet output
    ("normal", ("research/00_gap_",)),        # scout gap-analysis output
    ("normal", ("research/intel_",)),          # lumen-intel feed output
    ("normal", ("research/2026-",)),           # dated research outputs
    ("normal", ("research_prompts_done/",)),
    ("normal", ("deep_research_output/",)),
    ("normal", ("github_findings/",)),

    # === Untouchable: live queue ===
    ("skip", ("research_prompts/",)),
]


def classify(relpath_str):
    """Return (tier, matched_prefix) for a path relative to SCRATCH."""
    for tier, prefixes in RULES:
        for p in prefixes:
            if relpath_str.startswith(p):
                return tier, p
    return None, None   # unclassified — keep forever by default (safe)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Actually delete. Default is dry-run.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    now = time.time()
    log_path = LOGS / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.jsonl"

    candidates = []  # list of (relpath, tier, age_days, mtime)
    skipped = {"vip": 0, "too_new": 0, "skip_live_queue": 0, "unclassified": 0}

    for root, dirs, files in os.walk(SCRATCH):
        for f in files:
            full = Path(root) / f
            try:
                rel = str(full.relative_to(SCRATCH))
                st = full.stat()
            except (OSError, ValueError):
                continue

            tier, _ = classify(rel)

            if tier == "vip":
                skipped["vip"] += 1
                continue
            if tier == "skip":
                skipped["skip_live_queue"] += 1
                continue
            if tier is None:
                skipped["unclassified"] += 1
                continue
            # normal / ephemeral
            age = now - st.st_mtime
            if age < NEW_FILE_GRACE:
                skipped["too_new"] += 1
                continue
            ttl = TTL[tier]
            if ttl is None or age <= ttl:
                continue
            candidates.append((full, rel, tier, age / 86400, st.st_mtime))

    print(f"=== Scratch retention sweep ({'APPLY' if args.apply else 'DRY-RUN'}) ===")
    print(f"Total files walked.  Skipped: VIP={skipped['vip']}, too_new={skipped['too_new']}, "
          f"live_queue={skipped['skip_live_queue']}, unclassified={skipped['unclassified']}")
    print(f"Deletion candidates: {len(candidates)}")

    if len(candidates) > MAX_DELETE:
        print(f"ABORT: would delete {len(candidates)} files, which exceeds MAX_DELETE={MAX_DELETE}.")
        print("Review the candidate list manually before raising the limit.")
        return 2

    if not candidates:
        print("Nothing to do.")
        return 0

    by_tier = {}
    for _, rel, tier, age, _ in candidates:
        by_tier.setdefault(tier, []).append((age, rel))
    for tier, rows in sorted(by_tier.items()):
        print(f"\n-- tier={tier} ({len(rows)} files) --")
        for age, rel in sorted(rows, reverse=True)[:5]:
            print(f"  {age:6.1f}d  {rel}")
        if len(rows) > 5:
            print(f"  ... and {len(rows) - 5} more")

    if not args.apply:
        print("\nDRY-RUN: no deletions performed. Re-run with --apply to delete.")
        return 0

    # Apply: delete, log each
    deleted = 0
    with log_path.open("a") as logf:
        for full, rel, tier, age, mtime in candidates:
            try:
                full.unlink()
                deleted += 1
                logf.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "path": rel,
                    "tier": tier,
                    "age_days": round(age, 2),
                    "mtime": mtime,
                }) + "\n")
            except OSError as e:
                if args.verbose:
                    print(f"  WARN: failed to delete {rel}: {e}")

    print(f"\nDeleted {deleted} files. Log: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
