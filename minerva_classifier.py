#!/usr/bin/env python3
"""
minerva_classifier.py — Two-axis intel classifier.
Every intel item gets scored on:
  - audience_relevance (blog-post-worthy for homelab/self-hosted readers)
  - self_relevance (Lumen-adoptable: tool/framework/technique we could use)
  - category: tool|framework|paper|technique|news
Powered by Cerebras (fast + free).
"""
import json, re, sqlite3, time, requests, os, hashlib, sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv('/home/lumen/agent/.env')

DB = Path('/home/lumen/minerva/minerva.db')
INTEL_DB = Path('/home/lumen/agent/intel.db')
LOG = Path('/home/lumen/minerva/logs/classifier.log')
CEREBRAS_KEY = os.getenv('CEREBRAS_API_KEY','')  # legacy, unused since 2026-06-07
# 2026-06-07: route via local LiteLLM gateway (model swaps become config-level, calls observable
# in Langfuse). Old direct-Cerebras 'qwen-3-235b-a22b-instruct-2507' was removed upstream
# ~2026-05-28 -> silent http_404 fallbacks, pipeline starved for 10 days.
LITELLM_URL = 'http://localhost:4000/v1/chat/completions'
LITELLM_KEY = os.getenv('LITELLM_MASTER_KEY','')
MODEL = os.getenv('MINERVA_MODEL', 'cerebras-oss-120b')
CLASSIFY_TIMEOUT = int(os.getenv('CLASSIFY_TIMEOUT', '90'))
# MODEL is a REASONING model. At max_tokens=400 it spent ~395 tokens on
# reasoning and returned content=None with finish_reason='length' — no answer
# at all. That single setting produced every no_json/empty_response failure and
# the 123 "expected string or bytes-like object, got 'NoneType'" rows (re.search
# on None). Measured 2026-07-27: 400 -> content None; 1200 -> clean JSON with
# 255 reasoning + answer inside budget.
CLASSIFY_MAX_TOKENS = int(os.getenv('CLASSIFY_MAX_TOKENS', '1200'))

def log(m):
    ts = datetime.now(timezone.utc).strftime('%H:%M:%S UTC')
    line = f'[{ts}] {m}'
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG,'a') as f: f.write(line+'\n')

PROMPT = '''You evaluate AI/tech news items for a homelab-focused blog and for the blog's own autonomous AI agent named Lumen.

Lumen is: an autonomous Python-based AI research agent stack on Ubuntu 26.04, Dell T5810, 64GB RAM. All inference via a local LiteLLM gateway (Cerebras/SambaNova/Mistral/DeepSeek/Gemini/OpenRouter + local Ollama gemma4 12B). HAS the Minerva self-improvement layer: classifier, proposal generator, eval gate, hallucination guard (minerva_verify), skills table, archival memory, pgvector research index. systemd pipelines: scout, intel, signal engine, prompt engine, fleet researchers. NO publishing/CMS — research-only. Target audience for audience_relevance: r/homelab, r/selfhosted, developers running local AI.

For the item below, return ONLY a JSON object:
{
  "audience_relevance": <0.0-1.0>,  // Would r/homelab readers click a post on this?
  "self_relevance": <0.0-1.0>,       // Could Lumen adopt this to improve itself?
  "category": "tool|framework|paper|technique|news",
  "reasoning": "<1-2 sentence justification, <200 chars>",
  "adoption_hint": "<if self_relevance>=0.6, 1 sentence on HOW Lumen could use it; else empty>"
}

Scoring guide:
- audience_relevance 0.9+: concrete tool/tutorial homelab users want. 0.5: interesting news. 0.2: too academic. 0.0: irrelevant to homelab.
- self_relevance 0.9+: a framework/library Lumen could integrate (memory system, agent framework, eval tool, verification library). 0.6: a technique Lumen could implement from scratch. 0.3: inspiration only. 0.0: not applicable (e.g., image gen when Lumen is text-only).
- OVERRIDE: if the item concerns something on the RUNNING STACK list below, self_relevance is 0.8+ regardless of the guide above. A release or CVE in software the fleet already operates is directly actionable — upgrade, patch, or config change — not merely "inspiration". Do not score it 0.0 just because it is not a new thing to adopt.

RUNNING STACK (software this fleet operates today):
{stack}

ITEM:
Source: {source}
Title: {title}
Content: {content}

Return only the JSON object.'''


