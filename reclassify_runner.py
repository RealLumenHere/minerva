#!/usr/bin/env python3
"""One-shot recovery run: re-classify the items whose original classify call
failed and was silently persisted as self_relevance=0.0 (2,838 rows purged
2026-07-27). Loops until the unclassified backlog inside the lookback window
is drained, so a single stalled batch cannot end the run early.
"""
import os, sys, time, sqlite3

sys.path.insert(0, '/home/lumen/minerva')
os.environ.setdefault('INTEL_LOOKBACK_HOURS', '2400')   # ~100d, covers 2026-04-22
os.environ.setdefault('CLASSIFY_TIMEOUT', '90')

import minerva_classifier as mc

INTEL = '/home/lumen/agent/intel.db'
MINE = '/home/lumen/minerva/minerva.db'
BATCH = int(os.environ.get('BATCH', '100'))
MAX_ROUNDS = int(os.environ.get('MAX_ROUNDS', '60'))


def backlog():
    hours = int(os.environ['INTEL_LOOKBACK_HOURS'])
    a = sqlite3.connect(INTEL)
    ids = [r[0] for r in a.execute(
        "SELECT id FROM seen_items WHERE ts > datetime('now', ?)",
        (f'-{hours} hours',))]
    a.close()
    b = sqlite3.connect(MINE)
    done = set(r[0] for r in b.execute('SELECT id FROM classified_intel'))
    b.close()
    return len([i for i in ids if i not in done])


start = time.time()
print(f'=== recovery re-classify: backlog={backlog()} batch={BATCH} ===', flush=True)

stalled = 0
for rnd in range(1, MAX_ROUNDS + 1):
    before = backlog()
    if before == 0:
        print('backlog drained', flush=True)
        break
    mc.run(limit=BATCH)
    after = backlog()
    moved = before - after
    print(f'--- round {rnd}: {before} -> {after} (classified {moved}) '
          f'elapsed={int(time.time()-start)}s ---', flush=True)
    # every item in a round can legitimately fail (retry-later); only bail if
    # nothing moves several rounds running, which means a systemic outage.
    stalled = stalled + 1 if moved <= 0 else 0
    if stalled >= 3:
        print('ABORT: 3 consecutive rounds with zero progress — upstream problem',
              flush=True)
        break
    time.sleep(2)

print(f'=== done: backlog={backlog()} elapsed={int(time.time()-start)}s ===',
      flush=True)
