#!/usr/bin/env python3
"""
search_research.py — Semantic search over the pgvector research corpus.

Usage:
  python3 search_research.py "query text"
  python3 search_research.py "query text" --k 10 --category pe_winforge
  python3 search_research.py "query text" --snippet 300
"""
import os, sys, argparse, pathlib, requests, psycopg2

OLLAMA_URL  = "http://127.0.0.1:11434/api/embeddings"  # 2026-06-12: was retired PBox
EMBED_MODEL = "nomic-embed-text:latest"

def _load_env():
    p = pathlib.Path("/home/lumen/agent/.env")
    if not p.exists(): return
    for line in p.read_text().splitlines():
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1)
        k = k.strip(); v = v.strip().strip(chr(34)).strip(chr(39))
        if k and k not in os.environ: os.environ[k] = v
_load_env()

def _conn():
    return psycopg2.connect(
        host=os.environ["LUMEN_PGVECTOR_HOST"],
        port=int(os.environ["LUMEN_PGVECTOR_PORT"]),
        dbname=os.environ["LUMEN_PGVECTOR_DB"],
        user=os.environ["LUMEN_PGVECTOR_USER"],
        password=os.environ["LUMEN_PGVECTOR_PASSWORD"],
    )

def embed_query(text):
    r = requests.post(OLLAMA_URL, json={"model": EMBED_MODEL, "prompt": text}, timeout=30)
    r.raise_for_status()
    return r.json()["embedding"]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--category", default=None)
    ap.add_argument("--snippet", type=int, default=200)
    args = ap.parse_args()

    q_emb = embed_query(args.query)
    vec = "[" + ",".join(str(x) for x in q_emb) + "]"

    conn = _conn()
    cur = conn.cursor()
    sql = """
        SELECT filepath, filename, category, mtime,
               embedding <=> %s::vector AS distance,
               substring(content, 1, %s) AS snippet
        FROM research_docs
        WHERE (%s::text IS NULL OR category = %s)
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """
    cur.execute(sql, (vec, args.snippet, args.category, args.category, vec, args.k))
    rows = cur.fetchall()
    for i, r in enumerate(rows, 1):
        fp, fn, cat, mt, dist, snip = r
        print(f"[{i}] cos-dist={dist:.4f}  cat={cat}  mtime={mt:%Y-%m-%d}")
        print(f"    {fp}")
        print(f"    {snip.strip()[:args.snippet]}")
        print()
    conn.close()

if __name__ == "__main__":
    main()