def _stack_line():
    """What the fleet actually runs. Without this the classifier scored a Gitea
    CVE at self=0.00 because its Lumen description never mentioned Gitea."""
    try:
        repos = json.loads(Path('/home/lumen/agent/fleet_stack.json').read_text())['repos']
        return '; '.join(f"{r.split('/')[-1]} ({w})" for r, w in repos.items())[:2000]
    except Exception:
        return '(stack list unavailable)'

def _failed(reason):
    """A FAILED call is not a verdict.

    Claude 2026-07-26: every error path used to return a well-formed result with
    self_relevance=0.0, which persisted as if the model had judged the item
    irrelevant. Because the proposal generator requires self>=0.6, any item whose
    classify call timed out was silently and permanently disqualified. 1,228
    LiteLLM read timeouts were buried this way. Callers must check 'failed' and
    leave the item UNCLASSIFIED so a later run retries it."""
    return {'audience_relevance':0.5,'self_relevance':0.0,'category':'news',
            'reasoning':str(reason)[:200],'adoption_hint':'','failed':True}

def classify_cerebras(item: dict) -> dict:
    if not LITELLM_KEY:
        return _failed('no_key')
    prompt = (PROMPT
            .replace('{stack}', _stack_line())
            .replace('{source}', str(item.get('source',''))[:80])
            .replace('{title}', str(item.get('title',''))[:300])
            .replace('{content}', str(item.get('content','') or item.get('title',''))[:2500]))
    try:
        # 30s was too tight for cerebras-oss-120b under load: 1,228 read
        # timeouts recorded. Retry transient failures instead of burning the item.
        r = None
        for attempt, wait in enumerate((2, 5, 0)):
            try:
                r = requests.post(LITELLM_URL,
                    headers={'Authorization':f'Bearer {LITELLM_KEY}','Content-Type':'application/json'},
                    json={'model':MODEL,'messages':[{'role':'user','content':prompt}],
                          'temperature':0.2,'max_tokens':CLASSIFY_MAX_TOKENS,
                          # structurally prevents no_json, which was the single
                          # largest failure mode here. minerva_verify already
                          # carried this (aq#66); the classifier never got it.
                          'response_format':{'type':'json_object'}},
                    timeout=CLASSIFY_TIMEOUT)
                if r.status_code == 200:
                    break
            except requests.exceptions.RequestException as e:
                if not wait:
                    return _failed(f'err:{e}')
            if wait:
                time.sleep(wait)
        if r is None:
            return _failed('no_response')
        if r.status_code != 200:
            return _failed(f'http_{r.status_code}')
        txt = r.json()['choices'][0]['message']['content']
        if not txt:
            return _failed('empty_response')
        # guard: re.search on None raised "expected string or bytes-like
        # object, got 'NoneType'" and poisoned 123 rows.
        m = re.search(r'\{[\s\S]*\}', str(txt))
        if not m:
            return _failed('no_json')
        obj = json.loads(m.group(0))
        return {
            'audience_relevance': max(0.0,min(1.0,float(obj.get('audience_relevance',0.5)))),
            'self_relevance':     max(0.0,min(1.0,float(obj.get('self_relevance',0.0)))),
            'category': obj.get('category','news')[:20],
            'reasoning': obj.get('reasoning','')[:400],
            'adoption_hint': obj.get('adoption_hint','')[:500],
        }
    except Exception as e:
        return _failed(f'err:{e}')

