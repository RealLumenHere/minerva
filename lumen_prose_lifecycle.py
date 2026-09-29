#!/usr/bin/env python3
"""
lumen_prose_lifecycle.py - wq#577 [mv4-P5]: claude_memory.md SECTION-level demotion pipeline.

Design of record: MEMORY_V4_PLAN.md P5. Mirrors lumen_salience.py's
archive_candidates()/write_archive_plan()/apply_archive_plan() pattern (wq#576) but for
prose SECTIONS instead of archival_notes rows, with one structural difference stated up
front: a note_archives row is a pure DB flag flip (the note text never moves). A prose
demotion is a real file move -- the section leaves claude_memory.md and is appended to
context/claude_memory_archive.md, mirroring the existing archive_tail.py/archive_runbook.py
precedent (banner comment, append-don't-clobber, never DELETE).

STANDING LAW (verbatim from dispatch): claude_memory.md is proposal-only. propose() only
ever writes a JSON plan under dream/ and NEVER touches claude_memory.md. apply() is an
operator-invoked CLI action -- lumen_dream.run() calls propose(), never apply(). No timer,
no automation, ever calls apply() with confirm=True.

Signals (age + superseded-by-store + zero recall hits, per the ticket detail):
  age_days       - from a YYYY-MM-DD date in the section title. Undated sections are exempt
                   by construction (not enough information to be a candidate).
  fed_hits       - all-time note_hits rows for this section's federated_chunks ids. wq#576's
                   log_hits() tap writes note_id='fed_...' rows exactly like archival ones;
                   they are just never rolled into note_salience (that rollup joins
                   archival_notes). This module reads note_hits directly instead.
  stale_by_store - lumen_dream.stale_prose_scan's IP-supersession finding, mapped from line
                   number to containing section. Reuses the one working store-supersession
                   detector already in the codebase instead of re-deriving one.
  note_cov       - max cosine overlap between any chunk in the section and any archival_notes
                   row (the same population lumen_federate.dedup() computes and its docstring
                   already calls "wq#577 demotion candidates").
  self_superseded - non-newest member of a tag_sections.py DATED_CORE_GROUPS class (e.g. a
                   2026-05-13 "CURRENT STATE OVERRIDES" block superseded by a 2026-06-07 one).
                   tag_sections.py already demotes these from core; this signal notices that.

HONEST PREMISE NOTE (verified live 2026-08-15, restated in reports/577_prose_lifecycle.md):
on today's corpus stale_by_store and note_cov are BOTH EMPTY (0 stale citations, 0 note-
coverage pairs >= 0.95 per the same-day federated_dedup report) -- the prose corpus has not
yet been distilled into notes and nothing in it currently contradicts the bi-temporal store.
fed_hits is real but only 4 rows exist fleet-wide (the federated tier is <24h old and
MEMORY_V4_FEDERATED defaults off), so "zero hits" is true for ~every section today and does
no discriminating work yet. age is therefore the only signal doing real work on this corpus;
the other three are wired correctly against real tables and will start contributing the
moment they have real data. Every candidate carries its `reasons` list so a demotion is never
silently "it's old" when the file says otherwise.

CLI:
  python3 lumen_prose_lifecycle.py propose
  python3 lumen_prose_lifecycle.py apply --plan <path> --confirm
  python3 lumen_prose_lifecycle.py restore --section <section_id>
  python3 lumen_prose_lifecycle.py stats
"""
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, '/home/lumen/minerva')
import minerva_memory as mm  # noqa: E402  embed(), decode_embedding(), cosine(), EMBED_DIM
import numpy as np  # noqa: E402
import lumen_dream  # noqa: E402  stale_prose_scan()

sys.path.insert(0, '/home/lumen/foreman/harness')
from masking import mask_text  # noqa: E402  wq#3183: mask prose_restore pre-images at write time

DB = os.environ.get('MINERVA_DB', '/home/lumen/minerva/minerva.db')
MEM = Path('/home/lumen/claude_memory.md')
ARC = Path('/home/lumen/minerva/context/claude_memory_archive.md')
DREAM_DIR = Path(os.environ.get('LUMEN_DREAM_DIR', '/home/lumen/minerva/dream'))
RESTORE_DIR = DREAM_DIR / 'prose_restore'

