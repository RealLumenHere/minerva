#!/usr/bin/env python3
"""wq#309: populate research_chunks from research_docs.content (additive; never
touches research_docs). Overlap-window chunker NOW; wq#244 upgrades the chunker later.
Windows: 6000 chars, 600 overlap (stride 5400). Per-chunk embed reuses index_research's
script-aware safe_truncate + retry embed(). Resumable: skips (filepath,chunk_ix) whose
doc_hash matches. Usage: chunk_reembed.py [--limit N] [--rate-test]"""
import sys, time, pathlib
sys.path.insert(0, "/home/lumen/minerva/retrieval")
from index_research import _load_env, _conn, embed, sha256_of, log  # reuse calibrated pieces
from psycopg2.extras import execute_values

WIN, OVERLAP = 6000, 600
STRIDE = WIN - OVERLAP

def chunks_of(content):
    n = len(content)
    if n <= WIN:
        yield 0, 0, n, content
        return
    ix, pos = 0, 0
    while pos < n:
        end = min(pos + WIN, n)
        yield ix, pos, end, content[pos:end]
        if end >= n: break
        ix += 1; pos += STRIDE

def main():
    limit = None; rate_test = "--rate-test" in sys.argv
    if "--limit" in sys.argv: limit = int(sys.argv[sys.argv.index("--limit")+1])
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT filepath, chunk_ix, doc_hash FROM research_chunks")
    done = {(r[0], r[1]): r[2] for r in cur.fetchall()}
    # wq#1773: asserted_at rides along with the other denormalised doc columns so a
    # chunk hit can print its as-of date without joining back to research_docs.
    cur.execute("SELECT id, filepath, filename, category, content, content_hash, asserted_at FROM research_docs WHERE content IS NOT NULL ORDER BY id")
    docs = cur.fetchall()
    if limit: docs = docs[:limit]
    log.info(f"wq309 chunker: {len(docs)} docs, {len(done)} chunks already present")
    batch, new, skip, err = [], 0, 0, 0
    t0 = time.time()
    for di, (doc_id, fp, fn, cat, content, chash, asserted) in enumerate(docs, 1):
        for ix, cs, ce, txt in chunks_of(content):
            if done.get((fp, ix)) == chash:
                skip += 1; continue
            emb = embed(txt)
            if emb is None:
                err += 1; continue
            batch.append((doc_id, fp, fn, cat, ix, cs, ce, chash, txt, emb, asserted))
            new += 1
            if len(batch) >= 40:
                execute_values(cur, """INSERT INTO research_chunks
                  (doc_id, filepath, filename, category, chunk_ix, char_start, char_end, doc_hash, text, embedding, asserted_at)
                  VALUES %s ON CONFLICT (filepath, chunk_ix) DO UPDATE SET
                  doc_id=EXCLUDED.doc_id, filename=EXCLUDED.filename, category=EXCLUDED.category,
                  char_start=EXCLUDED.char_start, char_end=EXCLUDED.char_end, doc_hash=EXCLUDED.doc_hash,
                  text=EXCLUDED.text, embedding=EXCLUDED.embedding, indexed_at=now(),
                  asserted_at=EXCLUDED.asserted_at""",
                  batch, template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")
                conn.commit(); batch.clear()
        if di % 50 == 0 or (rate_test and di == len(docs)):
            el = time.time() - t0
            log.info(f"wq309 progress docs {di}/{len(docs)} chunks new={new} skip={skip} err={err} elapsed={el:.0f}s rate={new/max(el,1):.2f} emb/s")
    if batch:
        execute_values(cur, """INSERT INTO research_chunks
          (doc_id, filepath, filename, category, chunk_ix, char_start, char_end, doc_hash, text, embedding, asserted_at)
          VALUES %s ON CONFLICT (filepath, chunk_ix) DO UPDATE SET
          doc_id=EXCLUDED.doc_id, filename=EXCLUDED.filename, category=EXCLUDED.category,
          char_start=EXCLUDED.char_start, char_end=EXCLUDED.char_end, doc_hash=EXCLUDED.doc_hash,
          text=EXCLUDED.text, embedding=EXCLUDED.embedding, indexed_at=now(),
          asserted_at=EXCLUDED.asserted_at""",
          batch, template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")
        conn.commit()
    el = time.time() - t0
    log.info(f"wq309 DONE docs={len(docs)} chunks new={new} skip={skip} err={err} elapsed={el/60:.1f}min")
    conn.close()

if __name__ == "__main__":
    main()
