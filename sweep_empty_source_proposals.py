#!/usr/bin/env python3
"""wq#2027 step 3 — sweep pending proposals seeded from a classified_intel row
with no fetched url/summary, and re-judge them.

Reference defect (foreman/research/AGENTFORGE_EVAL.md section 0/1/6):
proposals ff80d5e94515 (source classified_intel row for Saichandra2520/
AgentForge) and 37cb4ce26a9b (source row for Roshanrameshhub/AgentForge) were
both generated from a repo TITLE only — classified_intel.url and .summary
were empty for both source rows — and both scored verify()=1.00, because
every layer that existed then checked a claim against Lumen CONTEXT, never
against the external subject. The eval doc's own recommendation (section 6)
is REJECT both, left untouched in proposals/pending/ pending this sweep.

"Re-judge" here means: apply the wq#2027 gate that now exists (get_candidates()
in minerva_proposal.py refuses to seed a NEW proposal from such a row; verify()
now blocks a claim about a subject with no fetched source, see
source_grounding_check()) to proposals that were generated BEFORE the gate
existed. A pending proposal whose source row still has no url/summary cannot
pass that gate today, so it is rejected now rather than left in limbo — the
same "reject or already covered" discipline INVARIANTS demands of the judge
itself (minerva_proposal.py's DECISION DISCIPLINE block). This is NOT a blind
re-approval path (contrast reopen_stale_rejects.py, which requeues for a
fresh judge pass because its false-positive gates were themselves the bug):
here the source is still missing, so the only honest outcome is reject.

Fail-safe: dry-run by default, prints the count and a sample; --apply
required to write. Idempotent: only touches proposals currently
status/decision='pending' whose source_intel_id row has an empty url or
summary (after the additive `summary` column migration).
"""
import argparse
import sqlite3
from datetime import datetime, timezone

DB = '/home/lumen/minerva/minerva.db'

REJECT_REASON = (
    'wq#2027 sweep: source_intel_id classified_intel row has no fetched '
    'url/summary — claims about the external subject cannot be grounded '
    '(source_grounding_check() fail-closed). Re-eligible once the classifier '
    'backfills url/summary and re-generates a fresh proposal.'
)

SELECT_SQL = """
    SELECT p.id, p.title, p.source_intel_id, c.url, c.summary
    FROM proposals p
    JOIN classified_intel c ON p.source_intel_id = c.id
    WHERE p.status = 'pending'
      AND (c.url IS NULL OR trim(c.url) = '' OR c.summary IS NULL OR trim(c.summary) = '')
"""


def _ensure_summary_column(conn):
    cols = {r[1] for r in conn.execute('PRAGMA table_info(classified_intel)')}
    if 'summary' not in cols:
        conn.execute('ALTER TABLE classified_intel ADD COLUMN summary TEXT')
        conn.commit()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--apply', action='store_true', help='write the change; default is dry-run')
    ap.add_argument('--db', default=DB)
    a = ap.parse_args()

    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row
    _ensure_summary_column(conn)
    rows = conn.execute(SELECT_SQL).fetchall()
    print(f'{len(rows)} pending proposal(s) sourced from a classified_intel row with no url/summary')
    for r in rows[:10]:
        print(f'  {r["id"]}  intel={r["source_intel_id"]}  url={r["url"] or "<empty>"!r}  {(r["title"] or "")[:60]}')
    if len(rows) > 10:
        print(f'  ... and {len(rows) - 10} more')

    if not a.apply:
        print('dry-run: nothing written (pass --apply to reject these)')
        conn.close()
        return 0

    ids = [r['id'] for r in rows]
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "UPDATE proposals SET status = 'rejected', decision = 'reject', "
        "decision_reason = ? WHERE id = ?",
        [(f'{REJECT_REASON} (swept {now})', i) for i in ids])
    conn.commit()
    n = conn.execute(
        "SELECT count(*) FROM proposals WHERE status = 'rejected' AND id IN "
        f"({','.join('?' * len(ids))})", ids).fetchone()[0] if ids else 0
    conn.close()
    print(f'rejected {n}/{len(ids)} proposal(s) — source row still has no fetched url/summary')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
