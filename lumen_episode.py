#!/usr/bin/env python3
"""
lumen_episode.py - Memory v2 Phase 2: episodic experience layer (repeat-failure guard).

Built ON minerva_memory.py (existing 3-tier store) -- does NOT duplicate it.
Reflexion/ExpeL pattern: commit a structured post-mortem after a session/change,
then RECALL relevant past episodes BEFORE attempting similar work. This is the
mechanism that stops Lumen's recurring "house pathologies" (acting on retired
PBox, the node-killer reaping live sessions, daily fleet deaths, etc.).

API:
  commit(summary, changed=[], worked=[], failed=[], why='', kind='session',
         tags=[], session_ref='') -> note_id      # structured post-mortem
  recall(query, top_k=6) -> list[dict]             # relevant past episodes
  precheck(plan) -> (verdict, hits)                # gate: have we tried/failed this?

CLI:
  lumen_episode.py recall  "<query>"
  lumen_episode.py precheck "<plan>"
  lumen_episode.py commit  --summary "..." --changed "a;b" --worked "c" --failed "d" --why "..." --tags "x,y"
  lumen_episode.py backfill                         # seed documented house-pathologies
"""
import sys, argparse
import minerva_memory as mm

FAILURE_KINDS = {'failure', 'lesson', 'bug', 'pathology', 'outage'}

def commit(summary, changed=None, worked=None, failed=None, why='',
           kind='session', tags=None, session_ref=''):
    def _b(label, items):
        items = [i for i in (items or []) if i]
        return f'## {label}\n' + ('\n'.join(f'- {i}' for i in items) if items else '- (none)') + '\n'
    content = (
        _b('What changed', changed) + '\n' +
        _b('What worked', worked) + '\n' +
        _b('What failed', failed) + '\n' +
        f'## Why / lessons\n{why or "(none)"}\n'
    )
    nid = mm.write_note(kind, summary[:200], content, tags=tags, source_ref=session_ref)
    # also drop a terse recall-tier event so FTS keyword search finds it fast
    fail_str = '; '.join(failed or [])
    mm.log_event(kind, f'{summary} :: changed={"; ".join(changed or [])} :: failed={fail_str} :: {why}'[:3500],
                 ref=session_ref or nid)
    return nid

def recall(query, top_k=6):
    return mm.search(query, top_k=top_k, tiers=['recall', 'archival'])

def precheck(plan):
    hits = recall(plan, top_k=8)
    fails = [h for h in hits if (h.get('kind') or '').lower() in FAILURE_KINDS]
    if fails:
        verdict = f'\u26a0\ufe0f  {len(fails)} related past failure/lesson(s) found -- review before acting.'
    elif hits:
        verdict = f'{len(hits)} related prior episode(s) found (no recorded failures).'
    else:
        verdict = 'No prior episodes on this. Proceed, then commit a post-mortem.'
    return verdict, hits

