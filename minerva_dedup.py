#!/usr/bin/env python3
"""minerva_dedup — near-duplicate suppression for the Minerva proposal pipeline.

WHY (audited 2026-08-14): 33 of 266 defers in four days were the same proposal
re-derived from different feeds - "add model X to LiteLLM" arrived from Phoronix,
HuggingFace, VentureBeat and The Decoder as four separate items, each burning a
reasoning-model call and each landing in the defer pile. Nothing deduped them,
because dedup only existed for research PROMPTS (agent/dedup_prompts.py), never
for intel -> proposals.

Two independent layers, cheapest first:
  1. route_conflict()  deterministic: a proposal to add a LiteLLM route whose
                       model_name already exists in litellm_config.yaml is a
                       duplicate by definition, no model call needed.
  2. check()           semantic: nomic-embed-text (local Ollama, no API spend)
                       cosine against every proposal-generating item from the
                       last WINDOW_DAYS. Threshold is calibrated, not guessed -
                       see calibrate() and reports/dedup_calibration.

Fail-open everywhere: any error returns "not a duplicate", so dedup can never
block the pipeline it is protecting.
"""
import json, os, re, sqlite3, struct, sys, time
import urllib.request
from pathlib import Path

DB = '/home/lumen/minerva/minerva.db'
OLLAMA = os.getenv('OLLAMA_URL', 'http://localhost:11434')
EMBED_MODEL = os.getenv('DEDUP_EMBED_MODEL', 'nomic-embed-text')
# CALIBRATED 2026-08-14 on 400 real proposals (reports/dedup_calibration_20260814.md):
#   random pairs      p50 .580  p90 .670  p99 .743  max .798
#   true duplicates   .82 - .91   (same model arriving from 4 different feeds)
# Cosine ALONE is not enough: "Inkling-Small GGUF into LiteLLM" vs "MiniMax-H3 GGUF
# into LiteLLM" scores .902 and they are different models - the boilerplate dominates
# the sentence. So a duplicate must ALSO share its distinctive entity tokens.
# Measured separation on labelled pairs: true dups share .67-1.00, distinct .00-.50.
THRESHOLD = float(os.getenv('DEDUP_THRESHOLD', '0.85'))
ENTITY_SHARE = float(os.getenv('DEDUP_ENTITY_SHARE', '0.60'))

# Boilerplate that shows up in nearly every adopt-a-model proposal. Stripping it is
# what makes the entity check discriminative.
STOP = set('''add adds adding integrate integrating integration adopt adopting evaluate evaluating
litellm gateway route routes model models modeling pool endpoint endpoints local remote inference
inferencing api apis config configuration yaml service services lumen minerva fleet agent agents
gguf fp8 int8 q4 q8 awq quantized quantization support via into for the and with as a an of on to
new low high cost costs latency throughput coding code research researcher tasks task diversified
diversify diversification fallback provider primary secondary reasoning vision embedding embeddings
scout intel pipeline pipelines use using benchmark benchmarking test testing eval evaluation'''.split())


def entities(text):
    """Distinctive tokens: drop boilerplate, keep anything with a digit or >=4 chars."""
    out = set()
    for w in re.split(r'[^a-z0-9.]+', (text or '').lower()):
        w = w.strip('.')
        if not w or w in STOP:
            continue
        if any(ch.isdigit() for ch in w) or len(w) >= 4:
            out.add(w)
    return out


def entity_share(a, b):
    A, B = entities(a), entities(b)
    if not A or not B:
        return 0.0
    return len(A & B) / min(len(A), len(B))
WINDOW_DAYS = int(os.getenv('DEDUP_WINDOW_DAYS', '45'))
LITELLM_CONFIG = '/home/lumen/agent/litellm_config.yaml'


