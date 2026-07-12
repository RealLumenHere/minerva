#!/usr/bin/env python3
"""reclassify_backfill.py — re-score classified_intel rows poisoned by the
dead Cerebras model (http_404 since 2026-05-28, http_429 era 05-20..05-27).
Uses the patched gateway-routed classify_cerebras(). Keeps original ts,
sets processed=0 so the proposal generator reconsiders high-self items.
One-shot. Safe to re-run (only touches rows still carrying fallback markers).
Frank-approved 2026-06-07.
"""
import sqlite3, sys, time
sys.path.insert(0, '/home/lumen/minerva')
import minerva_classifier as mc

DB = '/home/lumen/minerva/minerva.db'
SEL = ("SELECT id, source, title FROM classified_intel "
       "WHERE ts >= '2026-05-20' AND (reasoning LIKE 'http_%' "
       "OR reasoning LIKE 'err:%' OR reasoning IN ('no_key','no_json'))")

conn = sqlite3.connect(DB, timeout=30)
rows = conn.execute(SEL).fetchall()
total = len(rows)
print(f'backfill start: {total} poisoned rows', flush=True)

ok = fb = hi = 0
for n, (iid, source, title) in enumerate(rows, 1):
    item = {'id': iid, 'source': source, 'title': title, 'content': title}
    r = mc.classify_cerebras(item)
    if r['reasoning'].startswith(('http_', 'err:', 'no_key', 'no_json')):
        time.sleep(1.0)
        r = mc.classify_cerebras(item)  # one retry — sporadic no_json/429 is normal
    if r['reasoning'].startswith(('http_', 'err:', 'no_key', 'no_json')):
        fb += 1
        if fb <= 5:
            print(f'fallback row {n} ({iid[:12]}): {r["reasoning"][:60]}', flush=True)
        if n == 10 and fb == 10:
            print('ABORT: first 10 calls ALL fell back — backend broken.', flush=True)
            sys.exit(1)
    else:
        reasoning = r['reasoning'] + ((' | HINT: ' + r['adoption_hint']) if r.get('adoption_hint') else '')
        conn.execute(
            "UPDATE classified_intel SET audience_relevance=?, self_relevance=?, "
            "category=?, reasoning=?, processed=0 WHERE id=?",
            (r['audience_relevance'], r['self_relevance'], r['category'], reasoning[:900], iid))
        ok += 1
        if r['self_relevance'] >= 0.6:
            hi += 1
    if n % 25 == 0:
        conn.commit()
        print(f'progress {n}/{total} ok={ok} fallback={fb} high_self={hi}', flush=True)
    time.sleep(0.25)

conn.commit()
conn.close()
print(f'DONE: {total} rows | rescored={ok} fallback={fb} high_self(>=0.6)={hi}', flush=True)
