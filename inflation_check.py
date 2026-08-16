#!/usr/bin/env python3
"""inflation_check.py — warning-only reward/impact-inflation report.
(Minerva proposal ac353883cea7, wq#459, Scribe #320.)

PREMISE CORRECTIONS: the proposal named quantities that do not exist in the
pipeline ("estimated memory-retention benefit", a lumen-dream post-hoc
proxy). What DOES exist and is checkable: proposals self-rate impact
(S/M/L/XL) at generation time, and the verify gate independently scores
their factual claims (verification_log.final_score, 2170 rows). The
inflation pattern: HIGH self-claimed impact + WEAK independent verification
= the proposal class most likely to be overselling itself (the same class
that dominated the 57 manual rejections in the 2026-08-09 YELLOW triage).

Strictly WARNING-ONLY as mandated: standalone report, zero edits to
minerva_eval or any live service, no gate behavior change, no timer.
Callable manually or by factory triage:
  inflation_check.py [--threshold 0.5] [--status deploy_ready,planned,pending]
Output: table to stdout + JSON at reports/inflation_check_latest.json.
"""
import argparse, datetime, json, os, sqlite3

DB = "/home/lumen/minerva/minerva.db"
OUT = "/home/lumen/minerva/reports/inflation_check_latest.json"

def latest_verify(c, title):
    row = c.execute(
        "SELECT final_score, citation_score, judge_score FROM verification_log "
        "WHERE subject = ? ORDER BY id DESC LIMIT 1",
        (f"proposal:{title[:200]}"[:250],)).fetchone()
    if row:
        return row
    # fallback: prefix match (subject truncation varies)
    return c.execute(
        "SELECT final_score, citation_score, judge_score FROM verification_log "
        "WHERE subject LIKE ? ORDER BY id DESC LIMIT 1",
        (f"proposal:{title[:60]}%",)).fetchone()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.5,
                    help="verify final_score below this + impact L/XL = flag")
    ap.add_argument("--status", default="pending,accepted,deploy_ready,planned")
    args = ap.parse_args()
    statuses = tuple(s.strip() for s in args.status.split(","))
    c = sqlite3.connect(DB)
    q = ("SELECT id, title, impact, status FROM proposals WHERE impact IN "
         f"('L','XL') AND status IN ({','.join('?' * len(statuses))}) "
         "ORDER BY created_at DESC")
    flags = []
    for pid, title, impact, status in c.execute(q, statuses):
        v = latest_verify(c, title or "")
        if v is None:
            flags.append({"id": pid, "impact": impact, "status": status,
                          "verify": None, "reason": "no verification record",
                          "title": (title or "")[:80]})
        elif v[0] is not None and v[0] < args.threshold:
            flags.append({"id": pid, "impact": impact, "status": status,
                          "verify": round(v[0], 2),
                          "reason": f"claimed {impact} impact, verify "
                                    f"{v[0]:.2f} < {args.threshold}",
                          "title": (title or "")[:80]})
    for f in flags:
        print(f"[{f['id']}] {f['impact']}/{f['status']} verify="
              f"{f['verify']} — {f['title']}")
    print(f"\n{len(flags)} inflation-risk proposal(s) "
          f"(threshold {args.threshold}, statuses {statuses})")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump({"ts": datetime.datetime.now(
                       datetime.timezone.utc).isoformat(),
                   "threshold": args.threshold, "statuses": statuses,
                   "flags": flags, "warning_only": True}, f, indent=2)

if __name__ == "__main__":
    main()
