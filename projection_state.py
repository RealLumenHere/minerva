#!/usr/bin/env python3
"""wq#1633 — projection dirty-flag store (MINERVA_PIPELINE_MAP.md §4.4/§5#5).

Every rendered projection of `state_facts` (LIVE_FLEET_STATE(.md/_FULL.md),
lumen_state.txt) was a hand copy with a generated header and no regen trigger:
`state-regen` ran only when a human remembered, or when `state_reconcile`
happened to apply a change. This closes that gap with one tiny table:

  projection_state(name TEXT PRIMARY KEY, dirty INTEGER NOT NULL DEFAULT 1,
                    last_regen_at TEXT)

`lumen_state.ingest()` marks the state-derived projections dirty in the SAME
transaction as the fact write (see `_ingest()` in lumen_state.py, right after
`_migrate(conn)` — every exit path in that function commits after that point,
so the dirty write always lands with the fact it describes). `state-regen`
clears the flag after a successful render and stamps each output file with a
`generated-from: <store>@<ts>` header (see `stamp()`). `--if-dirty` lets
`claude_context.sh --core` call state-regen unconditionally and pay for a
real regen only when state_facts actually changed since the last one.

CLI:
  projection_state.py mark-dirty NAME [NAME...]
  projection_state.py clear NAME [NAME...] [--ts ISO]
  projection_state.py is-dirty NAME [NAME...]   # exit 0 if ANY is dirty, 1 if all clean
  projection_state.py stamp NAME FILE [FILE...] # prepend/replace the generated-from header
  projection_state.py status [--json]
  projection_state.py check-warn NAME --file PATH
                                                 # prints "WARN: ..." to stdout, exit 0 always
                                                 # (a stale projection is a notice, not a gate)

Env: MINERVA_DB (default /home/lumen/minerva/minerva.db)
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

MINERVA_DB = os.environ.get('MINERVA_DB', '/home/lumen/minerva/minerva.db')

# The two projections lumen_state.ingest() drives (MINERVA_PIPELINE_MAP.md §1.2/§1.3).
# federated_index (§1.7) is driven by claude_memory.md changes, not state_facts, so
# ingest() never marks it -- it is wired onto minerva-tag-sections.path instead.
STATE_PROJECTIONS = ('live_fleet_state', 'lumen_state_txt')

HEADER_RE = re.compile(r'^(?:<!--\s*|#\s*)?generated-from:\s*(\S+)@(\S+?)\s*(?:-->)?\s*$')


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _conn(path=None):
    conn = sqlite3.connect(path or MINERVA_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn):
    """Additive, idempotent — safe to call from any caller's transaction
    (mirrors lumen_state.py's own _migrate() convention)."""
    conn.execute('''CREATE TABLE IF NOT EXISTS projection_state (
        name TEXT PRIMARY KEY,
        dirty INTEGER NOT NULL DEFAULT 1,
        last_regen_at TEXT
    )''')


def mark_dirty(conn, names):
    """Set dirty=1 for each name. Caller commits (same-transaction contract)."""
    ensure_schema(conn)
    for name in names:
        conn.execute(
            'INSERT INTO projection_state (name, dirty) VALUES (?, 1) '
            'ON CONFLICT(name) DO UPDATE SET dirty=1',
            (name,))


def mark_state_projections_dirty(conn):
    """Called by lumen_state.py `_ingest()` for every accepted write."""
    mark_dirty(conn, STATE_PROJECTIONS)


def clear_dirty(conn, names, ts=None):
    ensure_schema(conn)
    ts = ts or now_iso()
    for name in names:
        conn.execute(
            'INSERT INTO projection_state (name, dirty, last_regen_at) VALUES (?, 0, ?) '
            'ON CONFLICT(name) DO UPDATE SET dirty=0, last_regen_at=excluded.last_regen_at',
            (name, ts))
    conn.commit()


def is_dirty(conn, name):
    """A projection with no row yet (never regenerated) reads as dirty."""
    ensure_schema(conn)
    row = conn.execute(
        'SELECT dirty FROM projection_state WHERE name=?', (name,)).fetchone()
    return True if row is None else bool(row['dirty'])


def any_dirty(conn, names):
    return any(is_dirty(conn, n) for n in names)


def last_regen_at(conn, name):
    ensure_schema(conn)
    row = conn.execute(
        'SELECT last_regen_at FROM projection_state WHERE name=?', (name,)).fetchone()
    return row['last_regen_at'] if row else None


def generated_from_header(store, ts=None, fmt='md'):
    ts = ts or now_iso()
    line = f'generated-from: {store}@{ts}'
    return f'<!-- {line} -->' if fmt == 'md' else f'# {line}'


def parse_generated_from(text):
    """First line only -- stamp() always writes the header as line 1."""
    first = (text.splitlines() or [''])[0]
    m = HEADER_RE.match(first.strip())
    return (m.group(1), m.group(2)) if m else (None, None)


def stamp_file(path, store, ts=None):
    """Prepend a generated-from header, replacing one already there (idempotent —
    re-stamping a file state-regen just wrote is a no-op on content, header only)."""
    fmt = 'txt' if path.endswith('.txt') else 'md'
    header = generated_from_header(store, ts, fmt)
    with open(path) as f:
        body = f.read()
    lines = body.splitlines(keepends=True)
    if lines and HEADER_RE.match(lines[0].strip()):
        lines = lines[1:]
    with open(path, 'w') as f:
        f.write(header + '\n')
        f.writelines(lines)


def max_ingested_at(conn):
    row = conn.execute(
        'SELECT MAX(ingested_at) AS m FROM state_facts').fetchone()
    return row['m'] if row and row['m'] else None


def freshness_warn(header_ts, compare_ts):
    """True when the generated file is older than the source it claims to render."""
    if not header_ts or not compare_ts:
        return False
    try:
        h = datetime.fromisoformat(header_ts)
        c = datetime.fromisoformat(compare_ts)
    except ValueError:
        return False
    return h < c


def cmd_mark_dirty(args):
    conn = _conn(args.db)
    mark_dirty(conn, args.names)
    conn.commit()
    return 0


def cmd_clear(args):
    conn = _conn(args.db)
    clear_dirty(conn, args.names, ts=args.ts)
    return 0


def cmd_is_dirty(args):
    conn = _conn(args.db)
    dirty = any_dirty(conn, args.names)
    print('dirty' if dirty else 'clean')
    return 0 if dirty else 1


def cmd_stamp(args):
    for path in args.files:
        stamp_file(path, args.name)
    return 0


def cmd_status(args):
    conn = _conn(args.db)
    ensure_schema(conn)
    rows = {r['name']: dict(r) for r in
            conn.execute('SELECT name, dirty, last_regen_at FROM projection_state')}
    for name in STATE_PROJECTIONS:
        rows.setdefault(name, {'name': name, 'dirty': 1, 'last_regen_at': None})
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for name, row in sorted(rows.items()):
            state = 'DIRTY' if row['dirty'] else 'clean'
            print(f"{name:20s} {state:6s} last_regen_at={row['last_regen_at']}")
    return 0


def cmd_check_warn(args):
    conn = _conn(args.db)
    with open(args.file) as f:
        first_line = f.readline()
    store, header_ts = parse_generated_from(first_line)
    if header_ts is None:
        print(f'WARN: {args.file} carries no generated-from header (wq#1633)')
        return 0
    max_ts = max_ingested_at(conn)
    if freshness_warn(header_ts, max_ts):
        print(f'WARN: {args.name} generated {header_ts} is older than the latest '
              f'state_facts write {max_ts} (wq#1633) — run state-regen')
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--db', default=MINERVA_DB)
    sub = p.add_subparsers(dest='cmd', required=True)

    pm = sub.add_parser('mark-dirty')
    pm.add_argument('names', nargs='+')

    pc = sub.add_parser('clear')
    pc.add_argument('names', nargs='+')
    pc.add_argument('--ts')

    pi = sub.add_parser('is-dirty')
    pi.add_argument('names', nargs='+')

    ps = sub.add_parser('stamp')
    ps.add_argument('name')
    ps.add_argument('files', nargs='+')

    pst = sub.add_parser('status')
    pst.add_argument('--json', action='store_true')

    pw = sub.add_parser('check-warn')
    pw.add_argument('name')
    pw.add_argument('--file', required=True)

    args = p.parse_args(argv)
    fn = {
        'mark-dirty': cmd_mark_dirty,
        'clear': cmd_clear,
        'is-dirty': cmd_is_dirty,
        'stamp': cmd_stamp,
        'status': cmd_status,
        'check-warn': cmd_check_warn,
    }[args.cmd]
    return fn(args)


if __name__ == '__main__':
    sys.exit(main())
