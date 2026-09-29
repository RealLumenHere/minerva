#!/usr/bin/env python3
"""memory_embed_guard.py — wq#465: fast repair + fail-loud guard for the SQLite
archival embedding store (minerva.db / archival_notes.embedding).

WHY THIS EXISTS
  2026-08-10: a single ollama embed timeout persisted a ZERO-LENGTH blob. In SQLite
  x'' is NOT NULL, so `WHERE embedding IS NOT NULL` selected it; np.frombuffer gave
  shape (0,) and np.dot raised out of lumen_recall2.search_v3 -> the WHOLE fleet
  silently degraded to v2 recall (R@1 .911 -> .839, ~2x latency) for hours.
  The only repair loop lived on the WEEKLY dream timer => up to a 7-day blind window,
  and the failure surfaced only as a stderr line. This closes both gaps.

  The write path (minerva_memory.write_note) now stores NULL rather than b'', and
  the read path (decode_embedding) is dimension-checked and cannot raise. This guard
  is the third layer: detect, repair, and ALARM rather than degrade quietly.

WHAT IT CHECKS
  1. malformed embeddings   — NULL, zero-length, non-multiple-of-4, or wrong dim
  2. live v3 probe          — search_v3 must actually run its dense path
  3. failure breadcrumb     — lumen_episode._breadcrumb_v3_failure marker file

Repairs malformed rows in place (idempotent, embeddings only — never touches note
text, tags, provenance, or any other table). Alarms via ntfy-send on anything it
could not fix, and exits non-zero so systemd records a failure.

CLI:
  memory_embed_guard.py            # check + repair + alarm (timer default)
  memory_embed_guard.py --check    # read-only, no repair, no alarm
  memory_embed_guard.py --quiet    # suppress ntfy (exit code still meaningful)

EXIT: 0 clean | 1 unresolved anomaly (alarm sent) | 2 guard itself broke
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, '/home/lumen/minerva')

DB = '/home/lumen/minerva/minerva.db'
NTFY = '/home/lumen/bin/ntfy-send'
TOPIC = 'lumen-alerts'
STATE = '/home/lumen/minerva/logs/embed_guard.json'
V3_FAIL_MARKER = '/home/lumen/minerva/logs/v3_recall_failure.json'
PROBE_QUERY = 'vault HA switchover promote standby'
MIN_DENSE_SCANNED = 100          # a healthy store scans ~1000; <100 means broken


def _now():
    return datetime.now(timezone.utc).isoformat()


def alarm(title, msg, prio='high', quiet=False):
    print(f'ALARM [{title}] {msg}', file=sys.stderr)
    if quiet or not os.path.exists(NTFY):
        return
    try:
        subprocess.run([NTFY, '-t', TOPIC, '-p', prio, '-T', title, msg],
                       timeout=30, check=False)
    except Exception as e:
        print(f'  ntfy dispatch failed: {e}', file=sys.stderr)


# wq#2930 (class fix from wq#2907, same guard class as ir-triage). 2026-09-28 a leaked
# BEGIN IMMEDIATE held minerva.db's write lock for 35 min; this guard's catch-all turned
# 'database is locked' into exit 2, a failed oneshot unit, restart/alert spam. A held lock
# is not a fault in the embedding store: retry with backoff, and if it outlasts every
# retry raise ONE deduped aq row and exit 0. The aq row is the alert (wq#2570/2571); the
# next hourly fire retries the whole pass.
AQ_BIN = '/home/lumen/bin/aq'
LOCK_BACKOFF_S = (5, 15, 45)


def _is_locked(e):
    return isinstance(e, sqlite3.OperationalError) and (
        'locked' in str(e) or 'busy' in str(e))


def alert_db_locked(message):
    """One deduped aq row (source=memory-embed-guard, key=minerva-db-locked).
    Best-effort: a missing aq binary only logs."""
    try:
        subprocess.run([AQ_BIN, 'add', 'memory-embed-guard', 'minerva-db-locked',
                        'memory-embed-guard: minerva.db locked, pass skipped', message],
                       check=False, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        print(f'  aq unavailable ({e}); lock alert dropped', file=sys.stderr)


def run_with_lock_retry(fn, alert=True):
    """Run fn() (a full guard pass; idempotent) and return its exit code. A locked
    minerva.db is retried on LOCK_BACKOFF_S; if it outlasts them, alert once and
    return 0. Any other exception propagates to the caller's crash path."""
    for delay in LOCK_BACKOFF_S:
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if not _is_locked(e):
                raise
            print(f'  minerva.db locked ({e}); retrying in {delay}s', file=sys.stderr)
            time.sleep(delay)
    try:
        return fn()
    except sqlite3.OperationalError as e:
        if not _is_locked(e):
            raise
        print(f'  minerva.db still locked after {len(LOCK_BACKOFF_S)} retries: {e}',
              file=sys.stderr)
        if alert:
            alert_db_locked(f'held lock outlasted {len(LOCK_BACKOFF_S)} retries ({e}); '
                            'guard pass skipped, the next timer fire retries it')
        return 0


def scan(conn):
    """Return (total, malformed_ids, length_histogram). Read-only."""
    total = conn.execute('SELECT COUNT(*) FROM archival_notes').fetchone()[0]
    hist = dict(conn.execute(
        'SELECT COALESCE(length(embedding), -1), COUNT(*) '
        'FROM archival_notes GROUP BY 1').fetchall())
    bad = [r[0] for r in conn.execute(
        'SELECT id FROM archival_notes WHERE embedding IS NULL '
        'OR length(embedding) = 0 OR length(embedding) % 4 != 0').fetchall()]
    return total, bad, hist


