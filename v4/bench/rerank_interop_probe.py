#!/usr/bin/env python3
"""wq#596 — decompose the 247ms(#569) vs 372ms(live service) gap at 16 threads.

Transient load-bench-unload in the PRODUCTION venv. Isolates:
  - in-process vs over-HTTP  (this script is in-process)
  - inter_op=1 (our pin) vs inter_op default
Same seed/corpus/pool construction as rerank_pool20_bench.py.
Env: BENCH_INTRA (default 16), BENCH_INTER (0 = leave torch default).
"""
import json, os, random, sqlite3, statistics, sys, time

INTRA = int(os.environ.get("BENCH_INTRA", "16"))
INTER = int(os.environ.get("BENCH_INTER", "1"))
for v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[v] = str(INTRA)
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import torch
torch.set_num_threads(INTRA)
if INTER > 0:
    try:
        torch.set_num_interop_threads(INTER)
    except RuntimeError as e:
        print("interop pin failed:", e)

from sentence_transformers import CrossEncoder

DB = "/home/lumen/minerva/minerva.db"
con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
rows = con.execute("SELECT title, content FROM archival_notes "
                   "WHERE content IS NOT NULL AND length(content) > 300 ORDER BY id").fetchall()
con.close()

rnd = random.Random(596)
idx = list(range(len(rows)))
cases = []
for _ in range(21):
    pool = rnd.sample(idx, 20)
    q_i = rnd.choice(pool)
    q = (rows[q_i][0] or rows[q_i][1][:80])[:2000]
    cases.append((q, [rows[i][1][:2000] for i in pool]))

m = CrossEncoder("tomaarsen/ms-marco-ettin-17m-reranker")
lat = []
for n, (q, texts) in enumerate(cases):
    pairs = [[q, t] for t in texts]
    t0 = time.time()
    m.predict(pairs)
    ms = (time.time() - t0) * 1000
    if n > 0:
        lat.append(ms)
s = sorted(lat)
print(json.dumps({"intra": INTRA, "inter_requested": INTER,
                  "torch_intra": torch.get_num_threads(),
                  "torch_inter": torch.get_num_interop_threads(),
                  "n": len(lat), "p50_ms": round(s[len(s) // 2], 1),
                  "mean_ms": round(statistics.fmean(lat), 1),
                  "min_ms": round(min(lat), 1), "max_ms": round(max(lat), 1)}))
