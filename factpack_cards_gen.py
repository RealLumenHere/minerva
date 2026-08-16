#!/usr/bin/env python3
"""Capability-card generator for minerva_factpack (Phase 1, 2026-07-28).

WHY: 13 of the goldv2 primary-rejects are TRUE mechanism-absence claims
("Memory V2 has no future-relevance scoring") that no unit/model/table
enumeration can entail — the evidence lives inside module source. Cards are
per-module mechanism inventories generated FROM the source, hash-keyed so a
card regenerates only when its file changes.

GROUND-TRUTH DISCIPLINE: cards feed the hallucination guard, so a wrong card
poisons the judge. Guards here: (1) the generation prompt contains ONLY the
source file, no other context to import errors from; (2) absences must be
mechanisms a reader would plausibly assume, not exhaustive negatives; (3)
every card carries source hash + date; (4) deployment is gated on a
verify_gold_v2 rescore holding precision at 1.00, same as the factpack itself
was. Cards are STAGED to factpack_cards.json and only consumed by
minerva_factpack when FACTPACK_CARDS=1 (default off until the gate passes).
"""
import os, json, time, hashlib, requests
from pathlib import Path

OUT = Path('/home/lumen/minerva/factpack_cards.json')
LITELLM = 'http://localhost:4000/v1/chat/completions'
KEY = [l.split('=', 1)[1].strip().strip('"') for l in open('/home/lumen/agent/.env')
       if l.startswith('LITELLM_MASTER_KEY=')][0]
MODEL = os.getenv('CARDS_MODEL', 'cerebras-oss-120b')

TARGETS = [
    # memory subsystem (goldv2 items 22, 27, 29)
    '/home/lumen/minerva/lumen_dream.py',
    '/home/lumen/minerva/lumen_reflector.py',
    '/home/lumen/minerva/lumen_convo_learn.py',
    '/home/lumen/minerva/lumen_episode.py',
    '/home/lumen/minerva/lumen_state.py',
    # retrieval (23, 24, 28)
    '/home/lumen/minerva/retrieval/index_research.py',
    '/home/lumen/ab_rag/rerank/rerank_server.py',
    # context producers / formats (26)
    '/home/lumen/minerva/lumen_scout_expanded.py',
    '/home/lumen/agent/lumen_intel.py',
    '/home/lumen/agent/signal_engine.py',
    # prompt / persona (15)
    '/home/lumen/agent/prompt_engine.py',
    '/home/lumen/agent/persona_engine.py',
    # agent5 pipeline (2, 27)
    '/home/lumen/autonomy/agent5/planner.py',
    '/home/lumen/autonomy/agent5/generator.py',
    '/home/lumen/autonomy/agent5/evaluator.py',
    # eval / serving internals (13, 14, 25)
    '/home/lumen/minerva/minerva_eval.py',
    '/etc/systemd/system/llama-qwen36.service',
]

PROMPT = """You are indexing one source file for a fact-checking system that \
verifies claims about this codebase. Read ONLY the file below. Return ONLY JSON:
{"present": ["short factual line per mechanism this file actually implements"],
 "absent": ["short line per mechanism a reader might plausibly assume exists here but which this file clearly does NOT implement"]}
Rules: every line must be checkable against this file alone; <=10 present, <=6 absent; \
name concrete mechanisms (scoring, caching, pruning, schemas, training, compression), \
not vague qualities. File: {path}

<source>
{src}
</source>"""


def card(path):
    src = Path(path).read_text(errors='ignore')[:14000]
    h = hashlib.sha256(src.encode()).hexdigest()[:16]
    body = {'model': MODEL,
            'messages': [{'role': 'user',
                          'content': PROMPT.replace('{path}', path).replace('{src}', src)}],
            'temperature': 0.1, 'max_tokens': 1500,
            'response_format': {'type': 'json_object'}}
    for wait in (0, 20, 45):
        if wait:
            time.sleep(wait)
        try:
            r = requests.post(LITELLM, headers={'Authorization': f'Bearer {KEY}'},
                              json=body, timeout=120)
            if r.status_code != 200:
                continue
            txt = r.json()['choices'][0]['message']['content']
            d = json.loads(txt)
            if isinstance(d.get('present'), list) and isinstance(d.get('absent'), list):
                return {'hash': h, 'generated': time.strftime('%Y-%m-%d'),
                        'present': [str(x)[:160] for x in d['present'][:10]],
                        'absent': [str(x)[:160] for x in d['absent'][:6]]}
        except Exception:
            continue
    return None


def main():
    cards = json.loads(OUT.read_text()) if OUT.exists() else {}
    for p in TARGETS:
        if not Path(p).exists():
            print(f'SKIP missing {p}', flush=True)
            continue
        h = hashlib.sha256(Path(p).read_text(errors='ignore')[:14000].encode()).hexdigest()[:16]
        if cards.get(p, {}).get('hash') == h:
            print(f'cached  {p}', flush=True)
            continue
        c = card(p)
        if c:
            cards[p] = c
            OUT.write_text(json.dumps(cards, indent=1))
            print(f'CARDED  {p}: {len(c["present"])} present / {len(c["absent"])} absent', flush=True)
        else:
            print(f'FAILED  {p} (left uncarded — factpack simply has no card line for it)', flush=True)
    print(f'done: {len(cards)} cards in {OUT}', flush=True)


if __name__ == '__main__':
    main()
