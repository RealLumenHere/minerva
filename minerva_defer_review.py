#!/usr/bin/env python3
"""minerva_defer_review — make 'defer' a state that ends, not a dead letter.

WHY (audited 2026-08-14): minerva_eval consumes status='accept' only. Nothing has
ever read status='defer'. 495 defers had decided_at NULL and the last human sweep
was 2026-08-10, so ~50 items a day were being written to a table nobody reads.
Worse, the single most common defer reason is an INFORMATION GAP ("uncertain
applicability", "needs a benchmark first") - which is not a verdict, it is a
research question that Lumen already has four idle research workers able to answer.

Three outcomes, no fourth:
  RESEARCH  information-gap defers worth resolving become real research prompts in
            /home/lumen/scratch/research_prompts/ (the queue the fleet workers
            already drain) and move to status='researching'.
  REAP      when that prompt lands in research_prompts/done/, the proposal is
            re-scored with the research note attached -> accept / reject, for real.
  RETIRE    settled defers ("overlaps what we already have", "paper not a tool")
            and anything older than STALE_DAYS become 'rejected_aged' with a reason,
            so the pile drains instead of growing.

Read-only against everything except the proposals table and the prompt queue.
Usage: minerva_defer_review.py [review|reap|status] [--dry] [--limit N]
"""
import json, os, re, sqlite3, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

DB = '/home/lumen/minerva/minerva.db'
PROMPTS = Path('/home/lumen/scratch/research_prompts')
DONE = PROMPTS / 'done'
MIN_AGE_DAYS = int(os.getenv('DEFER_MIN_AGE_DAYS', '7'))
STALE_DAYS = int(os.getenv('DEFER_STALE_DAYS', '45'))
MAX_PROMPTS = int(os.getenv('DEFER_MAX_PROMPTS', '5'))
PREFIX = 'minerva_defer_'

INFO_GAP = re.compile(
    r'uncertain|unclear|no evidence|not proven|requires (a )?(benchmark|validation|investigation|evaluation)'
    r'|pending (a )?(benchmark|test|evaluation)|needs? (a )?(benchmark|measurement|proof)|unverified', re.I)
# Extended 2026-08-14 after sampling the real pile: the first cut left 241 of 495
# defers unclassified, and most of them were plainly settled - "academic paper, not
# a tool", "impractical on this hardware", "would require re-architecting". Those
# are verdicts already, so they retire instead of aging for 45 more days.
SETTLED = re.compile(
    r'overlap|already (has|have|built|exists|provides)|duplicat|off-mission|not applicable'
    r'|superseded|no clear (immediate )?use case|is a benchmark, not|theoretical paper|lacks a ready'
    r'|not (a )?(directly )?(ready|actionable|applicable)|academic (paper|technique|framework)'
    r'|no ready-to-use|impractical|requires? (a )?gpu|no gpu|beyond (the )?scope'
    r'|re-?architect|not directly proven|large new subsystem|speculative', re.I)


def log(m):
    print(f'[{datetime.now(timezone.utc).strftime("%H:%M:%S UTC")}] {m}', flush=True)


def db():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _value_ok(r):
    """Worth spending a research worker on."""
    return (r['impact'] in ('L', 'XL')) or (r['effort'] in ('S', 'M') and r['risk'] in ('S', 'M'))


def build_prompt(r):
    """The prompt names the exact unknown that caused the defer, so the research
    worker resolves THAT rather than re-summarising the source article."""
    return f"""RESEARCH PROMPT: resolve the open question blocking proposal {r['id']}

Proposal: {r['title']}
Deferred on: {(r['created_at'] or '')[:10]}
Why it was deferred: {r['decision_reason']}

What Lumen does today in this area:
{(r['current_state'] or '')[:800]}

What was proposed:
{(r['proposed_change'] or '')[:1000]}

OBJECTIVE — answer only the question that caused the deferral, with evidence:
1. Is the claimed capability real and currently maintained? Cite sources with dates.
2. Does it actually fit a CPU-only Dell T5810 (Xeon E5-2699 v4, no AVX-512/AMX, no
   inference GPU, 64GB RAM, all model traffic through a local LiteLLM gateway)?
   If it needs a GPU, AVX-512, AMX or a hosted API with per-token billing, say so
   plainly and recommend SKIP.
3. Where does it overlap what Lumen already runs, and what would it replace?
4. Give measured or vendor-published numbers where they exist; label which is which.

END WITH: RECOMMENDATION: ADOPT | WATCH | SKIP, one paragraph of reasoning, and — if
ADOPT — the smallest concrete first change (one file or one new service, named).
No paid APIs, no Anthropic spend."""


