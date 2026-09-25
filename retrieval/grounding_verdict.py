#!/usr/bin/env python3
"""grounding_verdict.py — wq#2066 (split from wq#1695 item 3).

Carries the wq#1695 fleet-grounding verdict onto pgvector `research_docs` so the
retrieval readers can stop serving reports whose fleet claims do not resolve,
without quarantining them: a failing report still holds valid web research, so
exclusion from RAG is the fix, not deletion.

Columns (additive, nullable, no re-embedding; NULL = never assessed = still served):
  grounding_verdict    TEXT     'pass' | 'fail'
  asserts_fleet_state  BOOLEAN  the doc makes at least one fleet claim
                                (path / unit / identifier / table) that does not
                                resolve on the box. A 'fail' caused only by a
                                format problem (too short, unterminated reasoning
                                block) leaves it FALSE.

Three surfaces:
  parse_header(content)  what index_research.py calls per new doc: read the
                         `grounding:` line fleet_grounding.header() wrote. No
                         fleet_grounding import, stdlib only, never raises.
  backfill               run fleet_grounding.check() over each doc's content for
                         rows still NULL. Idempotent + resumable (NULL-only unless
                         --force), commits per batch, --dry-run writes nothing.
  migrate / status       ADD COLUMN IF NOT EXISTS (after a mandatory snapshot
                         dump), and the per-verdict counts.

Readers exclude with RAG_FILTER_SQL (`d.grounding_verdict IS DISTINCT FROM 'fail'`),
which keeps NULL rows in: an unassessed doc is not a failing one.

Usage:
  python3 grounding_verdict.py migrate --dump /path/research_docs_pre_wq2066.csv
  python3 grounding_verdict.py backfill [--dry-run] [--limit N] [--force]
  python3 grounding_verdict.py status
"""
import argparse
import os
import pathlib
import re
import sys

ENV_FILE = os.environ.get("LUMEN_AGENT_ENV", "/home/lumen/agent/.env")
AUTONOMY_DIR = os.environ.get("FLEET_GROUNDING_DIR", "/home/lumen/autonomy")
PROMPTS_DONE = pathlib.Path(os.environ.get(
    "LUMEN_RESEARCH_PROMPTS_DONE", "/home/lumen/scratch/research_prompts_done"))
BATCH = 200

RAG_FILTER_SQL = "d.grounding_verdict IS DISTINCT FROM 'fail'"

MIGRATION_SQL = """
ALTER TABLE research_docs
  ADD COLUMN IF NOT EXISTS grounding_verdict   TEXT,
  ADD COLUMN IF NOT EXISTS asserts_fleet_state BOOLEAN;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'research_docs_grounding_verdict_chk') THEN
    ALTER TABLE research_docs ADD CONSTRAINT research_docs_grounding_verdict_chk
      CHECK (grounding_verdict IS NULL OR grounding_verdict IN ('pass', 'fail'));
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS research_docs_grounding_fail_idx
  ON research_docs (id) WHERE grounding_verdict = 'fail';
"""

DUMP_SQL = ("COPY (SELECT id, filepath, content_hash, asserted_at FROM research_docs ORDER BY id) "
            "TO STDOUT WITH CSV HEADER")

_HEADER = re.compile(
    r"^grounding:\s*(pass|fail)\b(?:[^\n]*?fleet_unresolved=(\d+))?", re.I | re.M)
HEADER_SCAN_LINES = 40


def parse_header(content):
    """(grounding_verdict, asserts_fleet_state) from the `grounding:` header line.

    (None, None) when the doc carries no header — pre-gate reports, or a header
    that is not in the first HEADER_SCAN_LINES lines (a report quoting the line
    in its body must not be able to set its own verdict).
    """
    head = "\n".join((content or "").splitlines()[:HEADER_SCAN_LINES])
    m = _HEADER.search(head)
    if not m:
        return None, None
    verdict = m.group(1).lower()
    unresolved = int(m.group(2)) if m.group(2) is not None else None
    if unresolved is None:
        return verdict, None
    return verdict, unresolved > 0


def assess(content, check, prompt_text=""):
    """Run fleet_grounding.check over content -> (verdict, asserts_fleet_state).

    URL provenance is skipped (no search context survives for old reports), the
    same mode the 65/400 measurement used.
    """
    r = check(content or "", prompt_text=prompt_text, check_urls=False)
    fleet = any(kind != "format" for kind, _ in r["unresolved"])
    return r["verdict"], fleet


