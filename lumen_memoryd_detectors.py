#!/usr/bin/env python3
"""lumen_memoryd_detectors.py — wq#610: wires design §3.3's D-1/D-3/D-4/D-5 into
the nightly dream run. D-2 (trust escalation) is deliberately NOT here: it is
already live and synchronous in kernel.py's validate_note() (fires at write
time, not next-dream), which is strictly better than the design's original
"computed nightly" framing for the one detector whose whole point is catching
a bypass before it lands — nothing to add.

PROPOSAL-ONLY, same discipline as every other lumen_dream consumer (merge
plan, instinct candidates, prose demotions): D-1/D-4 write a dated proposal
file for operator review; D-3/D-5 are report-line-only per §3.3's own action
table ("Actions: D-2 -> quarantine + alert. D-1/D-4 -> dream proposal for
operator review. D-3/D-5 -> report line only."). Nothing here mutates
archival_notes, memory_audit, or memory_quarantine.

HONEST LIMITATION, stated up front rather than discovered later: memoryd
(wq#575) went live 2026-08-15; as of this ticket its ENTIRE write history is
7 kernel-attributed archival_notes rows (6 lane='memory' drill writes from
mv4-w575-05322f, 1 lane='operator') and 146 memory_audit rows spanning under
17 hours. D-1's "trailing 8-week median" and D-3's "first ever" are both
running against a population that is, honestly, one session's worth of drill
data. The detectors are built correct-for-scale (same "gets sharper as the
lane dimension populates" honesty the design doc itself states in §3.3), not
padded to look more meaningful than the data currently supports.
"""
import json
import math
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path

DB = '/home/lumen/minerva/minerva.db'
OUT = Path('/home/lumen/minerva/dream')

LEGACY_LANE = 'legacy'   # design §2.1: pre-cutover rows are lane='legacy' (displayed only; the
                          # column itself stays NULL — §2.1's own "we do not backfill a trust
                          # level we cannot evidence" applies equally to lane).


def _now_pt():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(timezone.utc).astimezone(ZoneInfo('America/Los_Angeles')).strftime('%Y-%m-%d %H:%M PT')
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _lane(row_lane):
    return row_lane or LEGACY_LANE


# ---------- D-1: origin-mix shift ----------

def d1_origin_mix_shift(conn, window_days=7, lookback_weeks=8):
    """Per-lane write counts in the trailing window vs the trailing lookback_weeks
    of weekly counts before it (median + stdev). Flag at >3 sigma OR >5x median,
    whichever is LOOSER (design §3.3 — "whichever is looser" means the flag fires
    only if the observed count clears the MORE PERMISSIVE of the two bars, so a
    lane isn't flagged on a technicality when either single measure would forgive it).
    """
    rows = conn.execute(
        "SELECT lane, created_at FROM archival_notes WHERE created_at IS NOT NULL"
    ).fetchall()
    if not rows:
        return {'flagged': [], 'lanes': {}, 'note': 'no archival_notes rows with created_at'}

    now = datetime.now(timezone.utc)
    by_lane = {}
    for lane, created_at in rows:
        try:
            ts = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        by_lane.setdefault(_lane(lane), []).append(ts)

    flagged = []
    lane_report = {}
    for lane, tss in by_lane.items():
        tss.sort()
        cur_count = sum(1 for t in tss if (now - t).days < window_days)
        # weekly buckets for the lookback_weeks *before* the current window
        weekly = []
        for w in range(1, lookback_weeks + 1):
            lo = now.__sub__(__import__('datetime').timedelta(days=window_days * (w + 1)))
            hi = now.__sub__(__import__('datetime').timedelta(days=window_days * w))
            weekly.append(sum(1 for t in tss if lo <= t < hi))
        have_history = sum(1 for w in weekly if w > 0) >= 3   # need >=3 nonzero buckets to trust a median
        median = statistics.median(weekly) if weekly else 0
        stdev = statistics.pstdev(weekly) if len(weekly) > 1 else 0.0
        sigma_bar = median + 3 * stdev
        five_x_bar = median * 5
        looser_bar = max(sigma_bar, five_x_bar)
        flag = have_history and cur_count > looser_bar and cur_count > 0
        lane_report[lane] = {
            'current_window_count': cur_count, 'weekly_history': weekly,
            'median': median, 'stdev': round(stdev, 2),
            'have_sufficient_history': have_history, 'flagged': flag,
        }
        if flag:
            flagged.append({'lane': lane, 'current_window_count': cur_count,
                            'median': median, 'stdev': round(stdev, 2)})
    return {'flagged': flagged, 'lanes': lane_report,
            'note': ('insufficient trailing history for every lane (need >=3 nonzero '
                     'weekly buckets to trust a median) — expected on a 1-day-old '
                     'kernel-attributed corpus' if not flagged and
                     not any(v['have_sufficient_history'] for v in lane_report.values())
                     else '')}


# ---------- D-3: novel writer ----------

