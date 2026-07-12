#!/usr/bin/env python3
"""lumen_migrate.py — versioned schema migrations for minerva.db.

Ticket #73. Before this, every module ran its own `CREATE TABLE IF NOT EXISTS`
at import time (init_db.py, lumen_state.py, minerva_memory.py, ...). That is fine
for additive tables but has no story for *changing* an existing table (add a
column, backfill, rename) -- you either hand-edit prod or hope IF-NOT-EXISTS
covers it. This gives an ordered, recorded, idempotent path.

Convention (see MIGRATIONS.md):
  - Migrations live in /home/lumen/minerva/migrations/ as NNNN_slug.sql
    (zero-padded 4-digit id, ascending). 0000 is the baseline (current schema).
  - Each file is applied exactly once, inside a transaction, and recorded in
    the schema_migrations table. Applied files are never re-run or edited.
  - The existing per-module CREATE TABLE IF NOT EXISTS calls STAY (they bootstrap
    a fresh db). Migrations own everything from 0001 onward.

Usage:
  lumen_migrate.py status        # show applied vs pending
  lumen_migrate.py up            # apply all pending (default; backs up first)
  lumen_migrate.py up --dry-run  # print what would run, change nothing
  lumen_migrate.py new "add foo" # scaffold the next NNNN_add_foo.sql

Stdlib only. Safe to run repeatedly; a no-op when nothing is pending.
"""

import argparse
import datetime
import hashlib
import os
import re
import shutil
import sqlite3
import sys

DB_PATH = os.environ.get("MINERVA_DB", "/home/lumen/minerva/minerva.db")
MIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
BACKUP_DIR = os.path.dirname(DB_PATH)

_FNAME_RE = re.compile(r"^(\d{4})_([a-z0-9][a-z0-9_]*)\.sql$")


def _connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA foreign_keys=ON;")
    return con


def _ensure_table(con):
    con.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            id          TEXT PRIMARY KEY,
            slug        TEXT NOT NULL,
            sha256      TEXT NOT NULL,
            applied_at  TEXT NOT NULL
        )"""
    )
    con.commit()


def _discover():
    """Return sorted [(id, slug, path)] for well-named migration files."""
    if not os.path.isdir(MIG_DIR):
        return []
    out = []
    for fn in os.listdir(MIG_DIR):
        m = _FNAME_RE.match(fn)
        if m:
            out.append((m.group(1), m.group(2), os.path.join(MIG_DIR, fn)))
    out.sort(key=lambda t: t[0])
    return out


def _applied(con):
    return {row[0]: row[1] for row in con.execute(
        "SELECT id, sha256 FROM schema_migrations").fetchall()}


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _backup_db():
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(BACKUP_DIR, f"minerva.db.bak.{ts}.pre_migrate")
    shutil.copy2(DB_PATH, dst)
    return dst


def cmd_status(_args):
    con = _connect()
    _ensure_table(con)
    applied = _applied(con)
    migs = _discover()
    print(f"db: {DB_PATH}")
    print(f"migrations dir: {MIG_DIR}")
    if not migs:
        print("no migration files found.")
    pending = 0
    for mid, slug, path in migs:
        if mid in applied:
            drift = "" if applied[mid] == _sha(path) else "  ** CHANGED SINCE APPLIED **"
            print(f"  [x] {mid}_{slug}{drift}")
        else:
            print(f"  [ ] {mid}_{slug}  (pending)")
            pending += 1
    print(f"applied={len(applied)} pending={pending}")
    con.close()
    return 0


def cmd_up(args):
    con = _connect()
    _ensure_table(con)
    applied = _applied(con)
    migs = _discover()
    todo = [(mid, slug, path) for mid, slug, path in migs if mid not in applied]

    for mid, slug, path in migs:
        if mid in applied and applied[mid] != _sha(path):
            print(f"ERROR: {mid}_{slug} was edited after being applied. "
                  f"Applied migrations are immutable -- make a new NNNN file instead.",
                  file=sys.stderr)
            con.close()
            return 2

    if not todo:
        print("nothing pending -- schema up to date.")
        con.close()
        return 0

    if args.dry_run:
        print("DRY RUN -- would apply:")
        for mid, slug, path in todo:
            print(f"  {mid}_{slug}")
        con.close()
        return 0

    backup = _backup_db()
    print(f"backed up db -> {backup}")

    for mid, slug, path in todo:
        with open(path) as f:
            sql = f.read()
        try:
            con.execute("BEGIN")
            con.executescript(sql)
            con.execute(
                "INSERT INTO schema_migrations (id, slug, sha256, applied_at) VALUES (?,?,?,?)",
                (mid, slug, _sha(path), datetime.datetime.now(datetime.timezone.utc).isoformat()),
            )
            con.commit()
            print(f"applied {mid}_{slug}")
        except Exception as e:
            con.rollback()
            print(f"FAILED {mid}_{slug}: {e}", file=sys.stderr)
            print(f"rolled back. db unchanged from before this migration. "
                  f"restore point: {backup}", file=sys.stderr)
            con.close()
            return 1

    con.close()
    return 0


def cmd_new(args):
    os.makedirs(MIG_DIR, exist_ok=True)
    existing = _discover()
    nxt = max((int(m[0]) for m in existing), default=-1) + 1
    slug = re.sub(r"[^a-z0-9]+", "_", args.desc.lower()).strip("_") or "migration"
    fn = f"{nxt:04d}_{slug}.sql"
    path = os.path.join(MIG_DIR, fn)
    if os.path.exists(path):
        print(f"already exists: {path}", file=sys.stderr)
        return 1
    with open(path, "w") as f:
        f.write(f"-- {fn}\n-- {args.desc}\n-- Applied once, in a transaction, "
                f"then immutable. Use IF NOT EXISTS / additive changes where possible.\n\n")
    print(f"created {path}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="minerva.db migration runner")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    up = sub.add_parser("up")
    up.add_argument("--dry-run", action="store_true")
    new = sub.add_parser("new")
    new.add_argument("desc")
    args = ap.parse_args()
    return {"status": cmd_status, "up": cmd_up, "new": cmd_new}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
