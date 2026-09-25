#!/usr/bin/env python3
"""
lumen_reflector.py - Memory V2 Phase 3 (richer): LLM sleep-time reflector. PROPOSAL-ONLY.

Reads recent episodic notes + the latest dream report + memory growth stats, asks
deepseek-v4-pro via the LiteLLM gateway (NEVER messenger-chat/messenger-re) to reflect
and PROPOSE: (a) durable lessons worth committing to the episodic store, (b) claude_memory.md
consolidations / merges / prunes. Nothing is auto-applied. Output is a dated markdown report
+ JSON sidecar under dream/reflections/ for Frank to review and promote manually (DGM-style).

HARD GUARANTEES
  - NEVER mutates claude_memory.md, the bi-temporal state store, or the episodic store.
  - The ONLY filesystem writes are inside dream/reflections/.
  - Model pinned to a non-messenger DeepSeek entry (asserted at import).
  - Fails LOUD on LLM error: exits non-zero + ntfy alert, writes NO report (a silent/empty
    reflection must never masquerade as "nothing to improve").
"""
import os, sys, re, json, glob, sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT      = Path('/home/lumen/minerva')
DB        = ROOT / 'minerva.db'
MEMORY    = Path('/home/lumen/claude_memory.md')
DREAM_DIR = ROOT / 'dream'
OUT_DIR   = DREAM_DIR / 'reflections'
ENV_FILE  = Path('/home/lumen/agent/.env')

# wq#2573: this unit is Type=oneshot; ExecStartPost= (the wq#1658
# `.stamp.<unit>` writer) never runs when ExecStart exits non-zero, so a
# fail-loud exit here was invisible to cron_deadman.py's age-based staleness
# check for up to 2x the weekly timer's cadence (14 days) — that gap, not a
# bug in the fail-loud exit itself, is why the 2026-09-20 failure sat
# unticketed for 5 days. Write our own liveness + status signal on every
# exit path so cron_deadman.py (wq#2573 companion change) can page on a
# reported failure immediately instead of waiting on stamp age.
STAMP_DIR  = Path('/home/lumen/logs')
RUN_STATE  = STAMP_DIR / '.state.lumen-reflector'
RUN_STAMP  = STAMP_DIR / '.stamp.lumen-reflector'

LITELLM_URL   = 'http://localhost:4000/v1/chat/completions'
REFLECT_MODEL = os.getenv('REFLECT_MODEL', 'deepseek-v4-pro')
N_EPISODES    = int(os.getenv('REFLECT_N_EPISODES', '15'))
MAX_TOKENS    = int(os.getenv('REFLECT_MAX_TOKENS', '6000'))
TEMP          = float(os.getenv('REFLECT_TEMP', '0.3'))
NTFY_URL      = os.getenv('NTFY_URL_ALERTS', 'http://localhost:8080/lumen-updates')

# Never reflect through the messenger's isolated brain entries (bug #22249 isolation + mandate).
assert not REFLECT_MODEL.startswith('messenger'), 'reflector must NOT use messenger-chat/re'

# Surface Pro refurb business is strictly off-limits for AI/Lumen integration.
OFF_LIMITS = re.compile(r'surface\s*pro|refurb', re.I)


def _now_pt():
    return datetime.now(timezone.utc) - timedelta(hours=7)  # PDT


def _load_key():
    if os.getenv('LITELLM_MASTER_KEY'):
        return os.environ['LITELLM_MASTER_KEY']
    try:
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith('LITELLM_MASTER_KEY='):
                return line.split('=', 1)[1].strip().strip('"\'')
    except Exception:
        pass
    return ''


def recent_episodes(n=N_EPISODES):
    con = sqlite3.connect(str(DB))
    try:
        rows = con.execute(
            'SELECT created_at, kind, title, content, tags, source_ref '
            'FROM archival_notes ORDER BY created_at DESC LIMIT ?', (n,)).fetchall()
    finally:
        con.close()
    return [{'created_at': r[0] or '', 'kind': r[1], 'title': r[2] or '',
             'content': r[3] or '', 'tags': r[4] or '', 'ref': r[5] or ''} for r in rows]


def latest_dream():
    files = sorted(glob.glob(str(DREAM_DIR / 'dream-*.md')))
    if not files:
        return ''
    try:
        return Path(files[-1]).read_text()[:4000]
    except Exception:
        return ''


def growth():
    try:
        text = MEMORY.read_text()
        return text.count('\n') + 1, len(re.findall(r'(?m)^#{1,3}\s', text))
    except Exception:
        return 0, 0