def get_unprocessed_intel(limit=40):
    """Pull from lumen-intel intel.db items not yet classified."""
    if not INTEL_DB.exists():
        log(f'intel.db not found at {INTEL_DB}')
        return []
    # Pull recent items
    conn = sqlite3.connect(INTEL_DB)
    # Lookback is env-tunable so a backfill is a first-class operation.
    # NOTE (Claude 2026-07-26): this window is a silent data-loss edge. Anything
    # older than it is invisible to the classifier FOREVER — there is no catch-up
    # pass. At ~100 items/day vs 240/day capacity (40 x 6 runs) that is fine, but
    # a burst day (699 on 2026-07-16) can push items past the horizon unseen.
    _hours = int(os.environ.get('INTEL_LOOKBACK_HOURS', '48'))
    cutoff = (datetime.now(timezone.utc)-timedelta(hours=_hours)).isoformat()
    # Filter already-classified in SQL. Previously LIMIT was applied FIRST and
    # the classified set subtracted after, so a run asking for 100 could return
    # ~50 usable rows and could never page back to an older backlog — the newest
    # N are almost all already done. That made backlog recovery impossible.
    conn.execute("ATTACH DATABASE ? AS m", (str(DB),))
    rows = conn.execute(
        'SELECT s.id, s.source, s.title, s.score, s.ts, s.url, s.summary FROM seen_items s '
        'WHERE s.ts > ? AND s.id NOT IN (SELECT id FROM m.classified_intel) '
        'ORDER BY s.ts DESC LIMIT ?', (cutoff, limit)).fetchall()
    conn.close()
    # content = the actual source material when the fetcher captured it. It used
    # to be a second copy of the title, so every score was a judgement on a
    # headline. Fall back to the title only for pre-migration rows.
    return [{'id': r[0], 'source': r[1], 'title': r[2],
             'content': (r[6] or r[2]), 'ts': r[4],
             'url': (r[5] or ''), 'summary': (r[6] or '')} for r in rows]

def save_classification(item, result):
    conn = sqlite3.connect(DB)
    conn.execute('''INSERT OR REPLACE INTO classified_intel
        (id, source, title, url, audience_relevance, self_relevance, category, reasoning, ts, processed, summary)
        VALUES (?,?,?,?,?,?,?,?,?,0,?)''',
        (item['id'], item.get('source',''), item.get('title','')[:300], item.get('url',''),
         result['audience_relevance'], result['self_relevance'], result['category'],
         result['reasoning'] + (' | HINT: '+result['adoption_hint'] if result.get('adoption_hint') else ''),
         # keep the SOURCE timestamp; stamping now() destroyed publication
         # provenance and made per-day intake stats read as classify-date.
         item.get('ts') or datetime.now(timezone.utc).isoformat(),
         (item.get('summary') or '')[:4000]))
    conn.commit()
    conn.close()

def run(limit=40):
    log(f'=== Classifier run (limit {limit}) ===')
    items = get_unprocessed_intel(limit)
    log(f'Unprocessed items: {len(items)}')
    high_self = 0
    fallbacks = 0
    for i, item in enumerate(items, 1):
        r = classify_cerebras(item)
        if r.get('failed'):
            # NOT saved: an unsaved item stays unclassified and is retried on the
            # next run. Saving it would freeze self_relevance=0.0 forever.
            fallbacks += 1
            log(f'  [{i}/{len(items)}] RETRY-LATER ({r["reasoning"][:60]}) {str(item.get("title",""))[:50]}')
            continue
        # DETERMINISTIC FLOOR: whether the fleet runs a thing is a fact from
        # fleet_stack.json, not something to delegate to an LLM. Without this
        # the classifier scored Gitea/Vaultwarden/netdata CVEs at self=0.00.
        floor = {'Fleet Advisories': 0.9, 'Fleet Releases': 0.7}.get(item.get('source',''))
        if floor and r['self_relevance'] < floor:
            log(f'    floor {r["self_relevance"]:.2f} -> {floor:.2f} (source is the fleet\'s own stack)')
            r['self_relevance'] = floor
        save_classification(item, r)
        if r['self_relevance'] >= 0.6: high_self += 1
        log(f'  [{i}/{len(items)}] aud={r["audience_relevance"]:.2f} self={r["self_relevance"]:.2f} {r["category"]:12s} {item["title"][:60]}')
        time.sleep(0.3)  # gentle on Cerebras
    log(f'Done. High-self-relevance items queued for proposals: {high_self}')
    if items and fallbacks == len(items):
        log(f'ERROR: all {fallbacks} classifications fell back to defaults — LLM backend broken. Failing unit loudly.')
        sys.exit(1)
    if fallbacks:
        log(f'WARNING: {fallbacks}/{len(items)} classifications used fallback defaults.')
    return high_self

if __name__ == '__main__':
    run()
