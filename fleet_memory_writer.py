#!/usr/bin/env python3
"""fleet_memory_writer.py - wq#4795: the one writer for fleet_facts /
fleet_decisions rows in minerva.db.

Since the wq#4554 copy, claude_memory.md FLEET FACTS / decisions render from
minerva.db fleet_facts / fleet_decisions (context_builder.py), but
memory_mcp_server.py and reconcile.py still speak psycopg2 against the PG
copy those tables were copied FROM -- neither writes the live sqlite rows.
2026-10-31 (wq#4789) needed two raw sqlite UPDATEs to fix stale neith/khnum
prose because nothing else could do it. This module is that writer: it
reuses minerva_audit.audit() (the same append-only memory_audit chokepoint
lumen_state.py uses for state_facts) so every change lands with its audit
row in the same transaction, and refuses to touch a row that is not in the
state it expects.

CLI:
  fleet_memory_writer.py expire --table {fleet_facts,fleet_decisions} --id ID
                         --reason TEXT --source-ref wq#NNNN [--invariant TAG]
  fleet_memory_writer.py supersede-value --table {fleet_facts,fleet_decisions}
                         --id ID --new-value TEXT --reason TEXT
                         --source-ref wq#NNNN [--invariant TAG]

Both subcommands open their own connection, run the change inside one
BEGIN IMMEDIATE transaction (row update + memory_audit row), commit, then
regenerate claude_memory.md via context_builder.py so the fix is visible
immediately rather than at the next unrelated regen.

expire_facts_naming_subject() is the library entry point lumen_state.py
calls from its own transaction when a `lifecycle` ingest closes a subject
(retired/decommissioned): it expires every verified fleet_facts row whose
subject or value names that subject, so retiring a node retires every
prose copy of it, not just the state_facts rows lumen_state.py already owned.
"""
import argparse
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

import minerva_audit

DB_PATH = os.environ.get('MINERVA_DB_PATH', '/home/lumen/minerva/minerva.db')
CONTEXT_BUILDER_BIN = os.environ.get(
    'CONTEXT_BUILDER_BIN', '/home/lumen/minerva/memory/context_builder.py')
CLAUDE_MEMORY_MD = os.environ.get('CLAUDE_MEMORY_MD', '/home/lumen/claude_memory.md')

# Each table's editable text column and the status an "active" row must be
# in before this module will touch it (minerva.db observed values, wq#4795
# research: fleet_facts is verified/expired; fleet_decisions is active/superseded).
TABLES = {
    'fleet_facts': {'value_col': 'value', 'active_status': 'verified'},
    'fleet_decisions': {'value_col': 'decision', 'active_status': 'active'},
}


class RowStateError(ValueError):
    """Raised when a row is not in the state expire/supersede-value expects."""


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _table_cfg(table):
    try:
        return TABLES[table]
    except KeyError:
        raise ValueError(f"unknown table {table!r}; expected one of {list(TABLES)}")


