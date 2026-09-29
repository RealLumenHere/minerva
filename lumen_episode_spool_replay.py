#!/usr/bin/env python3
"""
lumen_episode_spool_replay.py - wq#3173: drain EPISODE_SPOOL_DIR.

lumen_episode.py commit spools a payload here when memoryd's identity gate rejects
it (agent not registered / no live claim on source_ref / missing source_ref -- see
lumen_episode.py's module docstring). This script re-POSTs each spooled entry through
lumen_episode._memoryd_commit() -- the SAME code path and SAME gate the CLI used, so
an accept here means the gate's own current verdict changed, not a second copy of its
logic drifting out of sync with daemon.py.

Each entry replays under its ORIGINAL agent identity (WQ_AGENT is swapped in for the
call and restored after) -- a later run of the SAME rejected agent id is the only
thing "holds a live claim now" can mean once the CLI process that made the commit is
long gone.

Runs unattended off lumen-episode-spool-replay.timer (every 15 min, staging/). Safe to
run by hand or overlap: each entry is an independent file, a landed one is deleted, and
a still-rejected one is rewritten in place with an updated reason/attempt count.

CLI: lumen_episode_spool_replay.py [--dry-run]
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lumen_episode as le

MAX_AGE_DAYS = 14
MAX_ATTEMPTS = 200


def _age_days(entry):
    try:
        spooled = datetime.fromisoformat(entry['spooled_at'])
    except Exception:
        return 0.0
    return (datetime.now(timezone.utc) - spooled).total_seconds() / 86400.0


def replay_one(path, dry_run=False):
    """(outcome, detail) for one spool file. Never raises -- a bad entry is reported,
    not fatal to the rest of the drain."""
    try:
        with open(path) as f:
            entry = json.load(f)
    except Exception as e:
        return 'unreadable', str(e)
    agent = (entry.get('agent') or '').strip()
    if not agent:
        return 'no-agent', 'nothing a replay could authenticate as'
    if dry_run:
        return 'dry-run', 'would retry as %s' % agent

    prev_agent = os.environ.get('WQ_AGENT')
    os.environ['WQ_AGENT'] = agent
    try:
        nid, reason = le._memoryd_commit(
            entry.get('kind') or 'session', entry.get('title') or '',
            entry.get('content') or '', entry.get('tags') or [],
            entry.get('source_ref') or '')
    finally:
        if prev_agent is None:
            os.environ.pop('WQ_AGENT', None)
        else:
            os.environ['WQ_AGENT'] = prev_agent

    if nid:
        os.remove(path)
        return 'landed', str(nid)

    entry['attempts'] = int(entry.get('attempts', 0)) + 1
    entry['last_reason'] = str(reason)[:300]
    entry['last_tried'] = datetime.now(timezone.utc).isoformat()
    if entry['attempts'] >= MAX_ATTEMPTS or _age_days(entry) >= MAX_AGE_DAYS:
        expired_dir = os.path.join(os.path.dirname(path), 'expired')
        os.makedirs(expired_dir, mode=0o700, exist_ok=True)
        os.replace(path, os.path.join(expired_dir, os.path.basename(path)))
        return 'expired', reason
    with open(path, 'w') as f:
        json.dump(entry, f)
    return 'still-rejected', reason


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help='list what would be retried without POSTing or mutating spool files')
    args = ap.parse_args()

    spool_dir = le.EPISODE_SPOOL_DIR
    os.makedirs(spool_dir, mode=0o700, exist_ok=True)
    paths = sorted(glob.glob(os.path.join(spool_dir, '*.json')))
    if not paths:
        print('spool empty (%s)' % spool_dir)
        return 0
    counts = {}
    for path in paths:
        outcome, detail = replay_one(path, dry_run=args.dry_run)
        counts[outcome] = counts.get(outcome, 0) + 1
        print('%s: %s (%s)' % (os.path.basename(path), outcome, detail))
    print('summary: ' + ', '.join('%s=%d' % kv for kv in sorted(counts.items())))
    return 0


if __name__ == '__main__':
    sys.exit(main())
