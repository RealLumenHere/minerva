#!/usr/bin/env python3
"""
index_research.py — Embed /home/lumen/scratch/research/ markdown files into
the lumen-pgvector research database.
"""
import os, sys, time, hashlib, logging, pathlib
from datetime import datetime, timezone
import requests, psycopg2
from psycopg2.extras import execute_values

ROOT           = pathlib.Path(os.environ.get("LUMEN_RESEARCH_ROOT", "/home/lumen/scratch/research"))
OLLAMA_URL     = "http://localhost:11434/api/embeddings"
EMBED_MODEL    = "nomic-embed-text:latest"
EMBED_TIMEOUT  = 60
EMBED_RETRY    = 5
EMBED_BACKOFF  = (2, 5, 15, 45, 120)
BATCH_SIZE     = 50

LOG_PATH = pathlib.Path("/home/lumen/logs/index_research.log")
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()])
log = logging.getLogger("index_research")

def _load_env():
    env_path = pathlib.Path("/home/lumen/agent/.env")
    if not env_path.exists(): return
    for line in env_path.read_text().splitlines():
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

def sha256_of(text): return hashlib.sha256(text.encode("utf-8","replace")).hexdigest()

def classify(filename):
    if filename.startswith("000_VIP_camp"): return "vip_campaign"
    if filename.startswith("00_gap_"):      return "scout_gap"
    for pfx in ("pe_seo","pe_pain","pe_money","pe_agent","pe_build","pe_winforge","pe_s5","pe_watch8","pe_pivot","pe_competitor"):
        if filename.startswith(pfx): return pfx
    if filename.startswith("pe_"): return "pe_other"
    return "other"

def _embed_char_token_cost(ch):
    """Approx nomic-embed tokens/char by script class (BPE fragments non-Latin).
    Calibrated 2026-07-05 against live nomic-embed-text: Latin ~0.25, CJK ~1.0,
    other non-Latin (Cyrillic/Arabic/Devanagari/Thai/...) ~0.72 measured -> 0.8 w/ margin."""
    o = ord(ch)
    if (0x3040 <= o <= 0x30FF) or (0x3400 <= o <= 0x9FFF) or (0xAC00 <= o <= 0xD7A3) \
       or (0xF900 <= o <= 0xFAFF) or (0x20000 <= o <= 0x2FA1F):
        return 1.0            # CJK / Kana / Hangul
    if o < 0x0250:
        return 0.25           # ASCII + Latin-1/ext -> ~4 chars/token
    return 0.8                # Cyrillic/Arabic/Hebrew/Indic/Thai/Greek etc.

def safe_truncate(text, token_budget=1750, hard_char_cap=7000):
    """Script-aware truncation so non-English docs stay under nomic-embed's 2048-token
    ctx on the FIRST embed call (a flat char cap assumes ~4 chars/token = English-only,
    so CJK/Cyrillic/Arabic overflowed -> HTTP 500 + halve-retry churn). ASCII fast-path
    keeps English behaviour identical (text[:7000])."""
    if text.isascii():
        return text[:hard_char_cap]
    acc = 0.0
    for i, ch in enumerate(text):
        acc += _embed_char_token_cost(ch)
        if acc > token_budget:
            return text[:i]
    return text[:hard_char_cap]

def embed(text):
    # 2026-06-07: nomic-embed-text ctx is 2048 tokens; ollama returns HTTP 500 on overflow.
    # 2026-07-05: replaced flat text[:7000] (assumed ~4 chars/token = English-only, so
    # CJK/Cyrillic/Arabic overflowed -> 500 + halve-retry churn) with script-aware
    # safe_truncate(). Measured overflow on non-EN/mixed sample: 4/5 -> 0/5. English unchanged.
    truncated = safe_truncate(text)
    payload = {"model": EMBED_MODEL, "prompt": truncated}
    last_err = None
    for attempt in range(EMBED_RETRY):
        try:
            r = requests.post(OLLAMA_URL, json=payload, timeout=EMBED_TIMEOUT)
            if r.status_code == 500 and 'context length' in r.text:
                # deterministic overflow — halve once immediately, no backoff
                if len(payload["prompt"]) > 3500:
                    payload = {"model": EMBED_MODEL, "prompt": truncated[:3500]}
                    continue
            r.raise_for_status()
            emb = r.json().get("embedding")
            if not emb or len(emb) != 768:
                last_err = f"bad-shape:{len(emb) if emb else 0}"
            else:
                return emb
        except Exception as e:
            last_err = str(e)[:200]
        if attempt < EMBED_RETRY - 1:
            time.sleep(EMBED_BACKOFF[attempt])
    log.warning(f"embed failed: {last_err}")
    return None