def _table_exists(conn, table):
    """True if `table` exists in this DB -- the same sqlite_master check
    minerva_audit.ensure_table() makes before it writes memory_audit."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone() is not None


def _fetch_active(conn, table, row_id):
    cfg = _table_cfg(table)
    row = conn.execute(
        f"SELECT status, {cfg['value_col']} FROM {table} WHERE id=?", (row_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"{table} id={row_id} not found")
    status, value = row
    if status != cfg['active_status']:
        raise RowStateError(
            f"{table} id={row_id} is status={status!r}, expected "
            f"{cfg['active_status']!r} -- refusing to expire/supersede a row "
            f"not in the expected state")
    return value


def expire_row(conn, table, row_id, reason, source_ref, invariant=None):
    """Mark one fleet_facts/fleet_decisions row expired + one memory_audit
    row, on `conn`. Caller commits (minerva_audit.audit()'s own contract)."""
    _table_cfg(table)
    _fetch_active(conn, table, row_id)
    now = _now_iso()
    if table == 'fleet_facts':
        conn.execute(
            "UPDATE fleet_facts SET status='expired', expires_at=? WHERE id=?",
            (now, row_id))
    else:
        conn.execute(
            "UPDATE fleet_decisions SET status='expired' WHERE id=?", (row_id,))
    minerva_audit.audit(conn, 'revoke', table, str(row_id), source_ref=source_ref,
                         detail=reason, invariant=invariant)


def supersede_value(conn, table, row_id, new_value, reason, source_ref, invariant=None):
    """Rewrite the editable text column of one row in place + one memory_audit
    row recording the old value, on `conn`. Caller commits."""
    cfg = _table_cfg(table)
    old_value = _fetch_active(conn, table, row_id)
    conn.execute(f"UPDATE {table} SET {cfg['value_col']}=? WHERE id=?",
                 (new_value, row_id))
    detail = f"{reason}; old_value={old_value[:200]!r}"
    minerva_audit.audit(conn, 'supersede', table, str(row_id), source_ref=source_ref,
                         detail=detail, invariant=invariant)


def expire_facts_naming_subject(conn, subject, source_ref, invariant=None):
    """Expire every verified fleet_facts row whose subject or value names
    `subject` as a whole word (case-insensitive), on `conn`. Caller commits.

    wq#4789 found stale copies under subjects that are NOT the retired
    hostname itself (a `misc:commander-ha-pair-neith-khnum...` subject, a
    fleet_decisions-adjacent value mentioning it in prose) -- matching only
    `fleet_facts.subject == subject` would have missed exactly the rows that
    incident needed fixed by hand. Hostnames are joined with `-`/`:`/`,` in
    this prose (`commander-ha-pair-neith-khnum`), so only `\\w` counts as
    part of the token; `-`/`:` are boundaries like whitespace, not
    word-continuation characters, or the exact wq#4789 subject would never
    match.

    No-ops (returns []) on a DB that has no fleet_facts table: that table is
    not in lumen_state.init_schema() -- it was populated once by the wq#4554
    Postgres copy -- so every throwaway test fixture, and any minerva.db
    rebuilt before that copy runs, has none. This is the opportunistic half of
    a lifecycle closure and runs inside the CALLER's transaction
    (lumen_state.py:826-833), so raising here rolled back the whole
    state_facts closure the caller had already written, losing far more than
    the cascade (wq#4802; wq#4799 found it, 12 lumen_state tests). Same
    non-fatal stance as a missing context_builder regen below.

    Returns the list of fleet_facts ids expired."""
    needle = (subject or '').strip()
    if not needle:
        return []
    if not _table_exists(conn, 'fleet_facts'):
        return []
    pattern = re.compile(r'(?<!\w)' + re.escape(needle) + r'(?!\w)', re.IGNORECASE)
    rows = conn.execute(
        "SELECT id, subject, value FROM fleet_facts WHERE status='verified'").fetchall()
    expired = []
    for fid, fsubject, fvalue in rows:
        if pattern.search(fsubject or '') or pattern.search(fvalue or ''):
            expire_row(conn, 'fleet_facts', fid,
                       f'retired subject {needle} (lumen_state lifecycle)',
                       source_ref, invariant=invariant or f'lifecycle-retire-{needle}')
            expired.append(fid)
    return expired


def _regenerate_claude_memory():
    try:
        subprocess.run(
            ['python3', CONTEXT_BUILDER_BIN, '--out', CLAUDE_MEMORY_MD],
            timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"context_builder regen failed (non-fatal): {e}", file=sys.stderr)


def _cli_expire(args):
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    try:
        conn.execute('BEGIN IMMEDIATE')
        expire_row(conn, args.table, args.id, args.reason, args.source_ref,
                   invariant=args.invariant)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _regenerate_claude_memory()
    print(f"expired {args.table} id={args.id}")


def _cli_supersede(args):
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    try:
        conn.execute('BEGIN IMMEDIATE')
        supersede_value(conn, args.table, args.id, args.new_value, args.reason,
                         args.source_ref, invariant=args.invariant)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _regenerate_claude_memory()
    print(f"superseded {args.table} id={args.id}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest='cmd', required=True)

    pe = sub.add_parser('expire')
    pe.add_argument('--table', required=True, choices=list(TABLES))
    pe.add_argument('--id', required=True, type=int)
    pe.add_argument('--reason', required=True)
    pe.add_argument('--source-ref', required=True)
    pe.add_argument('--invariant', default=None)
    pe.set_defaults(func=_cli_expire)

    ps = sub.add_parser('supersede-value')
    ps.add_argument('--table', required=True, choices=list(TABLES))
    ps.add_argument('--id', required=True, type=int)
    ps.add_argument('--new-value', required=True)
    ps.add_argument('--reason', required=True)
    ps.add_argument('--source-ref', required=True)
    ps.add_argument('--invariant', default=None)
    ps.set_defaults(func=_cli_supersede)

    args = ap.parse_args()
    try:
        args.func(args)
    except (LookupError, RowStateError, ValueError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
