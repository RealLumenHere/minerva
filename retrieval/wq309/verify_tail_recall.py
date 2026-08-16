#!/usr/bin/env python3
"""wq#309 acceptance: pick 10 docs >14k chars, mine a distinctive phrase from the
TAIL (last 20%, i.e. beyond the 7000-char embed cap), query both surfaces top-20:
  pre  = research_docs (doc-level, truncated embeddings)
  post = research_chunks (chunk-level, deduped per doc)
PASS = post hits >= 8/10 AND post hits > pre hits."""
import sys, re, random
sys.path.insert(0, "/home/lumen/minerva/retrieval")
from index_research import _load_env, _conn, embed

random.seed(309)

def top20(cur, table, vec, snippet_col):
    if table == "research_docs":
        cur.execute("SELECT filepath FROM research_docs ORDER BY embedding <=> %s::vector LIMIT 20", (vec,))
    else:
        cur.execute("""SELECT filepath FROM (
            SELECT DISTINCT ON (filepath) filepath, embedding <=> %s::vector AS d
            FROM research_chunks ORDER BY filepath, embedding <=> %s::vector) b
            ORDER BY d LIMIT 20""", (vec, vec))
    return [r[0] for r in cur.fetchall()]

def main():
    conn = _conn(); cur = conn.cursor()
    cur.execute("""SELECT filepath, content FROM research_docs
                   WHERE length(content) > 14000 ORDER BY md5(filepath) LIMIT 10""")
    docs = cur.fetchall()
    pre_hits = post_hits = 0
    for fp, content in docs:
        tail = content[int(len(content)*0.8):]
        # distinctive query: longest sentence-ish span of 8-24 words from the tail
        spans = [s.strip() for s in re.split(r'[.\n]', tail) if 8 <= len(s.split()) <= 24]
        q = max(spans, key=len) if spans else tail[:300]
        vec = "[" + ",".join(str(x) for x in embed(q)) + "]"
        pre = fp in top20(cur, "research_docs", vec, "content")
        post = fp in top20(cur, "research_chunks", vec, "text")
        pre_hits += pre; post_hits += post
        print(f"{'PRE-HIT ' if pre else 'pre-miss'} {'POST-HIT ' if post else 'POST-MISS'} {fp.split('/')[-1][:60]}")
    print(f"RESULT pre={pre_hits}/10 post={post_hits}/10 -> {'PASS' if post_hits >= 8 and post_hits > pre_hits else 'FAIL'}")
    conn.close()

if __name__ == "__main__":
    main()
