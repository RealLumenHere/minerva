#!/usr/bin/env python3
"""
lumen_dream_digest.py — wq#1638: the drain report for the memory system's outbox.

minerva/dream is an outbox: every consolidation pass writes merge plans, prose
demotion plans, instinct candidates, detector proposals and reflections there, and
by design applies none of them. That half works. The other half — somebody reads
the queue — never ran: 36 artifacts, merge plans since 08-09, prose demotion plans
since 08-15, note_merges at 5 rows, prose_demotions at 0, no instinct ever retired.

Proposal-only stays law. This module changes exactly one thing about that: merge
pairs at cosine >= EXACT_THRESH auto-apply, because that class is mechanical
(near-identical notes, `method='exact-auto'` in note_merges), it is the class
lumen_dream.apply_merge_plan(exact_only=True) already wrote by hand, and it is
reversible with one statement — DELETE FROM note_merges WHERE absorbed_id=?.
Nothing else is applied here: prose, instincts, detectors and reflections are
listed with their age and their one-line apply command, and that is all.

Three outputs per run:
  1. dream/digest-<date>.md   — every unapplied plan, its age, its apply command
  2. a briefing post — briefing_section() renders it; lumen-briefing calls that,
     so the rendering lives in this repo and agent/daily_briefing.py stays a shim
  3. ONE user-lane wq ticket, which supersedes (closes) last week's digest ticket.
     The title carries the ISO week so `wq add`'s duplicate-title guard never
     collides, and the ticket carries a Liveness: tag that clears it for free once
     outbox-status.json says DRAINED.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

MINERVA = os.getenv('MINERVA_HOME', '/home/lumen/minerva')
DREAM = Path(MINERVA) / 'dream'
DB = os.getenv('MINERVA_DB', f'{MINERVA}/minerva.db')
WQ_BIN = os.getenv('DREAM_WQ_BIN', '/home/lumen/bin/wq')
QUEUE_DB = os.getenv('DREAM_QUEUE_DB', '/home/lumen/agents/work-queue/queue.db')

# The one auto-apply threshold in the memory system. Same number
# lumen_dream.apply_merge_plan defaults to, named once so the digest text, the
# ticket text and the apply call cannot drift apart.
EXACT_THRESH = 0.98
TICKET_LANE = 'user'
TICKET_PRI = 2
TICKET_MARK = '[dream-digest'          # title prefix; how last week's ticket is found
STATUS_JSON = 'outbox-status.json'     # liveness probe target + briefing input
DRAINED = 'OUTBOX DRAINED'             # the literal needle file_contains() looks for
STALE_DAYS = 10                        # briefing says so when the weekly run stopped


def _now():
    return datetime.now(timezone.utc)


_DATE_RE = re.compile(r'(\d{4}-\d{2}-\d{2})|(\d{8})')


def _age_days(path, now=None):
    """(age_days, source). Age comes from the artifact's own dated filename when it
    has one: instincts_candidates.yaml is rewritten in place every run, so mtime
    would report a 4-week-old backlog as fresh today."""
    now = now or _now()
    m = _DATE_RE.search(Path(path).name)
    if m:
        raw = m.group(1) or m.group(2)
        fmt = '%Y-%m-%d' if m.group(1) else '%Y%m%d'
        try:
            born = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            return max(0, (now - born).days), 'name'
        except ValueError:
            pass
    try:
        born = datetime.fromtimestamp(Path(path).stat().st_mtime, timezone.utc)
    except OSError:
        return -1, 'unknown'
    return max(0, (now - born).days), 'mtime'


def _connect(db=None):
    return sqlite3.connect(db or DB, timeout=30)


def _absorbed(conn):
    try:
        return {r[0] for r in conn.execute('SELECT absorbed_id FROM note_merges')}
    except sqlite3.Error:
        return set()


def _demoted(conn):
    try:
        return {r[0] for r in conn.execute(
            'SELECT section_id FROM prose_demotions WHERE restored_at IS NULL')}
    except sqlite3.Error:
        return set()


def _load(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _pending_merges(path, conn, state):
    """A merge plan is applied when every pair it proposes is in note_merges — which
    is also how a plan that a LATER plan already drained stops being listed. `pairs`
    is what _mark_superseded compares across plans."""
    plan = _load(path)
    if plan is None:
        return dict(pending=None, total=None, note='unreadable')
    merges = plan.get('merges') or []
    pending = [m for m in merges if m.get('absorb') not in state['absorbed']]
    n_exact = sum(1 for m in pending if (m.get('sim') or 0) >= EXACT_THRESH)
    return dict(pending=len(pending), total=len(merges),
                note=(f'{n_exact} at cosine >= {EXACT_THRESH}' if n_exact
                      else 'all sub-exact — manual review'),
                pairs=frozenset(frozenset((m.get('keep'), m.get('absorb'))) for m in pending))


def _pending_prose(path, conn, state):
    plan = _load(path)
    if plan is None:
        return dict(pending=None, total=None, note='unreadable')
    cands = plan.get('candidates') or []
    pending = [c for c in cands if c.get('id') not in state['demoted']]
    return dict(pending=len(pending), total=len(cands),
                note=f'{sum(1 for c in pending if c.get("strong"))} strong',
                pairs=frozenset(c.get('id') for c in pending))


def _pending_all(path, conn, state):
    """Review-only artifacts: nothing in the store records that a human read one,
    so the honest answer is 'still in the queue', not a fabricated applied-state."""
    return dict(pending=None, total=None, note='')


# Every artifact kind the dream/reflector/convo-learn passes drop in dream/. A file
# matching no glob still gets listed (see scan()) — a new proposal kind must show up
# in the digest by default, not vanish from it.
KINDS = (
    dict(name='episodic merge plan', glob='merge_plan_*.json', pending=_pending_merges,
         apply='python3 /home/lumen/minerva/lumen_dream.py apply --plan {p} --confirm',
         hint='exact-only by default; --all includes the sub-0.98 manual class'),
    dict(name='prose demotion plan', glob='prose_demotion_plan_*.json', pending=_pending_prose,
         apply='python3 /home/lumen/minerva/lumen_prose_lifecycle.py apply --plan {p} --confirm',
         hint='archive-not-erase; restore with `restore --section <id>`'),
    dict(name='instinct candidates', glob='instincts_candidates*.yaml', pending=_pending_all,
         apply=None, hint='Frank-gated: merge accepted rules into /home/lumen/instincts.yaml'),
    dict(name='governance detector proposal', glob='detector_proposals_*.json', pending=_pending_all,
         apply=None, hint='D-1/D-4 review — no apply path by design (design §3.3)'),
    dict(name='convo fact candidates', glob='convo-fact-candidates.md', pending=_pending_all,
         apply=None, hint='Frank-gated: never auto-supersedes state'),
    dict(name='reflection', glob='reflections/*.md', pending=_pending_all,
         apply=None, hint='read-only lesson proposals (lumen_reflector)'),
    dict(name='federated dedup report', glob='federated_dedup_*.md', pending=_pending_all,
         apply=None, hint='read-only (lumen_federate)'),
)

# Outputs of the run, not proposals waiting on anyone.
IGNORE = ('dream-*.md', STATUS_JSON, 'digest-*.md')


def _mark_superseded(items):
    """Each pass re-proposes what the last one proposed, so an unapplied plan whose
    every pending item is also in a newer plan of its kind is not a second job —
    applying the newest drains it too. Unmarked, seven overlapping merge plans read
    as seven commands to run and count the same pair seven times."""
    by_kind = {}
    for i in items:
        if i.get('pairs') is not None:
            by_kind.setdefault(i['kind'], []).append(i)
    for group in by_kind.values():
        if len(group) < 2:
            continue
        newest = min(group, key=lambda i: i['age_days'])
        newest['newest'] = True
        for i in group:
            if i is not newest and i['pairs'] <= newest['pairs']:
                i['superseded_by'] = Path(newest['path']).name
                i['apply'] = None
                i['note'] = f'every pending item is in {i["superseded_by"]} — apply that one'
                i['hint'] = 'no command of its own: the newest plan of this kind covers it'


def scan(plan_dir=None, db=None, now=None, conn=None):
    """Every artifact in dream/ that is still waiting on somebody. Appliable plans
    first (they are the ones a command drains), each group oldest first."""
    d = Path(plan_dir or DREAM)
    own = conn is None
    conn = conn or _connect(db)
    try:
        state = {'absorbed': _absorbed(conn), 'demoted': _demoted(conn)}
        items, seen = [], set()
        for kind in KINDS:
            for p in sorted(d.glob(kind['glob'])):
                if not p.is_file() or p in seen:
                    continue
                seen.add(p)
                found = kind['pending'](p, conn, state)
                if found['pending'] == 0:
                    continue            # every proposal in this plan is applied
                age, age_src = _age_days(p, now)
                items.append(dict(kind=kind['name'], path=str(p), age_days=age,
                                  age_from=age_src, hint=kind['hint'],
                                  apply=kind['apply'].format(p=p) if kind['apply'] else None,
                                  **found))
        for p in sorted(d.glob('*')):
            if not p.is_file() or p in seen or any(p.match(g) for g in IGNORE):
                continue
            age, age_src = _age_days(p, now)
            items.append(dict(kind='unclassified artifact', path=str(p), age_days=age,
                              age_from=age_src, pending=None, total=None, note='',
                              hint='no kind registered in lumen_dream_digest.KINDS',
                              apply=None))
    finally:
        if own:
            conn.close()
    _mark_superseded(items)
    for i in items:
        i.pop('pairs', None)            # comparison scratch, not digest content
    return sorted(items, key=lambda i: (i['apply'] is None, -i['age_days']))


def auto_apply(plan_path=None, plan_dir=None, db=None):
    """The ONE auto-apply: exact-duplicate merges from the newest plan. Returns the
    rows actually written (already-merged pairs are not re-reported)."""
    if plan_path is None:
        plans = sorted(Path(plan_dir or DREAM).glob('merge_plan_*.json'))
        if not plans:
            return []
        plan_path = plans[-1]
    # the sibling lumen_dream.py, not whatever MINERVA_HOME points at: apply and
    # digest ship together, and a worktree checkout must exercise its own copy
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import lumen_dream as _ld
    return _ld.apply_merge_plan(plan_path, confirm=True, exact_only=True,
                                exact_thresh=EXACT_THRESH, db=db or DB)


def _iso_week(now=None):
    return (now or _now()).strftime('%G-W%V')


def headline(items, applied):
    n_plans = len(items)
    # a plan the newest one already covers contributes no work of its own, so its
    # pairs are not counted twice
    n_items = sum(i['pending'] for i in items
                  if i['pending'] and not i.get('superseded_by'))
    oldest = max((i['age_days'] for i in items), default=0)
    return dict(pending_plans=n_plans, pending_items=n_items, oldest_days=oldest,
                applied=len(applied),
                commands=sum(1 for i in items if i['apply']),
                status=DRAINED if n_plans == 0 else f'OUTBOX PENDING: {n_plans}')


def render(items, applied, now=None, report=None):
    now = now or _now()
    h = headline(items, applied)
    L = [f'# Dream outbox digest — {now.strftime("%Y-%m-%d %H:%M UTC")}', '',
         f'**{h["pending_plans"]} unapplied artifact(s)**, {h["pending_items"]} distinct '
         f'proposal(s) inside them, oldest {h["oldest_days"]}d. '
         f'{h["commands"]} of them drain with one command; applied automatically this '
         f'run: {h["applied"]}.', '',
         '_Proposal-only is still the law: the only thing this run applied by itself is '
         f'exact-duplicate episodic merges (cosine >= {EXACT_THRESH}, `method=exact-auto`, '
         'reversible with `DELETE FROM note_merges WHERE absorbed_id=?`). Every line below '
         'waits on a human._', '']
    if report:
        L += [f'This week\'s consolidation report: `{report}`', '']
    L += [f'## Auto-applied exact merges ({len(applied)})']
    if applied:
        for m in applied:
            L.append(f'- `{m["absorb"]}` -> `{m["keep"]}`  (cosine {m["sim"]:.4f})  '
                     f'"{(m.get("absorb_title") or "")[:48]}"')
    else:
        L.append('- none — no pending pair reached the exact threshold this run.')
    L += ['', f'## Unapplied ({len(items)})']
    if not items:
        L.append(f'- none. {DRAINED}.')
    for i in items:
        count = (f'{i["pending"]}/{i["total"]} pending'
                 if i['pending'] is not None else 'review-only')
        stamp = f'{i["age_days"]}d' + ('' if i['age_from'] == 'name' else ' (mtime)')
        L.append(f'- **{i["kind"]}** · {stamp} · {count}'
                 + (f' · {i["note"]}' if i['note'] else '')
                 + (' · newest of its kind — start here' if i.get('newest') else ''))
        L.append(f'  `{i["path"]}`')
        L.append(f'  apply: `{i["apply"]}`' if i['apply'] else f'  {i["hint"]}')
        if i['apply']:
            L.append(f'  ({i["hint"]})')
    return '\n'.join(L) + '\n'


def write_status(items, applied, digest_path, ticket=None, plan_dir=None, now=None):
    """The machine-readable half: what the briefing renders and what the ticket's
    `Liveness: file_contains(..., OUTBOX DRAINED)` tag probes."""
    now = now or _now()
    h = headline(items, applied)
    p = Path(plan_dir or DREAM) / STATUS_JSON
    p.write_text(json.dumps(dict(
        generated=now.isoformat(), digest=str(digest_path), ticket=ticket,
        applied_ids=[m['absorb'] for m in applied],
        top=[dict(kind=i['kind'], age_days=i['age_days'], path=i['path'],
                  apply=i['apply']) for i in items[:5]], **h), indent=1) + '\n')
    return p


def briefing_section(plan_dir=None, now=None):
    """Rendered here, printed by lumen-briefing (agent/daily_briefing.py). Returns ''
    when there is nothing to say; says so out loud when the weekly run has stopped."""
    p = Path(plan_dir or DREAM) / STATUS_JSON
    s = _load(p)
    if not s:
        return ''
    now = now or _now()
    try:
        age = (now - datetime.fromisoformat(s['generated'])).days
    except (KeyError, ValueError):
        age = -1
    if age > STALE_DAYS:
        return (f'🌙 DREAM OUTBOX: no digest for {age}d — the weekly consolidation run '
                f'(lumen-dream.timer, Sun 18:00) has stopped. Last: {s.get("digest")}')
    if s.get('status') == DRAINED:
        return '🌙 DREAM OUTBOX: drained — no memory proposal is waiting on you.'
    L = [f'🌙 DREAM OUTBOX: {s.get("pending_plans", "?")} unapplied memory proposal(s), '
         f'oldest {s.get("oldest_days", "?")}d '
         f'({s.get("applied", 0)} exact merge(s) auto-applied)']
    for i in s.get('top', [])[:3]:
        L.append(f'   • {i["kind"]} ({i["age_days"]}d) — '
                 + (f'`{i["apply"]}`' if i.get('apply') else 'review-only'))
    if s.get('ticket'):
        L.append(f'   full digest: {s.get("digest")} (wq#{s["ticket"]})')
    return '\n'.join(L)


def _wq(args, wq_bin=None, timeout=60):
    try:
        return subprocess.run([wq_bin or WQ_BIN] + args, capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        print(f'# wq {args[0]} failed: {e}', file=sys.stderr)
        return None


def previous_tickets(queue_db=None):
    """Open user-lane digest tickets from earlier weeks. Read from the queue the same
    read-only way the briefing does — a state file under dream/ would drift the first
    time somebody closed the ticket by hand."""
    try:
        c = sqlite3.connect(f'file:{queue_db or QUEUE_DB}?mode=ro', uri=True)
        rows = c.execute(
            "SELECT id, title FROM tasks WHERE lane=? AND status='open' AND title LIKE ?",
            (TICKET_LANE, TICKET_MARK + '%')).fetchall()
        c.close()
    except sqlite3.Error as e:
        print(f'# queue read failed: {e}', file=sys.stderr)
        return []
    return rows


def open_ticket(items, applied, digest_path, status_path, now=None,
                wq_bin=None, queue_db=None):
    """Open this week's digest ticket, then close last week's against it. That order
    is deliberate: if `wq add` refuses, the old ticket is still open and Frank still
    has the backlog in front of him."""
    now = now or _now()
    h = headline(items, applied)
    if not items and not applied:
        return None, []
    title = (f'{TICKET_MARK} {_iso_week(now)}] {h["pending_plans"]} unapplied memory '
             f'proposal(s), oldest {h["oldest_days"]}d')
    lines = [
        f'The minerva/dream outbox has {h["pending_plans"]} artifact(s) waiting on you '
        f'({h["pending_items"]} proposals inside them). Full digest: {digest_path}', '',
        f'Auto-applied this run (the only class that applies itself): {len(applied)} '
        f'exact-duplicate episodic merge(s) at cosine >= {EXACT_THRESH}. '
        f'Reverse one with `DELETE FROM note_merges WHERE absorbed_id=\'<id>\'`.', '',
        'Top of the queue (age · one-line apply command):']
    for i in items[:8]:
        lines.append(f'- {i["kind"]} · {i["age_days"]}d · '
                     + (i['apply'] or f'review-only — {i["hint"]}'))
    lines += ['',
              'Nothing else auto-applies: prose demotion, instincts and detector '
              'proposals stay proposal-only by standing law (wq#1638).',
              f'This ticket supersedes the previous [dream-digest] ticket and is itself '
              f'superseded by next Sunday\'s run.',
              f'Liveness: file_contains({status_path}, {DRAINED})']
    out = _wq(['add', '--lane', TICKET_LANE, '--pri', str(TICKET_PRI),
               '--title', title, '--detail', '\n'.join(lines)], wq_bin)
    if out is None or out.returncode != 0:
        why = (out.stderr or out.stdout).strip()[:200] if out else 'wq unavailable'
        print(f'# digest ticket NOT opened: {why}', file=sys.stderr)
        return None, []
    m = re.search(r'added task (\d+)', out.stdout or '')
    tid = m.group(1) if m else None
    superseded = []
    for old_id, _title in previous_tickets(queue_db):
        if str(old_id) == str(tid):
            continue
        r = _wq(['done', str(old_id), '--notes',
                 f'superseded by wq#{tid} (this week\'s dream outbox digest). '
                 f'Current digest: {digest_path}'], wq_bin)
        if r is not None and r.returncode == 0:
            superseded.append(old_id)
        else:
            why = (r.stderr or r.stdout).strip()[:160] if r else 'wq unavailable'
            print(f'# wq#{old_id} not superseded: {why}', file=sys.stderr)
    return tid, superseded


def digest(plan_path=None, plan_dir=None, db=None, now=None, report=None,
           file_ticket=True, wq_bin=None, queue_db=None):
    """The whole pass: apply the exact merges, list what is left, write the digest,
    update the briefing/liveness status file, file one ticket. Called at the end of
    lumen_dream.run(); safe to run by hand (`lumen_dream.py digest`)."""
    now = now or _now()
    d = Path(plan_dir or DREAM)
    d.mkdir(parents=True, exist_ok=True)
    applied = auto_apply(plan_path=plan_path, plan_dir=d, db=db)
    items = scan(plan_dir=d, db=db, now=now)
    path = d / f'digest-{now.strftime("%Y-%m-%d")}.md'
    path.write_text(render(items, applied, now=now, report=report))
    status = d / STATUS_JSON
    ticket = superseded = None
    if file_ticket:
        ticket, superseded = open_ticket(items, applied, path, status, now=now,
                                         wq_bin=wq_bin, queue_db=queue_db)
    write_status(items, applied, path, ticket=ticket, plan_dir=d, now=now)
    return dict(path=path, items=items, applied=applied, ticket=ticket,
                superseded=superseded or [], status=status)


def report_section(result, err=''):
    """The block lumen_dream.run() puts in the weekly report."""
    if result is None:
        return [f'- digest FAILED: {err}']
    h = headline(result['items'], result['applied'])
    L = [f'- digest: {result["path"]} — {h["pending_plans"]} unapplied artifact(s), '
         f'oldest {h["oldest_days"]}d',
         f'- auto-applied {len(result["applied"])} exact merge(s) '
         f'(cosine >= {EXACT_THRESH}, method=exact-auto): '
         + (', '.join(m['absorb'] for m in result['applied'][:8]) or 'none')
         + (' …' if len(result['applied']) > 8 else '')]
    if result['ticket']:
        L.append(f'- user-lane ticket wq#{result["ticket"]}'
                 + (f' (superseded {", ".join("wq#%s" % s for s in result["superseded"])})'
                    if result['superseded'] else ''))
    else:
        L.append('- user-lane ticket: NOT filed (see stderr) — digest file still written')
    return L


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog='lumen_dream_digest.py', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd')
    r = sub.add_parser('run', help='apply exact merges, write the digest, file the ticket')
    r.add_argument('--no-ticket', action='store_true', help='digest only, no wq ticket')
    sub.add_parser('list', help='print the unapplied queue (no writes at all)')
    sub.add_parser('briefing', help='the lumen-briefing section (what the daily run prints)')
    args = ap.parse_args(argv)
    if args.cmd == 'briefing':
        print(briefing_section())
        return 0
    if args.cmd == 'list':
        print(render(scan(), []))
        return 0
    res = digest(file_ticket=not getattr(args, 'no_ticket', False))
    print('\n'.join(report_section(res)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
