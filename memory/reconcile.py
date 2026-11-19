#!/usr/bin/env python3
"""reconcile.py — the trusted-reconciler side of fleet_memory (wq#3647).

memory.propose_fact writes status='proposed'. A trusted operator reviews and
promotes to 'verified' (or rejects). Every action appends a memory_events row.

Usage:
  reconcile.py list                         # show all proposed facts
  reconcile.py promote <id> [--by NAME]     # proposed -> verified
  reconcile.py reject  <id> [--reason TEXT] # proposed -> rejected
"""
import argparse
import sys

import fm_db


def _row(cur, fid):
    cur.execute("""SELECT subject,attribute,value,status FROM fleet_facts WHERE id=%s""", (fid,))
    return cur.fetchone()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("promote"); p.add_argument("id", type=int); p.add_argument("--by", default="reconciler")
    r = sub.add_parser("reject"); r.add_argument("id", type=int); r.add_argument("--reason", default="rejected by reconciler")
    args = ap.parse_args()

    conn = fm_db.connect()
    cur = conn.cursor()
    if args.cmd == "list":
        cur.execute("""SELECT id,subject,attribute,left(value,60),confidence,created_by
                       FROM fleet_facts WHERE status='proposed' ORDER BY id""")
        rows = cur.fetchall()
        for i, s, a, v, c, cb in rows:
            print("#%d  %s/%s = %s  (conf %.2f, by %s)" % (i, s, a, v, c or 0, cb))
        print("%d proposed fact(s)" % len(rows))
        return

    before = _row(cur, args.id)
    if not before or before[3] != "proposed":
        sys.exit("fact #%d is not in 'proposed' state" % args.id)
    if args.cmd == "promote":
        cur.execute("""UPDATE fleet_facts SET status='verified', last_verified=now()
                       WHERE id=%s""", (args.id,))
        fm_db.log_event(cur, args.by, "promote", "fleet_fact", args.id,
                        {"status": "proposed"}, {"status": "verified"},
                        "reconciler promotion")
        print("promoted #%d -> verified" % args.id)
    else:
        cur.execute("""UPDATE fleet_facts SET status='rejected' WHERE id=%s""", (args.id,))
        fm_db.log_event(cur, "reconciler", "reject", "fleet_fact", args.id,
                        {"status": "proposed"}, {"status": "rejected"}, args.reason)
        print("rejected #%d" % args.id)
    conn.commit()


if __name__ == "__main__":
    main()