def db():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def migrate():
    c = db()
    c.execute('''CREATE TABLE IF NOT EXISTS proposal_dedup (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        intel_id TEXT, proposal_id TEXT, title TEXT,
        vec BLOB, created_at TEXT)''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_dedup_created ON proposal_dedup(created_at)')
    c.commit(); c.close()


def embed(text):
    """Local embedding. Returns list[float] or None (never raises)."""
    try:
        body = json.dumps({'model': EMBED_MODEL, 'prompt': text[:2000]}).encode()
        req = urllib.request.Request(f'{OLLAMA}/api/embeddings', data=body,
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=45) as r:
            v = json.load(r).get('embedding')
        return v if v else None
    except Exception:
        return None


def _pack(v):
    return struct.pack(f'{len(v)}f', *v)


def _unpack(b):
    return list(struct.unpack(f'{len(b)//4}f', b))


def _cos(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sa = sb = 0.0
    for x, y in zip(a, b):
        dot += x * y; sa += x * x; sb += y * y
    if sa <= 0 or sb <= 0:
        return 0.0
    return dot / ((sa ** 0.5) * (sb ** 0.5))


def route_conflict(text):
    """(conflict: bool, route_name: str|None) - deterministic LiteLLM dup check."""
    try:
        import yaml
        cfg = yaml.safe_load(open(LITELLM_CONFIG))
        names = [m.get('model_name', '') for m in cfg.get('model_list', []) if m.get('model_name')]
    except Exception:
        return False, None
    low = (text or '').lower()
    if not re.search(r'litellm|model.list|gateway route|model pool', low):
        return False, None
    for n in sorted(names, key=len, reverse=True):
        if len(n) >= 6 and n.lower() in low:
            return True, n
    return False, None


def check(text, threshold=None):
    """(is_dup, match_dict|None, score). Duplicate requires BOTH a high cosine and
    shared entity tokens - see the calibration note on THRESHOLD."""
    threshold = THRESHOLD if threshold is None else threshold
    v = embed(text)
    if not v:
        return False, None, 0.0
    try:
        c = db()
        rows = c.execute(
            "SELECT intel_id, proposal_id, title, vec FROM proposal_dedup "
            "WHERE created_at >= datetime('now', ?)", (f'-{WINDOW_DAYS} days',)).fetchall()
        c.close()
    except Exception:
        return False, None, 0.0
    best, best_s, best_share = None, 0.0, 0.0
    for r in rows:
        s = _cos(v, _unpack(r['vec']))
        if s < threshold:
            continue
        sh = entity_share(text, r['title'])
        if sh >= ENTITY_SHARE and s > best_s:
            best, best_s, best_share = r, s, sh
    if best is not None:
        return True, {'intel_id': best['intel_id'], 'proposal_id': best['proposal_id'],
                      'title': best['title'], 'entity_share': round(best_share, 2)}, best_s
    return False, None, best_s


def record(text, intel_id, proposal_id, title):
    """Add an item to the dedup index. Silent on failure."""
    try:
        v = embed(text)
        if not v:
            return False
        c = db()
        c.execute('INSERT INTO proposal_dedup (intel_id, proposal_id, title, vec, created_at) '
                  "VALUES (?,?,?,?,datetime('now'))",
                  (intel_id, proposal_id, title[:200], _pack(v)))
        c.commit(); c.close()
        return True
    except Exception:
        return False


def backfill(days=14, limit=400):
    """Seed the index from existing proposals so dedup works from the first run."""
    migrate()
    c = db()
    rows = c.execute(
        "SELECT id, source_intel_id, title, proposed_change FROM proposals "
        "WHERE created_at >= datetime('now', ?) ORDER BY created_at DESC LIMIT ?",
        (f'-{days} days', limit)).fetchall()
    have = {r['proposal_id'] for r in c.execute('SELECT proposal_id FROM proposal_dedup')}
    c.close()
    n = 0
    for r in rows:
        if r['id'] in have:
            continue
        if record(f"{r['title']}\n{(r['proposed_change'] or '')[:600]}",
                  r['source_intel_id'], r['id'], r['title'] or ''):
            n += 1
    return n, len(rows)


if __name__ == '__main__':
    migrate()
    if len(sys.argv) > 1 and sys.argv[1] == 'backfill':
        d = int(sys.argv[2]) if len(sys.argv) > 2 else 14
        print('indexed %d of %d proposals' % backfill(d))
    elif len(sys.argv) > 2 and sys.argv[1] == 'check':
        print(check(' '.join(sys.argv[2:])))
    else:
        print(__doc__)
