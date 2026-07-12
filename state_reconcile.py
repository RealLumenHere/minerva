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
        old = cur.get((subj, 'tailscale_ip'))
        if old == ip:
            continue
        changes.append((subj, old, ip))
        if not dry:
            ingest(subj, 'tailscale_ip', ip, source_ref='reconcile:tailscale')

    tag = 'DRY-RUN' if dry else 'APPLIED'
    if changes:
        print(f'[{tag}] tailscale_ip reconciled:')
        for subj, old, ip in changes:
            print(f'  {subj}: {old} -> {ip}')
    else:
        print(f'[{tag}] no tailscale_ip drift; store matches the wire.')
    if flags:
        print('UNKNOWN online peers (review; not auto-ingested):')
        for host, ip in flags:
            print(f'  {host} {ip}')
    if changes and not dry:
        # refresh the rendered live block
        subprocess.run(['/home/lumen/bin/state-regen'], capture_output=True)

if __name__ == '__main__':
    main()