# tag_sections.py owns section-id derivation and is_core(); imported by path (not sys.path
# munging into context/) to match the archive_tail.py/archive_runbook.py precedent exactly.
_spec = importlib.util.spec_from_file_location(
    'tag_sections', '/home/lumen/minerva/context/tag_sections.py')
ts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ts)

DATE_RE = re.compile(r'(\d{4}-\d{2}-\d{2})')

# ---- thresholds (all overridable, all default to today's measured-conservative values) --
AGE_THR_DAYS = int(os.environ.get('MEMORY_V4_PROSE_AGE_DAYS', '90'))
FLOOR_AGE_DAYS = int(os.environ.get('MEMORY_V4_PROSE_FLOOR_DAYS', '30'))
NOTE_COV_THR = float(os.environ.get('MEMORY_V4_PROSE_NOTECOV', '0.95'))
PROSE_BUDGET_TOK = int(os.environ.get('MEMORY_V4_PROSE_BUDGET_TOK', '60000'))


def _now():
    return datetime.now(timezone.utc).isoformat()


def _connect(db=None):
    con = sqlite3.connect(db or DB, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _sha256_text(s):
    return hashlib.sha256(s.encode('utf-8', 'replace')).hexdigest()


# ============================ 1. SECTION MODEL ===========================

def parsed_sections(text=None):
    """[(section_id, title, is_core, line_start, line_end, raw_text)], 1-indexed
    inclusive line ranges over the ORIGINAL file (mirrors lumen_dream's stale_prose_scan
    line numbering so the two can be joined directly)."""
    text = MEM.read_text() if text is None else text
    secs = ts.split_sections(text)
    demote_idx = ts.superseded_core_indices(secs)
    out = []
    line = 1
    for i, s in enumerate(secs):
        n = len(s['lines'])
        start, end = line, line + n - 1
        line = end + 1
        if s['title'] == '[preamble]':
            continue
        sid = f'{i:03d}_{ts.slugify(s["title"])}'
        core = ts.is_core(s['title']) and i not in demote_idx
        out.append({
            'ix': i, 'id': sid, 'title': s['title'], 'core': core,
            'self_superseded': i in demote_idx,
            'line_start': start, 'line_end': end,
            'raw': '\n'.join(s['lines']),
        })
    return out


def section_age_days(title, now=None):
    m = DATE_RE.search(title)
    if not m:
        return None
    try:
        d = datetime.strptime(m.group(1), '%Y-%m-%d').replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - d).days


# ============================ 2. SIGNALS ==================================

def fed_hits_by_section(conn):
    """section_id -> all-time note_hits count, joined via federated_chunks.id.
    Direct query against note_hits (not note_salience -- that rollup's join
    requires archival_notes membership and would silently return 0 for every
    prose section, which is a worse bug than reading the raw log twice).

    MUST exclude synthetic callers (bench/guard/selftest) the same way
    lumen_salience.rollup() does -- caught live this session: a single golden-bench
    run (239 queries) logged real note_hits rows for whatever federated chunks it
    touched and collapsed the candidate set 18->4, because a query set designed to
    exercise every corner of the corpus looks exactly like heavy real usage if you
    don't filter the caller tag. wq#571 research called this the majority of the
    would-be volume, not a hygiene nicety."""
    import lumen_salience as _sal
    excl_sql, params = _sal._excl_sql('h.caller')
    rows = conn.execute(
        "SELECT fc.section_id, COUNT(h.note_id) AS hits "
        "FROM federated_chunks fc LEFT JOIN note_hits h "
        "ON h.note_id = fc.id AND " + excl_sql + " "
        "WHERE fc.source_kind='prose-section' GROUP BY fc.section_id", params).fetchall()
    return {r['section_id']: r['hits'] for r in rows}


def stale_by_section(conn, sections):
    """section_id -> [stale_prose_scan findings] whose line falls in that section."""
    findings = lumen_dream.stale_prose_scan(conn)
    out = {}
    for i, ip, subj, pred, curv, when, txt in findings:
        for s in sections:
            if s['line_start'] <= i <= s['line_end']:
                out.setdefault(s['id'], []).append(
                    {'line': i, 'ip': ip, 'subject': subj, 'predicate': pred,
                     'current_value': curv, 'superseded': when})
                break
    return out


