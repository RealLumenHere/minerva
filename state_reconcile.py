#!/usr/bin/env python3
"""
state_reconcile.py - Change-time ingest hook (ground-truth driven).

Reconciles the bi-temporal state store against LIVE tailscale status on Lumen
(the tailnet authority). Tailscale-IP drift was the #1 staleness source (Thoth,
Apis, be3600 all re-registered with new IPs). This pulls truth from the wire and
ingests any changed tailscale_ip via the normal supersession path (old value's
window closes, history preserved) -- so the store self-heals without manual edits.

SAFETY:
- Only updates `tailscale_ip` for subjects ALREADY KNOWN in the store. Never
  creates nodes from arbitrary peers, never touches the curated `status` field
  (a retired node can still be on the tailnet). Unknown Egyptian-named online
  peers are FLAGGED for human review, not auto-ingested.
- --dry-run shows what would change without writing.

Usage:
  state_reconcile.py --dry-run
  state_reconcile.py            # apply
"""
import sys, json, subprocess, sqlite3
from lumen_state import ingest, current
from lumen_coverage import Coverage

# lowercased tailscale HostName -> store subject (for names that don't map directly)
ALIASES = {
    'toilet-940x5n': 'toilet',
    'gl-be3600': 'be3600',
}
# peers that are clients/phones/etc, never fleet subjects -- ignore silently
IGNORE = {'desktop-u520obe', 'galaxy s26 ultra', 'mirror'}

def known_subjects():
    c = sqlite3.connect('/home/lumen/minerva/minerva.db', timeout=30)
    rows = [r[0] for r in c.execute('SELECT DISTINCT subject FROM state_facts').fetchall()]
    c.close()
    return set(rows)

def ts_status():
    out = subprocess.run(['tailscale', 'status', '--json'], capture_output=True, text=True, timeout=20)
    d = json.loads(out.stdout)
    peers = []
    s = d.get('Self') or {}
    if s.get('HostName'):
        peers.append((s['HostName'], (s.get('TailscaleIPs') or [None])[0], s.get('Online')))
    for v in (d.get('Peer') or {}).values():
        peers.append((v.get('HostName', '?'), (v.get('TailscaleIPs') or [None])[0], v.get('Online')))
    return peers

def subject_for(hostname):
    h = hostname.lower()
    if h in IGNORE:
        return None
    return ALIASES.get(h, h)

def main():
    dry = '--dry-run' in sys.argv
    known = known_subjects()
    cur = {(r['subject'], r['predicate']): r['value'] for r in current(predicate='tailscale_ip')}
    changes, flags = [], []
    # wq#1611: "no drift" and "nothing checked" must not print the same line.
    cov, seen = Coverage('state_reconcile:tailscale_ip'), set()
    for host, ip, online in ts_status():
        if not ip:
            continue
        subj = subject_for(host)
        if subj is None:
            continue
        if subj not in known:
            if online:
                flags.append((host, ip))   # unknown online peer -> review
            continue
        seen.add(subj)
        old = cur.get((subj, 'tailscale_ip'))
        if old is None:
            cov.count('unverifiable', 'no-fact')   # on the wire, absent from the store
            continue
        if old == ip:
            cov.count('matched')
            continue
        cov.count('drifted')
        changes.append((subj, old, ip))
        if not dry:
            ingest(subj, 'tailscale_ip', ip, source_ref='reconcile:tailscale')
    for subj, _pred in cur:
        if subj not in seen:
            cov.count('unverifiable', 'not-seen')  # in the store, never offered by the wire

    tag = 'DRY-RUN' if dry else 'APPLIED'
    print(f'[{tag}] {cov.line()}')
    if changes:
        print(f'[{tag}] tailscale_ip reconciled:')
        for subj, old, ip in changes:
            print(f'  {subj}: {old} -> {ip}')
    elif cov.clean():
        print(f'[{tag}] no tailscale_ip drift; store matches the wire.')
    else:
        print(f'[{tag}] no tailscale_ip drift among the {cov.matched} subject(s) compared, '
              f'but {cov.unverifiable} could not be checked - NOT a clean run.')
    if flags:
        print('UNKNOWN online peers (review; not auto-ingested):')
        for host, ip in flags:
            print(f'  {host} {ip}')
    if changes and not dry:
        # refresh the rendered live block
        subprocess.run(['/home/lumen/bin/state-regen'], capture_output=True)

if __name__ == '__main__':
    main()
