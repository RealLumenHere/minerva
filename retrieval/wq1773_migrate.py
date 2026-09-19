#!/usr/bin/env python3
"""wq#1773 pgvector migration: add asserted_at to research_docs + research_chunks, backfill.

Additive only. Reverse: ALTER TABLE research_docs DROP COLUMN asserted_at;
                        ALTER TABLE research_chunks DROP COLUMN asserted_at;
Backup: /home/lumen/backups/pgvector/research_tables_pre_wq1773_20260919T074215Z.dump
Idempotent: safe to re-run.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import psycopg2
from asserted_at import asserted_at

conn = psycopg2.connect(host=os.environ["LUMEN_PGVECTOR_HOST"], port=int(os.environ["LUMEN_PGVECTOR_PORT"]),
                        dbname=os.environ["LUMEN_PGVECTOR_DB"], user=os.environ["LUMEN_PGVECTOR_USER"],
                        password=os.environ["LUMEN_PGVECTOR_PASSWORD"])
cur = conn.cursor()
cur.execute("ALTER TABLE research_docs   ADD COLUMN IF NOT EXISTS asserted_at timestamptz")
cur.execute("ALTER TABLE research_chunks ADD COLUMN IF NOT EXISTS asserted_at timestamptz")
conn.commit()

cur.execute("SELECT id, content, mtime FROM research_docs WHERE asserted_at IS NULL")
rows = cur.fetchall()
upd = [(asserted_at(c or "", m), i) for i, c, m in rows]
declared = sum(1 for (a, _), (_, _, m) in zip(upd, rows) if a != m)
cur.executemany("UPDATE research_docs SET asserted_at=%s WHERE id=%s", upd)
cur.execute("""UPDATE research_chunks c SET asserted_at=d.asserted_at
               FROM research_docs d WHERE c.doc_id=d.id AND c.asserted_at IS NULL""")
chunks = cur.rowcount
conn.commit()
cur.execute("SELECT count(*) filter (where asserted_at is null), count(*) FROM research_docs")
dn = cur.fetchone()
cur.execute("SELECT count(*) filter (where asserted_at is null), count(*) FROM research_chunks")
cn = cur.fetchone()
print(f"docs backfilled={len(upd)} (declared-date={declared}) chunks backfilled={chunks}")
print(f"null/total docs={dn} chunks={cn}")