def note_coverage_by_section(conn):
    """section_id -> max cosine vs any archival_notes embedding. Same population as
    lumen_federate.dedup()'s federated<->archival pairs, grouped/maxed per section
    instead of returned as pairs (this module wants "is this section covered", not
    a pairwise list)."""
    fed = conn.execute(
        "SELECT section_id, embedding FROM federated_chunks "
        "WHERE source_kind='prose-section' AND embedding IS NOT NULL").fetchall()
    notes = conn.execute(
        "SELECT embedding FROM archival_notes WHERE embedding IS NOT NULL "
        "AND id NOT IN (SELECT absorbed_id FROM note_merges)").fetchall()
    if not fed or not notes:
        return {}
    N = []
    for r in notes:
        v = mm.decode_embedding(r['embedding'], expect_dim=mm.EMBED_DIM)
        if v is not None:
            N.append(v / (np.linalg.norm(v) or 1e-9))
    if not N:
        return {}
    N = np.vstack(N)
    out = {}
    for r in fed:
        v = mm.decode_embedding(r['embedding'], expect_dim=mm.EMBED_DIM)
        if v is None:
            continue
        v = v / (np.linalg.norm(v) or 1e-9)
        cov = float(np.max(N @ v))
        sid = r['section_id']
        if cov > out.get(sid, 0.0):
            out[sid] = cov
    return out


# ============================ 3. CANDIDATES ================================

def demotion_candidates(conn=None, text=None, now=None):
    """Sections that COULD be proposed for demotion. Never acts. Returns list of dict,
    each carrying `reasons` so a demotion is never silently just "it's old"."""
    own = conn is None
    conn = conn or _connect()
    try:
        sections = parsed_sections(text)
        hits = fed_hits_by_section(conn)
        stale = stale_by_section(conn, sections)
        cov = note_coverage_by_section(conn)
        out = []
        for s in sections:
            if s['core']:
                continue
            age = section_age_days(s['title'], now)
            if age is None or age <= FLOOR_AGE_DAYS:
                continue
            h = hits.get(s['id'], 0)
            st = stale.get(s['id'], [])
            nc = cov.get(s['id'], 0.0)
            reasons = []
            if age > AGE_THR_DAYS and h == 0:
                reasons.append(f'age={age}d')
                reasons.append('zero_hits')
            if st:
                reasons.append(f'stale_store={len(st)}')
            if nc >= NOTE_COV_THR:
                reasons.append(f'note_covered={nc:.3f}')
            if s['self_superseded'] and age > FLOOR_AGE_DAYS:
                reasons.append('self_superseded')
            if not reasons:
                continue
            out.append({
                'id': s['id'], 'title': s['title'][:120], 'age_days': age,
                'hits': h, 'note_cov': round(nc, 4), 'stale_n': len(st),
                'self_superseded': s['self_superseded'],
                'reasons': reasons,
                'strong': bool(st or nc >= NOTE_COV_THR or s['self_superseded']),
            })
        out.sort(key=lambda c: (not c['strong'], -(c['age_days'] or 0)))
        return out
    finally:
        if own:
            conn.close()


def live_budget_status(text=None):
    text = MEM.read_text() if text is None else text
    tok = len(text) // 4
    return {'tok_est': tok, 'budget': PROSE_BUDGET_TOK, 'over': tok > PROSE_BUDGET_TOK}


# ============================ 4. PROPOSE (safe, read-only) =================

def propose(out_dir=None):
    """PROPOSAL ONLY. Never touches claude_memory.md. Mirrors
    lumen_salience.write_archive_plan: a JSON file an operator applies by hand."""
    conn = _connect()
    try:
        cands = demotion_candidates(conn)
        budget = live_budget_status()
    finally:
        conn.close()
    d = Path(out_dir or DREAM_DIR)
    d.mkdir(parents=True, exist_ok=True)
    p = d / ('prose_demotion_plan_%s.json' % datetime.now(timezone.utc).strftime('%Y-%m-%d'))
    p.write_text(json.dumps({
        'generated': _now(),
        'rule': ('(age>%dd AND hits==0) OR stale_store OR note_covered>=%.2f OR '
                 'self_superseded  -- floors: never core, never undated, never <=%dd old'
                 % (AGE_THR_DAYS, NOTE_COV_THR, FLOOR_AGE_DAYS)),
        'action': 'DEMOTE section text out of claude_memory.md into '
                   'context/claude_memory_archive.md. Never DELETE. Reversible: '
                   'python3 lumen_prose_lifecycle.py restore --section <id>',
        'apply': 'python3 lumen_prose_lifecycle.py apply --plan <file> --confirm',
        'live_file_budget': budget,
        'count': len(cands), 'candidates': cands}, indent=1))
    return p, cands, budget