def _prompt_text(filepath):
    p = PROMPTS_DONE / (pathlib.Path(filepath).stem + ".txt")
    try:
        return p.read_text(errors="replace") if p.exists() else ""
    except OSError:
        return ""


def load_check():
    sys.path.insert(0, AUTONOMY_DIR)
    import fleet_grounding
    return fleet_grounding.check


def _load_env(path=ENV_FILE):
    p = pathlib.Path(path)
    if not p.exists():
        return
    for raw in p.read_text().splitlines():
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        k, v = raw.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def connect():
    import psycopg2
    _load_env()
    return psycopg2.connect(
        host=os.environ["LUMEN_PGVECTOR_HOST"],
        port=int(os.environ["LUMEN_PGVECTOR_PORT"]),
        dbname=os.environ["LUMEN_PGVECTOR_DB"],
        user=os.environ["LUMEN_PGVECTOR_USER"],
        password=os.environ["LUMEN_PGVECTOR_PASSWORD"],
    )


def migrate(conn, dump_path):
    """Snapshot first, then the additive DDL. Refuses to run without the dump."""
    if not dump_path:
        raise SystemExit("REFUSED: --dump PATH required (dump first, wq#1773 precedent)")
    with open(dump_path, "w") as fh, conn.cursor() as cur:
        cur.copy_expert(DUMP_SQL, fh)
    if os.path.getsize(dump_path) == 0:
        raise SystemExit(f"REFUSED: dump {dump_path} is empty; not altering")
    with conn.cursor() as cur:
        cur.execute(MIGRATION_SQL)
    conn.commit()
    return dump_path


def backfill(conn, check, dry_run=False, limit=None, force=False, batch=BATCH):
    """Assess rows and write both columns. Returns counter dict.

    Reads and writes on separate cursors: a named cursor streams the content
    (fetching the whole corpus at once would not fit), and the UPDATEs commit per
    batch so an interrupt loses at most one batch. WITH HOLD keeps the read
    cursor alive across those commits.
    """
    where = "" if force else "WHERE grounding_verdict IS NULL"
    lim = f"LIMIT {int(limit)}" if limit else ""
    counts = {"seen": 0, "pass": 0, "fail": 0, "asserts_fleet_state": 0, "errors": 0}
    pending = []
    with conn.cursor(name="wq2066_backfill", withhold=True) as rd:
        rd.itersize = batch
        rd.execute(f"SELECT id, filepath, content FROM research_docs {where} ORDER BY id {lim}")
        for doc_id, filepath, content in rd:
            counts["seen"] += 1
            try:
                verdict, fleet = assess(content, check, _prompt_text(filepath))
            except Exception:
                counts["errors"] += 1
                continue
            counts[verdict] += 1
            counts["asserts_fleet_state"] += int(fleet)
            pending.append((verdict, fleet, doc_id))
            if len(pending) >= batch and not dry_run:
                _flush(conn, pending)
        if pending and not dry_run:
            _flush(conn, pending)
    if dry_run:
        conn.rollback()
    return counts


def _flush(conn, pending):
    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE research_docs SET grounding_verdict = %s, asserts_fleet_state = %s WHERE id = %s",
            pending)
    conn.commit()
    pending.clear()


def status(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(grounding_verdict, 'NULL'), count(*), "
                    "count(*) FILTER (WHERE asserts_fleet_state) "
                    "FROM research_docs GROUP BY 1 ORDER BY 1")
        return cur.fetchall()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("migrate")
    m.add_argument("--dump", help="write an id/filepath/content_hash/asserted_at snapshot here first")
    b = sub.add_parser("backfill")
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--limit", type=int)
    b.add_argument("--force", action="store_true", help="re-assess rows that already have a verdict")
    sub.add_parser("status")
    a = ap.parse_args(argv)

    conn = connect()
    if a.cmd == "migrate":
        print(f"dumped -> {migrate(conn, a.dump)}; columns ensured")
    elif a.cmd == "backfill":
        print(backfill(conn, load_check(), a.dry_run, a.limit, a.force))
    else:
        for verdict, n, fleet in status(conn):
            print(f"{verdict:6} docs={n} asserts_fleet_state={fleet}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
