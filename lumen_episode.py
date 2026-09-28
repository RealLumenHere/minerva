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

AUDITED WRITE PATH (wq#1634, MINERVA_PIPELINE_MAP.md §4.5): commit posts to memoryd
/commit when the daemon is healthy and the session is registered; otherwise it falls
back to a direct write that appends its own `memory_audit` row in the same
transaction. Either way the note is answerable by SELECT on memory_audit.
"""
import os, json, sys, argparse
import urllib.error
import urllib.request

import minerva_audit
import minerva_memory as mm

FAILURE_KINDS = {'failure', 'lesson', 'bug', 'pathology', 'outage'}

MEMORYD_URL = os.environ.get('MEMORYD_URL', 'http://127.0.0.1:8139')
MEMORYD_TIMEOUT = float(os.environ.get('MEMORYD_TIMEOUT', '4'))


def _memoryd_commit(kind, title, content, tags, source_ref):
    """POST the note to memoryd (wq#1634 / §4.5). Returns (note_id, None) when the
    daemon accepted it -- the kernel wrote the audit row itself -- or (None, why)
    so the caller can fall back to a direct write that keeps the trail.

    A CLI session that holds no wq claim is refused by design (I-7): memoryd derives
    the lane from a held ticket and will not take the writer's word for it. That is
    the ordinary case here, not an error, which is why every failure is a fall-back
    reason rather than an exception."""
    agent = minerva_audit.agent_id()
    if not agent:
        return None, 'no $WQ_AGENT (memoryd needs a registered session)'
    try:
        with urllib.request.urlopen(MEMORYD_URL + '/healthz',
                                    timeout=MEMORYD_TIMEOUT) as r:
            if r.status != 200:
                return None, f'healthz {r.status}'
        body = json.dumps({'kind': kind, 'title': title, 'content': content,
                           'tags': tags or [], 'source_ref': source_ref or ''}).encode()
        req = urllib.request.Request(
            MEMORYD_URL + '/commit', data=body, method='POST',
            headers={'Content-Type': 'application/json', 'X-Memoryd-Agent': agent})
        with urllib.request.urlopen(req, timeout=MEMORYD_TIMEOUT) as r:
            resp = json.loads(r.read().decode() or '{}')
    except urllib.error.HTTPError as e:
        return None, f'commit HTTP {e.code}'
    except Exception as e:
        return None, f'{type(e).__name__}: {str(e)[:80]}'
    if resp.get('status') in ('accept', 'noop') and resp.get('id'):
        return resp['id'], None
    return None, f"status={resp.get('status')} reason={str(resp.get('reason'))[:80]}"


def commit(summary, changed=None, worked=None, failed=None, why='',
           kind='session', tags=None, session_ref='', stated_by_frank=False, embed=True):
    def _b(label, items):
        items = [i for i in (items or []) if i]
        return f'## {label}\n' + ('\n'.join(f'- {i}' for i in items) if items else '- (none)') + '\n'
    content = (
        _b('What changed', changed) + '\n' +
        _b('What worked', worked) + '\n' +
        _b('What failed', failed) + '\n' +
        f'## Why / lessons\n{why or "(none)"}\n'
    )
    # wq#1634 §4.5: memoryd first (it audits and governs), direct write second.
    # Today the fallback is the primary path for CLI sessions; it must not be the
    # unaudited one.
    fail_str = '; '.join(failed or [])
    # terse recall-tier event so FTS keyword search finds it fast
    ev_text = f'{summary} :: changed={"; ".join(changed or [])} :: failed={fail_str} :: {why}'[:3500]
    nid, fell_back = _memoryd_commit(kind, summary[:200], content, tags, session_ref)
    if nid is None:
        # wq#2772: archival row + audit row + recall_fts event in ONE transaction,
        # committed BEFORE the .md and BEFORE the embed, then read back. Raises
        # mm.NotPersisted instead of returning an id that recall can never find.
        nid, _ = mm.write_note_verified(
            kind, summary[:200], content, tags=tags, source_ref=session_ref,
            audit={'action': minerva_audit.WRITE, 'source_ref': session_ref,
                   'detail': f'lumen_episode.commit fallback ({fell_back}) kind={kind}',
                   'source_kind': minerva_audit.resolve_source_kind(stated_by_frank),
                   'invariant': 'wq1634-fail-open'},
            recall_event=(kind, ev_text, session_ref or None), embed_now=False)
    else:
        rowid = mm.log_event(kind, ev_text, ref=session_ref or nid)
        missing = mm.verify_landed(nid, rowid, require_fts=True)
        if missing:
            raise mm.NotPersisted(f'memoryd-accepted note {nid}: {"; ".join(missing)}')
    direct_write = fell_back is not None
    # Failure Archaeology wire-in (2026-08-14, wq#590): a non-empty --failed
    # auto-creates an fq ledger row. The data was always written; now something
    # reads it. FAIL-OPEN: an episode commit must never die because the failure
    # ledger did (fq itself skips noise-only 'none'/'nothing' entries).
    if fail_str:
        try:
            import os as _os, subprocess as _sp
            if _os.path.exists('/home/lumen/bin/fq'):
                _sp.run(['/home/lumen/bin/fq', 'ingest-episode',
                         '--episode-id', str(nid), '--summary', summary[:200],
                         '--failed', fail_str[:1200], '--why', (why or '')[:400],
                         '--ref', session_ref or ''],
                        timeout=10, capture_output=True)
        except Exception:
            pass
    # wq#576 outcome joiner: attribute this session's logged recall hits to this
    # episode and label the session. FAIL-OPEN for the same reason as the fq
    # wire-in above -- an episode commit must never die because salience did.
    try:
        import lumen_salience as _sal
        _sal.label_outcome(nid, failed_str=fail_str, ref=session_ref)
    except Exception:
        pass
    # wq#2772: embed LAST. The row + FTS event are already durable and verified;
    # a timeout/kill from here on costs only the vector (NULL -> embed guard repairs).
    if embed and direct_write:
        mm.embed_note(nid)
    return nid

def recall(query, top_k=6):
    # Memory V3 P2 (2026-08-07): hybrid indexed recall (dense+lex+graph+recency).
    # A/B on 56-query golden set: R@1 .839->.911, R@5 .929->1.000, MRR .884->.943,
    # +23ms p50. Rollback: MEMORY_V3_RECALL=0 (or search_v3 fail-opens to v2 itself).
    import os
    if os.environ.get('MEMORY_V3_RECALL', '1') != '0':
        try:
            from lumen_recall2 import search_v3
            return search_v3(query, top_k=top_k)
        except Exception as e:
            # wq#465: a stderr line alone let a v3-wide outage run unnoticed for
            # hours (every precheck fleet-wide silently degraded to v2 recall:
            # R@1 .911 -> .839, and ~2x latency because the query is embedded
            # twice). Fail LOUD: drop a breadcrumb the guard timer picks up, and
            # honour MEMORY_V3_STRICT=1 for tests/CI that must not tolerate it.
            print(f'[lumen_episode] v3 recall failed ({e}); falling back to v2',
                  file=sys.stderr)
            _breadcrumb_v3_failure(e)
            if os.environ.get('MEMORY_V3_STRICT') == '1':
                raise
    return mm.search(query, top_k=top_k, tiers=['recall', 'archival'])


V3_FAIL_MARKER = '/home/lumen/minerva/logs/v3_recall_failure.json'


def _breadcrumb_v3_failure(exc):
    """Persist last v3 failure so memory_embed_guard.py can alarm on it.

    Best-effort and never raises -- recall must not die because logging did.
    """
    try:
        import json, os
        from datetime import datetime, timezone
        prev = {}
        if os.path.exists(V3_FAIL_MARKER):
            try:
                with open(V3_FAIL_MARKER) as f:
                    prev = json.load(f)
            except Exception:
                prev = {}
        os.makedirs(os.path.dirname(V3_FAIL_MARKER), exist_ok=True)
        with open(V3_FAIL_MARKER, 'w') as f:
            json.dump({'last_error': f'{type(exc).__name__}: {exc}'[:400],
                       'last_ts': datetime.now(timezone.utc).isoformat(),
                       'count': int(prev.get('count', 0)) + 1}, f)
    except Exception:
        pass

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
    ext = '[ext] ' if (h.get('trust') == 'external'
                       or str(h.get('ref') or '').startswith('messenger:')) else ''  # P5 quarantine marker
    return f'{mark}[{h["tier"]}/{h.get("kind","")}] {h.get("score",0):.2f}  {ext}{h.get("title","")[:80]}'

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
    pc.add_argument('--stated-by-frank', action='store_true',
                    help='wq#1634: Frank asserted this himself -> source_kind=frank_stated')
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
        # wq#2772: FAIL LOUD. Exit 3 + stderr when the row or its FTS event is not
        # in minerva.db; the "committed" line is printed only after read-back, and
        # BEFORE the embed, so a caller timeout during embed still saw the truth.
        try:
            nid = commit(a.summary, changed=sp(a.changed), worked=sp(a.worked),
                         failed=sp(a.failed), why=a.why, kind=a.kind,
                         tags=sp(a.tags), session_ref=a.ref,
                         stated_by_frank=a.stated_by_frank, embed=False)
        except mm.NotPersisted as e:
            print(f'EPISODE NOT PERSISTED: {e}', file=sys.stderr, flush=True)
            sys.exit(3)
        print(f'committed episode {nid} (db-verified: archival_notes + recall_fts)', flush=True)
        mm.embed_note(nid)

if __name__ == '__main__':
    _main()