# ---- documented house-pathologies, seeded so recall has real history ----
BACKFILL = [
    dict(kind='pathology', summary='Acting on PBox after it was retired (recurring stale-state)',
         changed=['Referenced/acted on PBox as a live inference node after 2026-06-07 retirement'],
         failed=['96+ stale PBox refs lingered in memory; embed/health checks pointed at a dead host'],
         why='No mechanism auto-invalidated "PBox active" when "PBox retired" landed. FIX (2026-06-25): bi-temporal state store -- current pbox status=retired auto-supersedes. ALWAYS query lumen_state.py current before acting on a node.',
         tags=['pbox','stale-state','retirement']),
    dict(kind='pathology', summary='Daily 13:00 UTC fleet deaths -- pause timer with no resume',
         changed=['lumen-pause.timer stopped agent+fleet workers at 13:00 UTC'],
         failed=['No resume counterpart -> fleet sat dead ~62h Jun 3-6'],
         why='Automation that stops services MUST have a paired restore. FIX: lumen-resume.timer 16:00 UTC. Lesson: any stop/disable needs a confirmed restore before session end.',
         tags=['fleet','systemd','pause','silent-failure']),
    dict(kind='pathology', summary='Node-killer/watchdog reaping live sessions',
         changed=['Watchdog reaped processes it judged stale'],
         failed=['Killed live work; "automation silently destroying critical function" (third instance of the pattern)'],
         why='Watchdogs need liveness signals that distinguish active work from hangs. Treat any auto-reaper with suspicion; gate destructive actions.',
         tags=['watchdog','node-killer','destructive-automation']),
    dict(kind='outage', summary='Minerva pipeline dead 5 days on silent http_404 fallback',
         changed=['Upstream removed hardcoded Cerebras model id; classifier/proposal/verify fell back silently'],
         failed=['aud=0.50/self=0.00, zero proposals 05-27..06-07; "verified" false claims from stale LUMEN_STATE in prompt'],
         why='Silent fallbacks are deadly. FIX: route via LiteLLM gateway, classifier exits 1 on whole-run fallback (fail-loud), verify fail-closed. Lesson: health checks must read error lines, not just service state.',
         tags=['minerva','silent-failure','fail-loud','litellm']),
    dict(kind='outage', summary='Messenger silent-Groq hallucination outage (2026-06-09)',
         changed=['lumen-messenger routed claude CLI through fragile Anthropic Max/API path'],
         failed=['Silently fell back to wrong provider -> hallucinated responses'],
         why='FIX: route messenger through LiteLLM -> DeepSeek V4 with dedicated entries + clamped max_tokens (dodges passthrough bug #22249). Do NOT route consolidation/extraction through messenger-chat/re.',
         tags=['messenger','deepseek','litellm','hallucination']),
    dict(kind='lesson', summary='Boot-drive inode crisis from orphaned MinIO stage files',
         changed=['Daily backup mirrored MinIO files as root:root; cleanup ran as lumen and could not delete them'],
         failed=['~22.6M orphan files; root 81% used / 87% inodes -- near-full'],
         why='Permission mismatch between writer and cleaner orphans files. FIX: STAGE_DIR -> /scratch, cleanup uses sudo rm + pre-run sweep. Watch inode% not just disk%.',
         tags=['storage','inode','backup','permissions']),
]

def backfill():
    n = 0
    for e in BACKFILL:
        commit(**e, session_ref='backfill:house-pathologies')
        n += 1
    print(f'backfilled {n} episodes')

def _fmt(h):
    k = (h.get('kind') or '').lower()
    mark = '\u26a0\ufe0f ' if k in FAILURE_KINDS else '  '
    return f'{mark}[{h["tier"]}/{h.get("kind","")}] {h.get("score",0):.2f}  {h.get("title","")[:80]}'

def _main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('backfill')
    pr = sub.add_parser('recall');   pr.add_argument('query', nargs='+')
    pp = sub.add_parser('precheck'); pp.add_argument('plan', nargs='+')
    pc = sub.add_parser('commit')
    pc.add_argument('--summary', required=True); pc.add_argument('--changed', default='')
    pc.add_argument('--worked', default=''); pc.add_argument('--failed', default='')
    pc.add_argument('--why', default=''); pc.add_argument('--kind', default='session')
    pc.add_argument('--tags', default=''); pc.add_argument('--ref', default='')
    a = ap.parse_args()
    if a.cmd == 'backfill':
        backfill()
    elif a.cmd == 'recall':
        for h in recall(' '.join(a.query)):
            print(_fmt(h))
    elif a.cmd == 'precheck':
        v, hits = precheck(' '.join(a.plan)); print(v)
        for h in hits:
            print(_fmt(h))
    elif a.cmd == 'commit':
        sp = lambda s: [x.strip() for x in s.replace(';', ',').split(',') if x.strip()]
        nid = commit(a.summary, changed=sp(a.changed), worked=sp(a.worked),
                     failed=sp(a.failed), why=a.why, kind=a.kind,
                     tags=sp(a.tags), session_ref=a.ref)
        print(f'committed episode {nid}')

if __name__ == '__main__':
    _main()