def build_prompt(eps, dream, lines, sections):
    block = []
    for e in eps:
        body = re.sub(r'\s+', ' ', e['content']).strip()[:700]
        block.append(f"- [{(e['created_at'])[:10]}] ({e['kind']}) {e['title']}\n    {body}")
    ep_text = '\n'.join(block) if block else '(no episodes recorded yet)'
    # Ability/proficiency decomposition (prop ecbc3e3172b5) — flag-gated
    # (A5_ABILITY_PROFICIENCY=1), fail-open '' when off/absent.
    try:
        import sys as _s; _s.path.insert(0, '/home/lumen/autonomy/agent5')
        from ability_proficiency import reflector_context as _apc
        ap = _apc()
        if ap:
            ep_text += '\n\n' + ap
    except Exception:
        pass
    return f'''You are Lumen's memory-consolidation reflector, running sleep-time and OFFLINE from the user.
REFLECT on recent operational episodes and PROPOSE improvements to long-term memory. You do NOT
apply anything; a human reviews and promotes. Be precise, skeptical, and concrete.

CONTEXT
claude_memory.md is currently {lines} lines / ~{sections} sections (grows unbounded; consolidation has value).
A rule-based pass already handles stale-IP detection and episode dedup; do NOT duplicate that work.
Focus on higher-order reflection the rule pass cannot do.

RECENT EPISODES (newest first):
{ep_text}

LATEST RULE-BASED DREAM REPORT (situational awareness):
{dream or '(none)'}

Propose items ONLY where genuinely warranted (quality over quantity; an empty list is fine):
  - "lesson"        : a durable, generalized lesson distilled from episodes (esp. recurring failures). Give exact text to store.
  - "consolidation" : scattered claude_memory.md notes that should merge into one canonical section. Name topic + what to merge.
  - "prune"         : content now obsolete/superseded and safe to archive. Name it + why it is safe.
  - "pattern"       : a cross-episode pattern or systemic risk worth flagging to the operator.

Return ONLY a JSON object, no prose, no markdown fences:
{{"summary":"<=2 sentence overview",
  "proposals":[
    {{"type":"lesson|consolidation|prune|pattern","title":"short title",
      "target":"file/section/episode concerned, or 'episodic-store'",
      "rationale":"why it matters, grounded in the episodes above",
      "proposed_action":"the exact change or text to store","confidence":0.0}}
  ]}}'''


def _post(prompt, key):
    # 2026-08-02: was a bare requests.post(timeout=180) with NO network exception
    # handling — one transient LiteLLM stall raised ReadTimeout straight out of
    # main() and left lumen-reflector.service permanently failed (08-02 18:33).
    # Now: 300s timeout, 3 tries, linear backoff. Same fix as lumen_news_digest.
    import requests, time as _t
    last = None
    for attempt in range(3):
        try:
            r = requests.post(
                LITELLM_URL,
                headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
                json={'model': REFLECT_MODEL, 'messages': [{'role': 'user', 'content': prompt}],
                      'temperature': TEMP, 'max_tokens': MAX_TOKENS,
                      'response_format': {'type': 'json_object'}},
                timeout=300)
            break
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError) as e:
            last = e
            if attempt < 2:
                _t.sleep(10 * (attempt + 1))
    else:
        return None, f'network_after_3_tries: {last}'
    if r.status_code != 200:
        return None, f'http_{r.status_code}: {r.text[:200]}'
    txt = (r.json()['choices'][0]['message'].get('content') or '').strip()
    if not txt:
        return None, 'empty_content (reasoning model returned no content; raise REFLECT_MAX_TOKENS)'
    return txt, None


def _extract_json(txt):
    """Tolerant parse: strip fences, grab outermost object, repair common LLM slips (trailing commas)."""
    t = txt.strip()
    if t.startswith('```'):
        t = re.sub(r'^```[a-zA-Z]*\n?', '', t).rsplit('```', 1)[0]
    m = re.search(r'\{[\s\S]*\}', t)
    if not m:
        return None
    seg = m.group(0)
    for cand in (seg, re.sub(r',(\s*[}\]])', r'\1', seg)):
        try:
            return json.loads(cand)
        except Exception:
            continue
    return None


def call_llm(prompt, key):
    # JSON mode + tolerant parse, then ONE corrective retry, then fail-loud.
    txt, err = _post(prompt, key)
    if err:
        return None, err
    obj = _extract_json(txt)
    if obj is not None:
        return obj, None
    txt2, err2 = _post(prompt + '\n\nIMPORTANT: your previous reply was not valid JSON. Reply with a '
                                'SINGLE strict JSON object only, no prose or fences. Escape every quote '
                                'and newline inside string values.', key)
    if err2:
        return None, f'retry_{err2}'
    obj = _extract_json(txt2)
    if obj is not None:
        return obj, None
    return None, f'bad_json_after_retry: {txt2[:200]}'

def alert(msg):
    try:
        import requests
        requests.post(NTFY_URL, data=msg.encode(),
                      headers={'Title': 'lumen_reflector FAILED', 'Priority': 'high', 'Tags': 'warning'},
                      timeout=5)
    except Exception as e:
        # wq#2573: this used to be a bare `pass` — a down/misconfigured ntfy
        # made the ONLY failure signal disappear with zero trace. Now at
        # least journalctl -u lumen-reflector shows it, and write_run_state()
        # below is a second, ntfy-independent channel for the deadman.
        print(f'lumen_reflector: alert() failed to reach ntfy: {e}', file=sys.stderr)