def existing_hashes(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT filepath, content_hash FROM research_docs")
        return dict(cur.fetchall())

def upsert_batch(conn, rows):
    if not rows: return
    sql = """
    INSERT INTO research_docs (filepath, filename, category, mtime, size_bytes, content, content_hash, embedding)
    VALUES %s
    ON CONFLICT (filepath) DO UPDATE SET
      filename=EXCLUDED.filename, category=EXCLUDED.category, mtime=EXCLUDED.mtime,
      size_bytes=EXCLUDED.size_bytes, content=EXCLUDED.content, content_hash=EXCLUDED.content_hash,
      embedding=EXCLUDED.embedding, indexed_at=now();
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, rows, template="(%s,%s,%s,%s,%s,%s,%s,%s)")
    conn.commit()

def log_error(conn, fp, err):
    # 2026-05-20 ri-fix: log_error may be called from inside an aborted
    # transaction (line 137 outer-except). Roll back FIRST so the INSERT
    # runs in a fresh transaction; otherwise psycopg2 raises
    # InFailedSqlTransaction and cascades the failure. Idempotent: if the
    # transaction is already clean, rollback() is a no-op.
    try:
        conn.rollback()
    except Exception:
        pass
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO research_docs_errors (filepath, error) VALUES (%s, %s)", (fp, err[:500]))
        conn.commit()
    except Exception as e:
        # Last-resort: if even the error-log INSERT fails, don't take down
        # the whole pass — log to stderr and roll back so the next iteration
        # starts clean.
        try: conn.rollback()
        except Exception: pass
        log.error(f"log_error itself failed for {fp}: {e}")

def main():
    if not ROOT.exists():
        log.error(f"root missing: {ROOT}"); return 1
    # 2026-06-12 (Claude): RECONCILE — delete rows whose file no longer exists.
    # Without this, the 2026-06 dupe quarantine left 76k stale rows polluting
    # retrieval. Runs every invocation; cheap relative to embedding.
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT filepath FROM research_docs")
            gone = [(r[0],) for r in cur.fetchall() if not pathlib.Path(r[0]).exists()]
            if gone:
                from psycopg2.extras import execute_values as _ev
                _ev(cur, "DELETE FROM research_docs WHERE filepath IN (SELECT v.fp FROM (VALUES %s) AS v(fp))", gone, page_size=5000)
                log.info(f"reconcile: pruned {len(gone)} rows for deleted files")
    except Exception as e:
        log.warning(f"reconcile prune failed: {e}")
    conn = _conn()
    seen = existing_hashes(conn)
    log.info(f"DB already has {len(seen)} docs indexed.")
    files = sorted(ROOT.rglob("*.md"))
    log.info(f"Found {len(files)} .md files")
    batch, new, skipped, err_c = [], 0, 0, 0
    start = time.time()
    for i, fp in enumerate(files, 1):
        try:
            rel = str(fp)
            try:
                content = fp.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                log_error(conn, rel, f"read-fail:{e}"); err_c += 1; continue
            if not content.strip():
                skipped += 1; continue
            h = sha256_of(content)
            if seen.get(rel) == h:
                skipped += 1; continue
            st = fp.stat()
            mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
            emb = embed(content)
            if emb is None:
                log_error(conn, rel, "embed-failed"); err_c += 1; continue
            batch.append((rel, fp.name, classify(fp.name), mtime, st.st_size, content, h, emb))
            new += 1
            if len(batch) >= BATCH_SIZE:
                upsert_batch(conn, batch); batch.clear()
                el = time.time()-start
                rate = (new+skipped)/max(el,1)
                eta = (len(files)-i)/max(rate,0.01)
                log.info(f"progress {i}/{len(files)} new={new} skip={skipped} err={err_c} rate={rate:.1f}/s eta={eta/60:.1f}min")
        except KeyboardInterrupt:
            log.warning("interrupt"); upsert_batch(conn, batch); conn.close(); return 130
        except Exception as e:
            log.exception(f"unexpected on {fp}")
            log_error(conn, str(fp), f"exc:{e}"); err_c += 1
    upsert_batch(conn, batch)
    log.info(f"DONE files={len(files)} new={new} skip={skipped} err={err_c} elapsed={(time.time()-start)/60:.1f}min")
    conn.close()
    # wq#309: chunk-level pass — research_chunks is the retrieval surface (tails embedded).
    # Resumable + doc_hash-gated, so this only embeds chunks for new/changed docs.
    try:
        sys.path.insert(0, "/home/lumen/minerva/retrieval/wq309")
        import chunk_reembed
        chunk_reembed.main()
    except Exception as e:
        log.error(f"wq309 chunk pass failed (doc-level index unaffected): {e}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