def repair(conn, ids):
    """Re-embed malformed rows. Returns (repaired_count, still_broken_ids)."""
    if not ids:
        return 0, []
    import numpy as np
    import minerva_memory as mm
    ok, still = 0, []
    for nid in ids:
        row = conn.execute(
            'SELECT title, content FROM archival_notes WHERE id=?', (nid,)).fetchone()
        if not row:
            continue
        emb = mm.embed(f'{row[0]}. {(row[1] or "")[:2000]}')
        if emb is None:
            still.append(nid)
            continue
        conn.execute('UPDATE archival_notes SET embedding=? WHERE id=?',
                     (np.asarray(emb, dtype=np.float32).tobytes(), nid))
        ok += 1
    conn.commit()
    return ok, still


def probe_v3():
    """Assert search_v3 runs its dense path. Returns (healthy, detail)."""
    try:
        import lumen_recall2 as lr
        t0 = time.monotonic()
        hits = lr.search_v3(PROBE_QUERY, top_k=5)
        ms = round((time.monotonic() - t0) * 1000)
        st = dict(lr.LAST_STATS)
        if st.get('failopen_v2'):
            return False, f'search_v3 fell open to v2 (empty candidate pool); {st}'
        if st.get('dense_scanned', 0) < MIN_DENSE_SCANNED:
            return False, (f'dense scan covered only {st.get("dense_scanned")} notes '
                           f'(expected >{MIN_DENSE_SCANNED}); {st}')
        if st.get('skipped_bad_emb'):
            return False, f'{st["skipped_bad_emb"]} unusable embedding blob(s) skipped'
        if not hits:
            return False, 'search_v3 returned zero hits on the probe query'
        return True, f'{len(hits)} hits, {st["dense_scanned"]} notes scanned, {ms}ms'
    except Exception as e:
        return False, f'search_v3 RAISED: {type(e).__name__}: {e}'


def read_breadcrumb():
    """v3 failures recorded by lumen_episode.recall since last clean run."""
    if not os.path.exists(V3_FAIL_MARKER):
        return None
    try:
        with open(V3_FAIL_MARKER) as f:
            return json.load(f)
    except Exception:
        return None


def clear_breadcrumb():
    try:
        os.remove(V3_FAIL_MARKER)
    except Exception:
        pass


def write_state(payload):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, 'w') as f:
            json.dump(payload, f, indent=2)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true', help='read-only, no repair/alarm')
    ap.add_argument('--quiet', action='store_true', help='suppress ntfy')
    a = ap.parse_args()

    try:
        conn = sqlite3.connect(DB, timeout=30)
    except Exception as e:
        if _is_locked(e):
            raise   # wq#2930: a held lock is retried by run_with_lock_retry, not a BROKE alarm
        alarm('memory embed guard BROKE', f'cannot open {DB}: {e}', quiet=a.quiet)
        return 2

    try:   # wq#2930: close on every exit path; a retry must not inherit a live handle
        total, bad, hist = scan(conn)
        print(f'[{_now()}] archival_notes={total}  malformed={len(bad)}  lengths={hist}')

        repaired, still = 0, list(bad)
        if bad and not a.check:
            repaired, still = repair(conn, bad)
            print(f'  repaired {repaired}/{len(bad)}; still broken: {still or "none"}')
    finally:
        conn.close()

    healthy, detail = probe_v3()
    print(f'  v3 probe: {"PASS" if healthy else "FAIL"} - {detail}')

    crumb = read_breadcrumb()
    if crumb:
        print(f'  breadcrumb: {crumb.get("count")} v3 failure(s), '
              f'last {crumb.get("last_ts")}: {crumb.get("last_error")}')

    problems = []
    if still:
        problems.append(f'{len(still)} embedding(s) unrepairable '
                        f'(ollama/nomic down?): {still[:5]}')
    if not healthy:
        problems.append(f'v3 recall unhealthy: {detail}')
    if crumb and healthy and not still:
        # Healthy NOW but failed since the last clean run: report once, then clear
        # so a transient that self-healed does not alarm forever.
        problems.append(f'v3 recall failed {crumb.get("count")} time(s) since last '
                        f'clean run (last: {crumb.get("last_error")}) - healthy now')

    write_state({'ts': _now(), 'total': total, 'malformed_found': len(bad),
                 'repaired': repaired, 'still_broken': still,
                 'v3_healthy': healthy, 'v3_detail': detail,
                 'breadcrumb': crumb, 'problems': problems})

    if problems:
        if not a.check:
            alarm('Lumen memory: embedding store degraded',
                  'wq#465 guard: ' + ' | '.join(problems)
                  + ' - semantic recall may be running degraded (v2 fallback). '
                    'Check: python3 /home/lumen/minerva/memory_embed_guard.py --check',
                  quiet=a.quiet)
            if healthy and not still:
                clear_breadcrumb()
        return 1

    clear_breadcrumb()
    print('  CLEAN')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(run_with_lock_retry(main, alert='--check' not in sys.argv))
    except Exception as e:
        print(f'guard crashed: {type(e).__name__}: {e}', file=sys.stderr)
        alarm('memory embed guard CRASHED', f'{type(e).__name__}: {e}')
        sys.exit(2)
