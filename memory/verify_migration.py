#!/usr/bin/env python3
"""verify_migration.py — prove the DB-generated candidate covers the live file.
Checks: every node, every PERMANENT/STANDING/FRANK bullet, and no secrets.
Exit 0 = all green, 1 = gaps found.
"""
import re
import sys

import fm_db

LIVE = "/home/lumen/claude_memory.md"
CAND = "/home/lumen/scratch/subtraction/memory-consolidation/claude_memory.candidate.md"

SECRET = re.compile(r"(sk-[A-Za-z0-9]{12}|password\s*[:=]\s*\S|BEGIN (?:RSA|OPENSSH) PRIVATE|"
                    r"api[_-]?key\s*[:=]\s*\S|cfat_[A-Za-z0-9]{8}|github_pat_)", re.I)


def nfull(s):
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def norm(s):
    return nfull(s)[:40]


def live_bullets(text, section_prefix):
    out, grab = [], False
    for ln in text.splitlines():
        if ln.startswith("## "):
            grab = ln[3:].upper().startswith(section_prefix)
            continue
        if grab:
            s = ln.strip()
            m = re.match(r"^(?:[-*]|\d+\.)\s+(.*)$", s)
            if m and len(m.group(1)) > 6:
                out.append(m.group(1))
    return out


def main():
    live = open(LIVE, encoding="utf-8", errors="replace").read()
    cand = open(CAND, encoding="utf-8", errors="replace").read()
    cand_norm = nfull(cand)
    gaps = 0

    # 1. node coverage (from DB roster)
    conn = fm_db.connect()
    cur = conn.cursor()
    cur.execute("SELECT hostname FROM fleet_devices ORDER BY hostname")
    hosts = [r[0] for r in cur.fetchall()]
    missing_hosts = [h for h in hosts if h.replace("-", "") not in cand_norm
                     and h not in cand]
    print("NODES: %d in DB, %d missing from candidate" % (len(hosts), len(missing_hosts)))
    if missing_hosts:
        print("  MISSING:", missing_hosts); gaps += len(missing_hosts)

    # 2. decision coverage per source section
    for pref, label in (("PERMANENT DECISIONS", "permanent"),
                        ("STANDING RULES", "standing"),
                        ("FRANK CORRECTIONS", "frank")):
        bl = live_bullets(live, pref)
        miss = [b for b in bl if norm(b) not in cand_norm]
        print("%s: %d bullets in live, %d not found in candidate" %
              (label, len(bl), len(miss)))
        for b in miss:
            print("   MISS:", b[:80]); gaps += 1

    # 3. secret scan on candidate
    hits = SECRET.findall(cand)
    print("SECRET SCAN on candidate: %d hits" % len(hits))
    if hits:
        print("   ", hits[:5]); gaps += len(hits)

    conn.close()
    print("\n%s" % ("ALL GREEN" if gaps == 0 else "GAPS: %d" % gaps))
    sys.exit(0 if gaps == 0 else 1)


if __name__ == "__main__":
    main()