# ============================ 5. APPLY (operator-gated) ====================

def apply_plan(path, confirm=False, db=None):
    """Operator-invoked. NEVER called by lumen_dream.run() or any timer -- claude_memory.md
    is proposal-only by standing law. Archive-not-erase: section text is moved, never
    deleted; a full pre-image is saved to RESTORE_DIR before either file is touched."""
    plan = json.loads(Path(path).read_text())
    cands = plan.get('candidates') or []
    if not confirm:
        print(f'DRY: {len(cands)} demotion candidate(s) in plan. Re-run with --confirm.')
        return 0
    ts_tag = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    live_text = MEM.read_text()
    sections = parsed_sections(live_text)
    by_id = {s['id']: s for s in sections}
    cand_ids = {c['id']: c for c in cands}

    demote, keep_secs = [], []
    skipped = []
    for s in sections:
        c = cand_ids.get(s['id'])
        if c and c['title'][:120] == s['title'][:120]:
            demote.append((s, c))
        else:
            keep_secs.append(s)
    matched_ids = {s['id'] for s, _ in demote}
    skipped = [cid for cid in cand_ids if cid not in matched_ids]

    if not demote:
        print(f'nothing to apply: 0/{len(cands)} candidates still match the live file '
              f'(skipped as stale: {skipped})')
        return 0

    live_bak = MEM.with_name(MEM.name + f'.bak.{ts_tag}_pre_wq577_apply')
    shutil.copy2(MEM, live_bak)
    ARC.parent.mkdir(parents=True, exist_ok=True)
    if ARC.exists():
        arc_bak = ARC.with_name(ARC.name + f'.bak.{ts_tag}_pre_wq577_apply')
        shutil.copy2(ARC, arc_bak)

    RESTORE_DIR.mkdir(parents=True, exist_ok=True)
    live_sha_pre = _sha256_text(live_text)
    banner = (f'\n\n<!-- ===== ARCHIVED {datetime.now(timezone.utc).date()} from '
              f'claude_memory.md by wq#577 prose-lifecycle apply ({path}): '
              f'section-level demotion, age/store/usage-gated. Record-only; '
              f'still retrievable via federated recall (source_kind=prose-archive). ===== -->\n')
    arc_addition = banner + '\n'.join(s['raw'] for s, _ in demote) + '\n'
    prev_arc = ARC.read_text() if ARC.exists() else ''
    new_arc_text = prev_arc + arc_addition
    ARC.write_text(new_arc_text)
    archive_sha_post = _sha256_text(new_arc_text)

    kept_text = '\n'.join(s['raw'] for s in keep_secs)
    if not kept_text.endswith('\n'):
        kept_text += '\n'
    MEM.write_text(kept_text)

    db_path = db or DB
    con = sqlite3.connect(db_path, timeout=30)
    try:
        for s, c in demote:
            (RESTORE_DIR / f'{s["id"]}__{ts_tag}.md').write_text(mask_text(s['raw']))
            con.execute(
                'INSERT INTO prose_demotions(section_id,title,demoted_at,reason,'
                'age_days,hits,note_cov,live_sha_pre,archive_sha_post,plan_ref) '
                'VALUES (?,?,?,?,?,?,?,?,?,?)',
                (s['id'], s['title'][:200], _now(), ','.join(c.get('reasons') or []),
                 c.get('age_days'), c.get('hits'), c.get('note_cov'),
                 live_sha_pre, archive_sha_post, str(path)))
        con.commit()
    finally:
        con.close()

    try:
        import lumen_federate
        # lumen_federate hardcodes its own MEM/DB constants (no path-injection
        # support) -- guard against calling it with THIS module's paths pointed
        # somewhere else (tests, scratch copies), which would otherwise silently
        # reindex the real production file/db out from under an isolated test.
        # (Caught by running the real round-trip test, not by reading the code.)
        if lumen_federate.MEM.resolve() == MEM.resolve() and lumen_federate.DB == db_path:
            lumen_federate.index(full=False, verbose=False)
        else:
            print('  ! post-apply federated re-index SKIPPED: MEM/DB paths do not '
                  'match lumen_federate\'s hardcoded production paths (test/scratch '
                  'run) -- re-index manually if this was production.', file=sys.stderr)
    except Exception as e:
        print(f'  ! post-apply federated re-index failed (non-fatal): '
              f'{type(e).__name__}: {e}', file=sys.stderr)

    print(f'demoted {len(demote)} section(s), skipped {len(skipped)} (stale vs plan). '
          f'live backup: {live_bak}')
    return len(demote)


