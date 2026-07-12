#!/usr/bin/env python3
"""
backfill_state.py - Seed the bi-temporal state layer from authoritative current
fleet truth (claude_memory.md 06-07 overrides + recent_updates corrections).

Seeds historical->current TRANSITIONS where known, so invalidation is demonstrable:
old values get their windows closed; current() returns truth; as_of(old_date)
returns the old value. Idempotent: re-running ingests no duplicates (same value = noop).
"""
import sqlite3
from lumen_state import init_schema, ingest

# (subject, predicate, value, valid_from, source_ref)
# Ordered so older facts precede the values that supersede them.
SEED = [
    # --- THOTH (resolves eval miss: Seneca MP, NOT Wyse 5070) ---
    ('thoth', 'hardware', 'Seneca MP (11th-gen i3-1115G4)', '2026-04-25', 'mem:thoth-rebuild'),
    ('thoth', 'role', 'torrent/media/Plex server', '2026-04-25', 'mem:thoth'),
    ('thoth', 'tailscale_ip', '100.88.71.47', '2026-04-25', 'mem:thoth-old'),
    ('thoth', 'tailscale_ip', '100.75.1.108', '2026-06-18', 'mem:thoth-update'),   # supersedes
    ('thoth', 'lan_ip', '192.168.8.218', '2026-06-18', 'mem:thoth-update'),
    ('thoth', 'status', 'online', '2026-06-18', 'mem:thoth-update'),

    # --- TOILET (resolves eval miss: TS 100.70.94.19) ---
    ('toilet', 'location', 'Frederick MD', '2026-01-01', 'mem:toilet'),
    ('toilet', 'tailscale_ip', '100.101.251.67', '2026-01-01', 'mem:toilet-old'),
    ('toilet', 'tailscale_ip', '100.70.94.19', '2026-06-17', 'mem:toilet-update'),  # supersedes
    ('toilet', 'lan_ip', '192.168.50.111', '2026-06-17', 'mem:toilet-update'),
    ('toilet', 'status', 'online', '2026-06-17', 'mem:toilet-update'),

    # --- APIS (TS reformat changed machine key) ---
    ('apis', 'role', 'RE/forensics bench (ASRock N3450)', '2026-01-01', 'mem:apis'),
    ('apis', 'tailscale_ip', '100.95.221.74', '2026-01-01', 'mem:apis-old'),
    ('apis', 'tailscale_ip', '100.89.147.95', '2026-06-18', 'mem:apis-reformat'),   # supersedes
    ('apis', 'lan_ip', '192.168.0.72', '2026-06-18', 'mem:apis-reformat'),


    # --- WADJET (newest node) ---
    ('wadjet', 'role', "Ma'at vault HA standby", '2026-06-24', 'mem:wadjet'),
    ('wadjet', 'status', 'deployed (HA standby, auto-promote armed)', '2026-06-24', 'mem:wadjet'),
    ('wadjet', 'lan_ip', '192.168.8.173', '2026-06-24', 'mem:wadjet'),

    # --- NEPHTHYS (still pending, awareness+embeddings role) ---
    ('nephthys', 'role', 'awareness + embeddings (Mac Pro 6,1)', '2026-05-18', 'mem:nephthys'),
    ('nephthys', 'status', 'planned (blocked on 1TB SSD)', '2026-05-18', 'mem:nephthys'),

    # --- LUMEN (orchestration host) ---
    ('lumen', 'tailscale_ip', '100.117.66.102', '2026-06-07', 'override:2026-06-07'),
    ('lumen', 'tailscale_hostname', 'lumen-1', '2026-06-07', 'override:2026-06-07'),
    ('lumen', 'role', 'primary AI orchestration host (T5810)', '2026-01-01', 'mem:lumen'),

    # --- ISIS / HATHOR (deployed Mac sisters) ---
    ('isis', 'lan_ip', '192.168.8.171', '2026-06-09', 'override:2026-06-07'),
    ('isis', 'tailscale_ip', '100.100.129.24', '2026-06-09', 'override:2026-06-07'),
    ('isis', 'status', 'deployed (Qwen3-32B)', '2026-06-09', 'override:2026-06-07'),
    ('hathor', 'lan_ip', '192.168.8.172', '2026-06-09', 'override:2026-06-07'),
    ('hathor', 'status', 'deployed (LFM2.5 + nomic-embed)', '2026-06-09', 'override:2026-06-07'),
    ('hathor', 'tailscale_ip', '100.80.71.13', '2026-06-09', 'mem:hathor'),

    # --- remaining live roster (so the store is the complete current fleet roster) ---
    ('thor', 'hardware', 'Galaxy Book Pro 360 (Xubuntu 26.04)', '2026-06-18', 'mem:thor'),
    ('thor', 'role', 'fleet workstation / TTS', '2026-06-18', 'mem:thor'),
    ('thor', 'lan_ip', '192.168.8.132', '2026-06-18', 'mem:thor'),
    ('thor', 'tailscale_ip', '100.112.151.100', '2026-06-18', 'mem:thor'),
    ('maat', 'role', 'Postgres + Vaultwarden node', '2026-06-01', 'mem:maat'),
    ('maat', 'lan_ip', '192.168.8.202', '2026-06-01', 'mem:maat'),
    ('maat', 'tailscale_ip', '100.105.219.34', '2026-06-01', 'mem:maat'),
    ('anubis', 'role', 'Borg backup repo + Gitea', '2026-06-01', 'mem:anubis'),
    ('anubis', 'lan_ip', '192.168.8.232', '2026-06-01', 'mem:anubis'),
    ('anubis', 'tailscale_ip', '100.121.17.5', '2026-06-01', 'mem:anubis'),
    ('seshat', 'role', 'Nextcloud + Collabora', '2026-06-01', 'mem:seshat'),
    ('seshat', 'lan_ip', '192.168.8.219', '2026-06-01', 'mem:seshat'),
    ('seshat', 'tailscale_ip', '100.121.243.124', '2026-06-01', 'mem:seshat'),
    ('3dprinter', 'role', '3D print server (Klipper dual-instance)', '2026-06-09', 'mem:3dprinter'),
    ('3dprinter', 'tailscale_ip', '100.114.82.85', '2026-06-09', 'mem:3dprinter'),
    ('be3600', 'role', 'GL-iNet Slate 7 workbench router', '2026-06-08', 'mem:be3600'),
    ('be3600', 'tailscale_ip', '100.108.143.46', '2026-06-08', 'mem:be3600'),
]

def main():
    init_schema()
    conn = sqlite3.connect('/home/lumen/minerva/minerva.db', timeout=30)
    conn.row_factory = sqlite3.Row
    counts = {'insert': 0, 'superseded': 0, 'noop': 0, 'skip_exists': 0}
    for subj, pred, val, vf, src in SEED:
        # Idempotency guard: a backfill replays HISTORICAL transitions, which is
        # not safe through ingest() alone (re-seeding an old value would re-open
        # it over the newer one). Skip any seed row already materialised exactly.
        exists = conn.execute(
            '''SELECT 1 FROM state_facts
               WHERE subject=? AND predicate=? AND value=? AND valid_from=?''',
            (subj.strip().lower(), pred.strip().lower(), str(val).strip(), vf)
        ).fetchone()
        if exists:
            counts['skip_exists'] += 1
            continue
        r = ingest(subj, pred, val, valid_from=vf, source_ref=src, conn=conn)
        counts[r['action']] = counts.get(r['action'], 0) + 1
    conn.close()
    print('backfill done:', counts)

if __name__ == '__main__':
    main()