def d3_novel_writers(conn, window_days=7):
    """First write EVER seen from a (lane, agent_id) pair, where 'ever' is scoped
    to this write being earlier than or equal to every other write from the same
    pair. Not suspicious alone (every new session is novel) — report-line only,
    counted into D-1's per-lane totals (which it already is, since D-1 sums by
    lane regardless of agent_id)."""
    rows = conn.execute(
        "SELECT lane, agent_id, created_at, id FROM archival_notes "
        "WHERE lane IS NOT NULL AND agent_id IS NOT NULL "
        "ORDER BY created_at ASC"
    ).fetchall()
    now = datetime.now(timezone.utc)
    seen = set()
    novel_in_window = []
    for lane, agent_id, created_at, nid in rows:
        key = (lane, agent_id)
        first_seen = key not in seen
        seen.add(key)
        if not first_seen:
            continue
        try:
            ts = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if (now - ts).days < window_days:
            novel_in_window.append({'lane': lane, 'agent_id': agent_id,
                                    'first_write_id': nid, 'first_write_at': created_at})
    return {'novel_writers_in_window': novel_in_window, 'total_known_pairs': len(seen)}


# ---------- D-4: near-duplicate flood ----------

def d4_dup_flood(conn, window_days=1, thresh=0.95, min_n=3):
    """>= min_n writes in window_days with cosine >= thresh to an EXISTING note.
    Reuses lumen_dream.episodic_dupes_v2 (design §3.3's own instruction: 'reuses
    lumen_dream.episodic_dupes_v2, which already computes exactly this') rather
    than re-implementing the all-pairs scan. A 'flood' is scoped to pairs where
    the NEWER note of the pair falls inside the window — an old duplicate pair
    that has sat in the corpus for months is #576/#577's dedup problem, not a
    flood signal."""
    import lumen_dream
    pairs = lumen_dream.episodic_dupes_v2(conn, thresh=thresh)
    now = datetime.now(timezone.utc)
    id_to_lane = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT id, lane, agent_id FROM archival_notes")}
    recent = []
    for sim, a, b in pairs:
        newer = a if (a[2] or '') >= (b[2] or '') else b
        try:
            ts = datetime.fromisoformat((newer[2] or '').replace('Z', '+00:00'))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if (now - ts).days < window_days:
            lane, agent_id = id_to_lane.get(newer[0], (None, None))
            recent.append({'sim': round(sim, 4), 'newer_id': newer[0],
                           'newer_title': newer[1][:60], 'newer_created_at': newer[2],
                           'lane': _lane(lane), 'agent_id': agent_id})
    by_source = {}
    for r in recent:
        by_source.setdefault((r['lane'], r['agent_id']), []).append(r)
    flagged = [{'lane': lane, 'agent_id': agent_id, 'count': len(items), 'items': items}
              for (lane, agent_id), items in by_source.items() if len(items) >= min_n]
    return {'flagged': flagged, 'all_recent_dup_writes': recent, 'window_days': window_days,
            'thresh': thresh, 'min_n': min_n}


# ---------- D-5: unresolvable attribution ----------

def d5_unresolvable_attribution(conn, window_days=7):
    """Count (not list — the payload is already in memory_audit for anyone who
    needs the detail) of I-7 kernel rejects. Under the kernel, an I-7 failure IS
    a reject (validate_note returns Decision('reject', ..., 'I-7') when
    source_ref doesn't resolve to a live wq claim) — this reads memory_audit,
    it does not re-implement I-7. Report-line only per §3.3."""
    now_expr = "datetime('now', ?)"
    row = conn.execute(
        f"SELECT COUNT(*) FROM memory_audit WHERE action='reject' AND invariant='I-7' "
        f"AND ts > {now_expr}", (f'-{window_days} days',)).fetchone()
    total_rejects = conn.execute(
        f"SELECT COUNT(*) FROM memory_audit WHERE action='reject' AND ts > {now_expr}",
        (f'-{window_days} days',)).fetchone()[0]
    return {'i7_rejects_in_window': row[0], 'total_rejects_in_window': total_rejects,
            'window_days': window_days}


# ---------- orchestration ----------

def write_detector_proposal(d1, d4):
    """D-1/D-4 findings -> a dated proposal file for operator review (§3.3
    action table). Mirrors write_merge_plan / write_instincts: a file under
    dream/, nothing auto-applied, nothing here can mutate archival_notes."""
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    p = OUT / f'detector_proposals_{today}.json'
    payload = {
        'generated': _now_pt(),
        'note': 'PROPOSAL-ONLY (wq#610 / design §3.3). Nothing here is auto-applied.',
        'd1_origin_mix_shift': d1['flagged'],
        'd4_dup_flood': d4['flagged'],
    }
    p.write_text(json.dumps(payload, indent=2, default=str))
    return p


def run_detectors(conn):
    d1 = d1_origin_mix_shift(conn)
    d3 = d3_novel_writers(conn)
    d4 = d4_dup_flood(conn)
    d5 = d5_unresolvable_attribution(conn)
    has_findings = bool(d1['flagged'] or d4['flagged'])
    proposal_path = write_detector_proposal(d1, d4) if has_findings else None
    return {'d1': d1, 'd3': d3, 'd4': d4, 'd5': d5, 'proposal_path': proposal_path}


if __name__ == '__main__':
    conn = sqlite3.connect(DB)
    result = run_detectors(conn)
    conn.close()
    print(json.dumps({k: (v if k != 'proposal_path' else str(v))
                      for k, v in result.items()}, indent=2, default=str))
