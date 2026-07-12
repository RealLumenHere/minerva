#!/usr/bin/env python3
"""
lumen_proposal_digest.py — write /home/lumen/minerva/lumen_state.txt, the
ground-truth digest for the minerva-proposal hallucination guard. wq#127.

History: the original auto-derive was lost when lumen_state_gen.py was
rewritten for Memory V2 (stdout-only LIVE_FLEET_STATE); the file went stale
2026-06-25 and minerva_proposal._load_lumen_state() silently fell back to the
hand-maintained string. This module restores the contract.

Design:
- LIVE PROBES for volatile host facts (RAM, disk, LiteLLM model list, active
  services) — same spirit as the pre-V2 file.
- Bi-temporal state store for the fleet summary (roles only, compact).
- SENTINEL CONVENTION PRESERVED: on any probe failure the section embeds
  'PROBE FAILED', which makes _load_lumen_state() reject the file and use the
  safe fallback (better stale-safe than fresh-wrong).
- Atomic write (tmp + rename); on total failure the old file is left in place.
- Size guard: hard cap 9000 chars (prompt real estate).
"""
import os, json, shutil, subprocess, urllib.request
from datetime import datetime, timezone

OUT = '/home/lumen/minerva/lumen_state.txt'
CAP = 9000


def _probe_hardware():
    try:
        cpu = ''
        for line in open('/proc/cpuinfo'):
            if line.startswith('model name'):
                cpu = line.split(':', 1)[1].strip()
                break
        mem_kb = 0
        for line in open('/proc/meminfo'):
            if line.startswith('MemTotal'):
                mem_kb = int(line.split()[1])
                break
        ram_gb = round(mem_kb / 1024 / 1024)
        osrel = ''
        for line in open('/etc/os-release'):
            if line.startswith('PRETTY_NAME'):
                osrel = line.split('=', 1)[1].strip().strip('"')
                break
        disks = []
        for mnt in ('/data/lumen', '/scratch'):
            try:
                u = shutil.disk_usage(mnt)
                disks.append(f'{mnt} {u.total // 2**30}G total, {u.free // 2**30}G free')
            except Exception:
                pass
        host = os.uname().nodename
        return (f'Hardware: host {host} (Dell T5810), {cpu}, {osrel}, ~{ram_gb}GB RAM, '
                f'no dedicated inference GPU (AMD RX550 display only). '
                f'Tailscale 100.117.66.102. Storage: ' + '; '.join(disks) + '.')
    except Exception as e:
        return f'Hardware: PROBE FAILED ({e})'


def _probe_litellm_models():
    try:
        key = ''
        with open('/home/lumen/agent/.env') as f:
            for line in f:
                if line.startswith('LITELLM_MASTER_KEY='):
                    key = line.split('=', 1)[1].strip().strip('"').strip("'")
                    break
        req = urllib.request.Request('http://localhost:4000/v1/models',
                                     headers={'Authorization': f'Bearer {key}'})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        names = sorted(m.get('id', '') for m in data.get('data', []))
        if not names:
            return 'LLM gateway models: PROBE FAILED (empty list)'
        return ('LLM gateway (LiteLLM localhost:4000) — ALL inference routes through it; '
                'NO Anthropic/OpenAI spend. Models available: ' + ', '.join(names) + '.')
    except Exception as e:
        return f'LLM gateway models: PROBE FAILED ({e})'


def _probe_services():
    try:
        r = subprocess.run(['systemctl', 'list-units', 'lumen-*', 'minerva-*',
                            '--state=active', '--no-legend', '--plain'],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return 'Active services: PROBE FAILED (systemctl rc!=0)'
        units = sorted(line.split()[0] for line in r.stdout.strip().splitlines() if line.split())
        return ('Active Lumen/Minerva systemd units right now: ' + ', '.join(units) + '.')
    except Exception as e:
        return f'Active services: PROBE FAILED ({e})'


def _fleet_summary():
    try:
        from lumen_state import current
        rows = current(min_confidence=0.25)
        by_subj = {}
        for r in rows:
            by_subj.setdefault(r['subject'], {})[r['predicate']] = r['value']
        lines = []
        for subj in sorted(by_subj):
            p = by_subj[subj]
            if 'role' not in p:
                continue
            bits = [str(p['role'])[:110]]
            if 'status' in p:
                bits.append(str(p['status'])[:60])
            lines.append(f'- {subj}: ' + ' | '.join(bits))
        return 'Fleet (from bi-temporal state store, roles only):\n' + '\n'.join(lines)
    except Exception as e:
        return f'Fleet summary: PROBE FAILED ({e})'


# Standing policy/gaps text — policy, not probes; keep aligned with
# minerva_proposal.LUMEN_STATE_FALLBACK when editing.
_POLICY = """Real current GAPS (the truth — do not invent others):
- Intel feed keyword pre-scoring is shallow; feed health is not monitored.
- No experiment/eval harness to measure whether adopted changes actually help.
- Prompt-engine topic banks are static, no auto-adaptation.
- No automated deployer for deploy_ready proposals; they require a human/Claude session.

Content policy: NO publishing of any kind. Ghost CMS was permanently deleted 2026-05 for legal reasons. Research outputs MUST become integration proposals, never published content. PBox GPU node RETIRED 2026-06-07 — no GPU inference node exists. Surface Pro refurbishment business is permanently off-limits."""


def build_digest():
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    parts = [
        f'LUMEN CURRENT STATE (auto-derived, verified {now}):',
        '',
        _probe_hardware(),
        '',
        _probe_litellm_models(),
        '',
        _probe_services(),
        '',
        _fleet_summary(),
        '',
        _POLICY,
    ]
    text = '\n'.join(parts)
    return text[:CAP]


def write_digest():
    text = build_digest()
    tmp = OUT + '.tmp'
    with open(tmp, 'w') as f:
        f.write(text)
    os.replace(tmp, OUT)
    ok = 'PROBE FAILED' not in text
    return (f'wrote {OUT} ({len(text)} chars) '
            f'{"OK" if ok else "WITH PROBE FAILURES (loader will fall back)"}')


if __name__ == '__main__':
    print(write_digest())