def cmd_review(limit=MAX_PROMPTS, dry=False):
    PROMPTS.mkdir(parents=True, exist_ok=True)
    c = db()
    rows = c.execute(
        "SELECT * FROM proposals WHERE status='defer' "
        "AND created_at <= datetime('now', ?) ORDER BY created_at ASC",
        (f'-{MIN_AGE_DAYS} days',)).fetchall()
    log(f'defers older than {MIN_AGE_DAYS}d: {len(rows)}')
    sent = retired = 0
    for r in rows:
        reason = r['decision_reason'] or ''
        stale = c.execute("SELECT 1 FROM proposals WHERE id=? AND created_at <= datetime('now', ?)",
                          (r['id'], f'-{STALE_DAYS} days')).fetchone() is not None
        if SETTLED.search(reason) and not INFO_GAP.search(reason):
            if not dry:
                _retire(r, 'settled at generation time: ' + reason[:200])
            retired += 1
            continue
        if INFO_GAP.search(reason) and _value_ok(r) and sent < limit:
            if _already_queued(r['id']):
                continue
            if not dry:
                _queue_research(r)
            log(f'  RESEARCH {r["id"]} {r["title"][:60]}')
            sent += 1
            continue
        if stale:
            if not dry:
                _retire(r, f'no path to a decision after {STALE_DAYS} days: ' + reason[:180])
            retired += 1
    c.close()
    log(f'review done: research={sent} retired={retired} dry={dry}')
    return sent, retired


def _already_queued(pid):
    for d in (PROMPTS, DONE, PROMPTS / 'stuck'):
        try:
            if any(f.name.startswith(PREFIX + pid) for f in d.iterdir()):
                return True
        except Exception:
            pass
    return False


def _queue_research(r):
    path = PROMPTS / f'{PREFIX}{r["id"]}.txt'
    path.write_text(build_prompt(r))
    c = db()
    c.execute("UPDATE proposals SET status='researching', decision_reason=? WHERE id=?",
              (f'Deferred for an information gap; research queued at {path.name}. '
               f'Original: {(r["decision_reason"] or "")[:280]}', r['id']))
    c.commit(); c.close()


def _retire(r, why):
    c = db()
    c.execute("UPDATE proposals SET status='rejected_aged', decided_at=?, decision_reason=? WHERE id=?",
              (datetime.now(timezone.utc).isoformat(), f'defer-review: {why}'[:500], r['id']))
    c.commit(); c.close()


def cmd_reap(dry=False):
    """Research came back -> re-score the proposal with the note attached."""
    c = db()
    rows = c.execute("SELECT * FROM proposals WHERE status='researching'").fetchall()
    c.close()
    done = 0
    for r in rows:
        note = _find_note(r['id'])
        if not note:
            continue
        text = ''
        try:
            text = note.read_text()[:6000]
        except Exception:
            continue
        log(f'  REAP {r["id"]} <- {note.name} ({len(text)} chars)')
        if dry:
            done += 1
            continue
        verdict, why = _verdict_from(text)
        c = db()
        c.execute('UPDATE proposals SET status=?, decided_at=?, decision_reason=? WHERE id=?',
                  (verdict, datetime.now(timezone.utc).isoformat(),
                   f'defer-review reaped research {note.name}: {why}'[:500], r['id']))
        c.commit(); c.close()
        done += 1
    log(f'reap done: {done}')
    return done


def _find_note(pid):
    """The fleet claims a prompt by renaming it, and moves it to done/ when the
    research note is written - so any file carrying the id under done/ counts."""
    for d in (DONE, PROMPTS):
        try:
            for f in d.iterdir():
                if PREFIX + pid in f.name and f.is_file():
                    return f
        except Exception:
            pass
    res = Path('/home/lumen/scratch/research')
    try:
        for f in res.glob(f'*{pid}*'):
            return f
    except Exception:
        pass
    return None


def _verdict_from(text):
    """Deterministic read of the required RECOMMENDATION line. No verdict found =
    stay conservative and retire it rather than inventing an accept."""
    m = re.search(r'RECOMMENDATION:\s*(ADOPT|WATCH|SKIP)', text, re.I)
    if not m:
        return 'rejected_aged', 'research returned no RECOMMENDATION line'
    v = m.group(1).upper()
    tail = text[m.end():m.end() + 300].strip().replace(chr(10), ' ')
    if v == 'ADOPT':
        return 'accept', 'research says ADOPT: ' + tail[:200]
    if v == 'WATCH':
        return 'defer', 'research says WATCH: ' + tail[:200]
    return 'rejected_aged', 'research says SKIP: ' + tail[:200]


def cmd_status():
    c = db()
    for row in c.execute("SELECT status, count(*) n, min(date(created_at)) oldest "
                         "FROM proposals GROUP BY status ORDER BY n DESC"):
        print(f"{row['status']:<16} {row['n']:>5}  oldest {row['oldest']}")
    q = c.execute("SELECT count(*) n FROM proposals WHERE status='defer' "
                  "AND created_at <= datetime('now', ?)", (f'-{MIN_AGE_DAYS} days',)).fetchone()
    print(f'\ndefers eligible for review (>{MIN_AGE_DAYS}d): {q["n"]}')
    c.close()


if __name__ == '__main__':
    a = sys.argv[1:]
    dry = '--dry' in a
    lim = MAX_PROMPTS
    if '--limit' in a:
        lim = int(a[a.index('--limit') + 1])
    cmd = a[0] if a and not a[0].startswith('-') else 'status'
    if cmd == 'review':
        cmd_reap(dry); cmd_review(lim, dry)
    elif cmd == 'reap':
        cmd_reap(dry)
    else:
        cmd_status()