def write_run_state(status, detail=''):
    """wq#2573: record status ('ok'|'error') for cron_deadman.py's
    immediate-page path, and unconditionally refresh the liveness stamp that
    ExecStartPost would have written on success -- see module docstring."""
    try:
        STAMP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = RUN_STATE.with_suffix('.tmp')
        tmp.write_text(json.dumps({'status': status, 'detail': str(detail)[:500],
                                    'ts': datetime.now(timezone.utc).isoformat()}))
        tmp.replace(RUN_STATE)
        RUN_STAMP.touch()
    except OSError as e:
        print(f'lumen_reflector: write_run_state failed: {e}', file=sys.stderr)


def main():
    """wq#2596: the guard, not the body. wq#2573 wired write_run_state() into
    the three exit paths _run() takes deliberately (no-key, LLM error,
    success), which left every *undeliberate* exit uncovered: recent_episodes()
    does a bare sqlite3.connect() ('database is locked' is a routine failure),
    _post() indexes r.json()['choices'][0]['message'] on faith, and OUT_DIR
    .mkdir() can fail on a full disk. Any of those crashed the process with the
    prior run's .state.lumen-reflector still reading status:ok, so
    cron_deadman.py's immediate-page path saw nothing wrong and the unit fell
    back to the 14-day stamp-age blind spot this was all filed to close.
    SystemExit is a BaseException, so _run()'s own sys.exit() calls pass
    through here untouched -- their state is already written."""
    try:
        _run()
    except Exception as e:
        write_run_state('error', f'uncaught {type(e).__name__}: {e}')
        alert(f'lumen_reflector: crashed with uncaught {type(e).__name__}: {e} '
              '(see journalctl -u lumen-reflector for the traceback).')
        raise


def _run():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    key = _load_key()
    if not key:
        write_run_state('error', 'no LITELLM_MASTER_KEY')
        alert('lumen_reflector: no LITELLM_MASTER_KEY; aborted (no report written).')
        print('FAIL: no LITELLM_MASTER_KEY', file=sys.stderr); sys.exit(2)

    eps = recent_episodes()
    dream = latest_dream()
    lines, sections = growth()
    obj, err = call_llm(build_prompt(eps, dream, lines, sections), key)
    if err:
        write_run_state('error', err)
        alert(f'lumen_reflector: LLM error ({err}) via {REFLECT_MODEL}; no report written (fail-loud).')
        print(f'FAIL: {err}', file=sys.stderr); sys.exit(1)

    props = obj.get('proposals') or []
    actionable, withheld = [], []
    for p in props:
        (withheld if OFF_LIMITS.search(json.dumps(p, ensure_ascii=False)) else actionable).append(p)

    day = _now_pt().strftime('%Y-%m-%d')
    stamp = _now_pt().strftime('%Y-%m-%d %H:%M PT')
    md = OUT_DIR / f'reflection-{day}.md'
    js = OUT_DIR / f'reflection-{day}.json'

    L = [f'# Lumen LLM reflection - {stamp}', '',
         f'_Model: `{REFLECT_MODEL}` (DeepSeek via LiteLLM). **PROPOSAL-ONLY - nothing auto-applied.** Review and promote manually._', '',
         f'_Inputs: {len(eps)} recent episodes - claude_memory.md {lines} lines / ~{sections} sections._', '',
         '## Summary', obj.get('summary', '(none)'), '',
         f'## Proposals ({len(actionable)})']
    if not actionable:
        L.append('_None - reflector found nothing worth proposing this cycle._')
    for i, p in enumerate(actionable, 1):
        L += [f'### {i}. [{p.get("type","?")}] {p.get("title","(untitled)")}  (confidence {p.get("confidence","?")})',
              f'- **Target:** {p.get("target","?")}',
              f'- **Rationale:** {p.get("rationale","")}',
              f'- **Proposed action:** {p.get("proposed_action","")}', '']
    if withheld:
        L += [f'## Withheld - off-limits ({len(withheld)})',
              '_Dropped automatically (Surface Pro / refurb business is off-limits for AI integration):_']
        L += [f'- {p.get("title","(untitled)")}' for p in withheld]
    L += ['', '---',
          '_To promote: lessons -> `lumen_episode.py commit ...`; memory edits -> hand-apply to claude_memory.md after backup._']
    md.write_text('\n'.join(L))
    js.write_text(json.dumps({'generated': stamp, 'model': REFLECT_MODEL,
                              'summary': obj.get('summary', ''),
                              'proposals': actionable, 'withheld': withheld}, indent=2))
    write_run_state('ok', f'{len(actionable)} actionable, {len(withheld)} withheld')
    print(f'wrote {md}')
    print(f'  proposals: {len(actionable)} actionable, {len(withheld)} withheld(off-limits) | episodes_in: {len(eps)}')


if __name__ == '__main__':
    main()
