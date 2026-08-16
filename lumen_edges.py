#!/usr/bin/env python3
"""lumen_edges.py — Memory V3 P2: deterministic edge extraction over memory objects.

Builds memory_edges (note -> subject/ticket/file/tag) from archival_notes.
NO LLM calls. Idempotent (INSERT OR IGNORE); incremental via --since or full via --rebuild.

Edges extracted per note (title+content scanned):
  note --mentions--> subject   (word-boundary match on state_facts subjects, len>=3)
  note --references--> ticket  (wq#N, aq#N)
  note --touches--> file       (/home/lumen/... absolute paths)
  note --tagged--> tag         (archival_notes.tags CSV)

Usage:
  python3 lumen_edges.py --rebuild        # full rebuild (delete + re-extract)
  python3 lumen_edges.py                  # incremental: only notes without edges
  python3 lumen_edges.py --stats
"""
import re
import sqlite3
import sys
import time

DB = '/home/lumen/minerva/minerva.db'
TICKET_RE = re.compile(r'\b(wq|aq)#(\d+)\b', re.I)
FILE_RE = re.compile(r'(/home/lumen/[\w\./\-]+)')
# subjects that are too generic as mention anchors (match everything, carry no signal)
SUBJECT_STOPLIST = {'lumen', 'fleet', 'minerva', 'models', 'security', 'wq'}


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def load_subjects(conn):
    subs = {r[0] for r in conn.execute(
        'SELECT DISTINCT subject FROM state_facts').fetchall()}
    return sorted(s for s in subs
                  if len(s) >= 3 and s not in SUBJECT_STOPLIST and not s.startswith('_'))


def extract_for_note(nid, title, content, tags, subjects_re):
    blob = f'{title or ""} {content or ""}'
    blob_lc = blob.lower()
    edges = set()
    for m in subjects_re.finditer(blob_lc):
        edges.add(('note', nid, 'mentions', 'subject', m.group(0)))
    for m in TICKET_RE.finditer(blob):
        edges.add(('note', nid, 'references', 'ticket',
                   f'{m.group(1).lower()}#{m.group(2)}'))
    for m in FILE_RE.finditer(blob):
        p = m.group(1).rstrip('.').rstrip('/')
        if len(p) > 12:
            edges.add(('note', nid, 'touches', 'file', p))
    for t in (tags or '').split(','):
        t = t.strip().lower()
        if t:
            edges.add(('note', nid, 'tagged', 'tag', t))
    return edges


def build(rebuild=False):
    conn = sqlite3.connect(DB)
    subjects = load_subjects(conn)
    subjects_re = re.compile(
        r'\b(' + '|'.join(re.escape(s) for s in subjects) + r')\b')
    if rebuild:
        conn.execute("DELETE FROM memory_edges WHERE src_type='note'")
    done_ids = {r[0] for r in conn.execute(
        "SELECT DISTINCT src_id FROM memory_edges WHERE src_type='note'").fetchall()}
    rows = conn.execute(
        'SELECT id, title, content, tags FROM archival_notes').fetchall()
    now = _now()
    n_notes = n_edges = 0
    for nid, title, content, tags in rows:
        if nid in done_ids:
            continue
        edges = extract_for_note(nid, title, content, tags, subjects_re)
        for e in edges:
            conn.execute(
                'INSERT OR IGNORE INTO memory_edges VALUES (?,?,?,?,?,?)',
                (*e, now))
        n_notes += 1
        n_edges += len(edges)
    conn.commit()
    conn.close()
    print(f'edges built: {n_notes} notes processed, {n_edges} edges added')


def stats():
    conn = sqlite3.connect(DB)
    for rel, dt, c in conn.execute(
            'SELECT rel, dst_type, COUNT(*) FROM memory_edges '
            'GROUP BY rel, dst_type ORDER BY 3 DESC').fetchall():
        print(f'  {rel} -> {dt}: {c}')
    top = conn.execute(
        "SELECT dst_id, COUNT(*) c FROM memory_edges WHERE dst_type='subject' "
        'GROUP BY dst_id ORDER BY c DESC LIMIT 10').fetchall()
    print('  top subjects:', ', '.join(f'{s}({c})' for s, c in top))
    conn.close()


if __name__ == '__main__':
    if '--stats' in sys.argv:
        stats()
    else:
        build(rebuild='--rebuild' in sys.argv)
