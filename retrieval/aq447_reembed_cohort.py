#!/usr/bin/env python3
"""aq#447 remediation: re-embed the 2026-07-17 stale cohort in place.

Why: chunks written by the wq#309 bulk backfill on 2026-07-17 carry vectors the
same model no longer reproduces (stored-vs-fresh cos 0.77-0.96); every cohort
from 07-19 onward is 1.0000. Queries embed with today's behaviour, so 45% of the
corpus competes at a systematic cosine handicap. See
agents/work-queue/reports/aq447_embed_drift_investigation.md

Safety:
  - UPDATE of `embedding` only. text/doc_hash/char offsets/indexed_at untouched
    (indexed_at deliberately preserved so the cohort stays identifiable).
  - Backup of all 4831 (id, embedding) pairs:
    /home/lumen/backups/research_chunks_cohort20260717.csv
  - Sequential (one embed at a time). Batching is the leading suspect for the
    original divergence, so this must NOT be parallelised.
  - Commits every 50 rows; resumable — re-running only touches rows whose stored
    vector still disagrees with a fresh embed.
  - Verifies each new vector is 768-d before writing; skips and logs otherwise.
"""
import json, math, sys, time, urllib.request

sys.path.insert(0, "/home/lumen/minerva/retrieval")

OLLAMA = "http://127.0.0.1:11434"
MODEL = "nomic-embed-text:latest"
COHORT = "2026-07-17"
COMMIT_EVERY = 50


def _env():
    e = {}
    for line in open("/home/lumen/agent/.env"):
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            e[k.strip()] = v.strip().strip('"')
    return e


def embed(text):
    req = urllib.request.Request(
        f"{OLLAMA}/api/embeddings",
        data=json.dumps({"model": MODEL, "prompt": text[:7000]}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)["embedding"]


def cos(a, b):
    d = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)); nb = math.sqrt(sum(y * y for y in b))
    return d / (na * nb) if na and nb else 0.0


def main():
    import psycopg2
    e = _env()
    conn = psycopg2.connect(
        host=e["LUMEN_PGVECTOR_HOST"], port=int(e["LUMEN_PGVECTOR_PORT"]),
        dbname=e["LUMEN_PGVECTOR_DB"], user=e["LUMEN_PGVECTOR_USER"],
        password=e["LUMEN_PGVECTOR_PASSWORD"])
    cur = conn.cursor()
    cur.execute("SELECT id, text, embedding FROM research_chunks "
                "WHERE indexed_at::date = %s AND embedding IS NOT NULL "
                "ORDER BY id", (COHORT,))
    rows = cur.fetchall()
    total = len(rows)
    print(f"cohort {COHORT}: {total} chunks", flush=True)
    upd = skip = err = 0
    t0 = time.time()
    for i, (cid, text, stored) in enumerate(rows, 1):
        try:
            v = embed(text)
            if not v or len(v) != 768:
                err += 1
                print(f"  id={cid} BAD SHAPE {len(v) if v else 0} - skipped", flush=True)
                continue
            sv = json.loads(stored) if isinstance(stored, str) else list(stored)
            if cos(v, sv) >= 0.9999:
                skip += 1
                continue
            cur.execute("UPDATE research_chunks SET embedding = %s WHERE id = %s",
                        (json.dumps(v), cid))
            upd += 1
        except Exception as ex:
            err += 1
            print(f"  id={cid} ERROR {str(ex)[:80]}", flush=True)
            continue
        if upd and upd % COMMIT_EVERY == 0:
            conn.commit()
        if i % 100 == 0:
            el = time.time() - t0
            print(f"  {i}/{total} upd={upd} skip={skip} err={err} "
                  f"{i/max(el,1):.2f}/s eta={(total-i)/max(i/max(el,1),0.01)/60:.0f}min",
                  flush=True)
    conn.commit()
    el = time.time() - t0
    print(f"DONE cohort={COHORT} total={total} updated={upd} already_ok={skip} "
          f"errors={err} elapsed={el/60:.1f}min", flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