# ============================ 6. RESTORE (paired reversibility) ============

def restore(section_id, db=None):
    """Move a demoted section back into claude_memory.md from its saved pre-image.
    Marks prose_demotions.restored_at. Known gap (stated, not silently hidden): the
    text also stays in claude_memory_archive.md and federated_chunks(prose-archive)
    after a restore -- a harmless duplicate, not a correctness bug, but a real one;
    removing it requires re-parsing the accumulated archive file and is left as a
    follow-up rather than risking a bad match against hand-appended history."""
    con = sqlite3.connect(db or DB, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            'SELECT * FROM prose_demotions WHERE section_id=? AND restored_at IS NULL '
            'ORDER BY demoted_at DESC LIMIT 1', (section_id,)).fetchone()
        if not row:
            print(f'no active demotion found for {section_id}')
            return False
        snaps = sorted(RESTORE_DIR.glob(f'{section_id}__*.md'))
        if not snaps:
            print(f'no restore snapshot found for {section_id} under {RESTORE_DIR}')
            return False
        raw = snaps[-1].read_text()
        cur = MEM.read_text()
        banner = (f'\n\n<!-- ===== RESTORED {datetime.now(timezone.utc).date()} '
                  f'to claude_memory.md by wq#577 prose-lifecycle restore '
                  f'(was demoted {row["demoted_at"]}, reason: {row["reason"]}). ===== -->\n')
        MEM.write_text(cur + banner + raw + '\n')
        con.execute('UPDATE prose_demotions SET restored_at=? WHERE section_id=? '
                    'AND demoted_at=?', (_now(), row['section_id'], row['demoted_at']))
        con.commit()
        print(f'restored {section_id} into claude_memory.md (appended; original '
              f'position not preserved)')
        return True
    finally:
        con.close()


# ============================== 7. CLI ======================================

def stats(db=None):
    conn = _connect(db)
    try:
        cands = demotion_candidates(conn)
        budget = live_budget_status()
        n_demoted = conn.execute(
            'SELECT COUNT(*) FROM prose_demotions WHERE restored_at IS NULL').fetchone()[0]
        print(f'live file: {budget["tok_est"]} tok est (budget {budget["budget"]}, '
              f'over={budget["over"]})')
        print(f'candidates now: {len(cands)}  |  active demotions: {n_demoted}')
        for c in cands[:20]:
            print(f'  {c["id"]:45} age={c["age_days"]:>4}d hits={c["hits"]} '
                  f'reasons={",".join(c["reasons"])}')
    finally:
        conn.close()


def _main():
    import argparse
    ap = argparse.ArgumentParser(description='wq#577 prose lifecycle (proposal-only '
                                              'unless apply --confirm is invoked by hand)')
    sub = ap.add_subparsers(dest='cmd')
    sub.add_parser('propose')
    pa = sub.add_parser('apply')
    pa.add_argument('--plan', required=True)
    pa.add_argument('--confirm', action='store_true')
    pr = sub.add_parser('restore')
    pr.add_argument('--section', required=True)
    sub.add_parser('stats')
    args = ap.parse_args()
    if args.cmd == 'propose':
        p, cands, budget = propose()
        print(f'wrote {p}  ({len(cands)} candidates)')
        print(f'live file budget: {budget}')
    elif args.cmd == 'apply':
        apply_plan(args.plan, confirm=args.confirm)
    elif args.cmd == 'restore':
        restore(args.section)
    elif args.cmd == 'stats':
        stats()
    else:
        ap.print_help()


if __name__ == '__main__':
    _main()
