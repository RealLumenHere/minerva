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

ITEM:
Source: {source}
Title: {title}
Content: {content}

Return only the JSON object.'''

def classify_cerebras(item: dict) -> dict:
    if not LITELLM_KEY:
        return {'audience_relevance':0.5,'self_relevance':0.0,'category':'news','reasoning':'no_key','adoption_hint':''}
    prompt = (PROMPT
            .replace('{source}', str(item.get('source',''))[:80])
            .replace('{title}', str(item.get('title',''))[:300])
            .replace('{content}', str(item.get('content','') or item.get('title',''))[:1500]))
    try:
        r = requests.post(LITELLM_URL,
            headers={'Authorization':f'Bearer {LITELLM_KEY}','Content-Type':'application/json'},
            json={'model':MODEL,'messages':[{'role':'user','content':prompt}],'temperature':0.2,'max_tokens':400},
            timeout=30)
        if r.status_code != 200:
            return {'audience_relevance':0.5,'self_relevance':0.0,'category':'news','reasoning':f'http_{r.status_code}','adoption_hint':''}
        txt = r.json()['choices'][0]['message']['content']
        m = re.search(r'\{[\s\S]*\}', txt)
        if not m:
            return {'audience_relevance':0.5,'self_relevance':0.0,'category':'news','reasoning':'no_json','adoption_hint':''}
        obj = json.loads(m.group(0))
        return {
            'audience_relevance': max(0.0,min(1.0,float(obj.get('audience_relevance',0.5)))),
            'self_relevance':     max(0.0,min(1.0,float(obj.get('self_relevance',0.0)))),
            'category': obj.get('category','news')[:20],
            'reasoning': obj.get('reasoning','')[:400],
            'adoption_hint': obj.get('adoption_hint','')[:500],
        }
    except Exception as e:
        return {'audience_relevance':0.5,'self_relevance':0.0,'category':'news','reasoning':f'err:{e}'[:200],'adoption_hint':''}

def get_unprocessed_intel(limit=40):
    """Pull from lumen-intel intel.db items not yet classified."""
    if not INTEL_DB.exists():
        log(f'intel.db not found at {INTEL_DB}')
        return []
    # Pull recent items
    conn = sqlite3.connect(INTEL_DB)
    cutoff = (datetime.now(timezone.utc)-timedelta(hours=48)).isoformat()
    rows = conn.execute('SELECT id, source, title, score, ts FROM seen_items WHERE ts > ? ORDER BY ts DESC LIMIT ?', (cutoff, limit)).fetchall()
    conn.close()
    # Check which are already classified
    conn = sqlite3.connect(DB)
    classified_ids = set(r[0] for r in conn.execute('SELECT id FROM classified_intel').fetchall())
    conn.close()
    return [{'id':r[0],'source':r[1],'title':r[2],'content':r[2],'ts':r[4]} for r in rows if r[0] not in classified_ids]

def save_classification(item, result):
    conn = sqlite3.connect(DB)
    conn.execute('''INSERT OR REPLACE INTO classified_intel
        (id, source, title, url, audience_relevance, self_relevance, category, reasoning, ts, processed)
        VALUES (?,?,?,?,?,?,?,?,?,0)''',
        (item['id'], item.get('source',''), item.get('title','')[:300], item.get('url',''),
         result['audience_relevance'], result['self_relevance'], result['category'],
         result['reasoning'] + (' | HINT: '+result['adoption_hint'] if result.get('adoption_hint') else ''),
         datetime.now(timezone.utc).isoformat()))
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
        if r['reasoning'].startswith(('http_', 'err:', 'no_key', 'no_json')):
            fallbacks += 1
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
