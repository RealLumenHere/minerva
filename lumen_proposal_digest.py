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
- Size guard: hard cap 13000 chars (prompt real estate).
"""
import os, json, shutil, subprocess, urllib.request
from datetime import datetime, timezone

OUT = '/home/lumen/minerva/lumen_state.txt'
# Raised 9000 -> 13000 (2026-07-26): the services section now carries unit
# DESCRIPTIONS and timer-driven units, not bare names. Root cause it fixes:
# 61% of proposal verifications were scoring judge=0.00 because claims about
# subsystem behaviour ("lumen-scout triggers a script") were unentailed by a
# name-only listing. See minerva_verify SEMANTIC mode (judge >= 0.70 gate).
# 17000 -> 26000 (2026-09-08, wq#1501): FLEET STACK section added and the file was
# ALREADY being cut at 17000 (17096 real), which silently dropped the GAPS/policy tail
# the judge relies on for negative claims. The judge does not read this file raw any
# more: minerva_factpack.judge_digest() compacts the systemd section to fit the local
# rung's 8K-token window; the generator (deepseek, large context) gets the full file.
CAP = 26000


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
    """Name + DESCRIPTION + run-mode for every lumen-*/minerva-* unit.

    Name-only listings starved the hallucination-guard judge: a proposal claim
    about what a subsystem DOES had nothing to entail against. Timer-driven
    oneshots (scout, intel, classifier, proposal, eval) sit 'inactive dead'
    between firings, so --state=active hid exactly the units proposals cite.
    """
    try:
        r = subprocess.run(['systemctl', 'list-units', 'lumen-*', 'minerva-*',
                            '--all', '--no-legend', '--plain'],
                           capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return 'Services: PROBE FAILED (systemctl rc!=0)'

        timed = set()
        t = subprocess.run(['systemctl', 'list-timers', '--all', '--no-legend',
                            '--no-pager'], capture_output=True, text=True, timeout=20)
        if t.returncode == 0:
            for line in t.stdout.strip().splitlines():
                cols = line.split()
                if cols:
                    timed.add(cols[-1])

        rows = []
        for line in r.stdout.strip().splitlines():
            cols = line.split()
            if len(cols) < 5:
                continue
            unit, active = cols[0], cols[2]
            desc = ' '.join(cols[4:])[:58].strip()
            if unit.endswith('.path') or unit.endswith('.timer'):
                continue
            base = unit.replace('.service', '')
            if unit in timed:
                # Name the .timer verbatim: the NLI judge requires names to
                # match the claim exactly, and proposals cite "<x>.timer".
                mode = f'timer-driven by {base}.timer'
            elif active == 'active':
                mode = 'running'
            else:
                mode = 'inactive'
            rows.append(f'- {base} [{mode}]: {desc}')
        if not rows:
            return 'Services: PROBE FAILED (no units parsed)'
        return ('Lumen/Minerva systemd units — name [run mode]: what it does. '
                '"timer-driven" units are SCHEDULED and normally sit inactive '
                'between firings; that is healthy, not stopped.\n'
                + '\n'.join(sorted(rows)))
    except Exception as e:
        return f'Services: PROBE FAILED ({e})'


def _fleet_summary():
    try:
        from lumen_state import current
        rows = current(min_confidence=0.25)
        by_subj = {}
        for r in rows:
            # wq#1629: token + note, so the digest keeps the prose that used to
            # live inside `value`.
            by_subj.setdefault(r['subject'], {})[r['predicate']] = (
                r.get('value_display') or r['value'])
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
- Research-to-action conversion is noisy: many proposals are rejected at build
  for stale/duplicate premises rather than for being bad ideas.

ALREADY BUILT — do NOT propose these again (verified 2026-08-10):
- Eval/experiment harnesses EXIST and measure whether changes help: DGM v2
  paired_eval + paired-bootstrap promote gate (agent5/dgm_v2.py), skillhex
  evidence-grounded hypothesis loop, pelican_bench (local model drift),
  model_audit (external rung correctness), predicate_audit (reasoning
  grounding), judgearena_bench (judge-vs-human agreement), memory_audit
  (pgvector embedding integrity), inflation_check (impact-vs-verify).
- Automated deployer EXISTS: minerva_factory (minerva-factory.timer) runs
  PLAN -> GATE -> CANARY APPLY -> RECORD with GREEN auto-apply whitelist,
  YELLOW approval-file staging, RED never-auto, health probe + auto-revert.
- Agent prompts DO auto-adapt via DGM v2 mutation + skillhex (human-gated
  promotion). There is no /data/lumen/prompt_engine/topics.yaml — do not
  reference that path.
- Cross-model agreement scoring EXISTS (cross_agreement.py, flag-gated
  XAGREE_ENABLED) and SpanUQ-lite self-consistency voting in minerva_verify.
- Entity novelty scoring EXISTS (familiarity_scorer.py).

Before proposing a NEW capability, check whether one of the above already
covers it; a proposal that duplicates existing machinery will be rejected.

Content policy: NO publishing of any kind. Ghost CMS was permanently deleted 2026-05 for legal reasons. Research outputs MUST become integration proposals, never published content. PBox GPU node RETIRED 2026-06-07 — no GPU inference node exists. Surface Pro refurbishment business is permanently off-limits."""


def build_digest():
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    parts = [
        f'LUMEN CURRENT STATE (auto-derived, verified {now}):',
        '',
        _probe_hardware(),
        '',
        _probe_stack_versions(),
        '',
        _probe_fleet_stack(),
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




def _probe_stack_versions():
    """Installed versions of the software the fleet runs.

    Without this the digest described Lumen's own units but nothing about
    Gitea, Vaultwarden, netdata, cloudflared and the rest, so an upgrade
    proposal had no ground truth to cite and the hallucination guard scored it
    judge=0.00. Watchlist lives in /home/lumen/agent/fleet_stack.json.
    """
    lines = []
    # 1. version facts already recorded in the bi-temporal state store
    try:
        import sqlite3
        conn = sqlite3.connect('/home/lumen/minerva/minerva.db', timeout=10)
        rows = conn.execute(
            "SELECT subject, predicate, value FROM state_facts "
            "WHERE valid_to IS NULL AND (predicate LIKE '%version%' OR predicate LIKE '%_ver') "
            "ORDER BY subject, predicate").fetchall()
        conn.close()
        # wq#1501: fold same-predicate/same-value rows across hosts into one line
        # ("netdata_version = 2.11.0 on anubis,apep,bes,...") — nine near-identical
        # lines were costing ~600 chars of a capped window for one fact.
        grouped = {}
        for subj, pred, val in rows:
            grouped.setdefault((pred, str(val)[:90]), []).append(subj)
        for (pred, val), subjs in grouped.items():
            if len(subjs) == 1:
                lines.append(f'  {subjs[0]}.{pred} = {val}')
            else:
                lines.append(f'  {pred} = {val} on {",".join(sorted(subjs))}')
    except Exception as e:
        lines.append(f'  state-store versions PROBE FAILED: {e}')

    # 2. container image tags running on Lumen right now
    try:
        out = subprocess.run(
            ['docker', 'ps', '--format', '{{.Names}}\t{{.Image}}'],
            capture_output=True, text=True, timeout=20)
        for ln in out.stdout.strip().splitlines():
            if '\t' in ln:
                name, image = ln.split('\t', 1)
                lines.append(f'  lumen container {name} = {image}')
    except Exception as e:
        lines.append(f'  docker images PROBE FAILED: {e}')

    # 3. binaries on Lumen
    for binname, args in (('tailscale', ['version']), ('cloudflared', ['--version']),
                          ('ollama', ['--version'])):
        try:
            out = subprocess.run([binname] + args, capture_output=True,
                                 text=True, timeout=15)
            first = (out.stdout or out.stderr).strip().splitlines()
            if first:
                lines.append(f'  lumen {binname} = {first[0][:90]}')
        except Exception:
            pass

    body = '\n'.join(lines) if lines else '  (none resolved)'
    return ('RUNNING VERSIONS (what this fleet has INSTALLED — compare against any '
            'upstream release or advisory before proposing an upgrade):\n' + body)


FLEET_STACK = '/home/lumen/agent/fleet_stack.json'
_STACK_NAMES = {  # repo -> product name as people (and proposals) say it
    'nextcloud/server': 'Nextcloud', 'home-assistant/core': 'Home Assistant', 'plausible/analytics': 'Plausible',
    'actualbudget/actual': 'Actual Budget', 'wonderwhy-er/DesktopCommanderMCP': 'Desktop Commander (DesktopCommanderMCP)',
    'VictoriaMetrics/VictoriaLogs': 'VictoriaLogs', 'danny-avila/LibreChat': 'LibreChat', 'Klipper3d/klipper': 'Klipper',
    'TwiN/gatus': 'Gatus', 'BerriAI/litellm': 'LiteLLM', 'go-gitea/gitea': 'Gitea', 'dani-garcia/vaultwarden': 'Vaultwarden',
    'langgenius/dify': 'Dify', 'esphome/esphome': 'ESPHome', 'renovatebot/renovate': 'Renovate', 'n8n-io/n8n': 'n8n',
    'netdata/netdata': 'netdata', 'grafana/grafana': 'Grafana', 'searxng/searxng': 'SearXNG', 'binwiederhier/ntfy': 'ntfy',
    'glanceapp/glance': 'Glance', 'langfuse/langfuse': 'Langfuse', 'aquasecurity/trivy': 'Trivy', 'simonw/sqlite-utils': 'sqlite-utils',
    'supercorp-ai/supergateway': 'supergateway', 'Kludex/uvicorn': 'uvicorn', 'fastapi/fastapi': 'FastAPI',
    'qdrant/qdrant': 'Qdrant', 'pgvector/pgvector': 'pgvector', 'ggml-org/llama.cpp': 'llama.cpp (llama-server)',
    'restic/restic': 'restic', 'tailscale/tailscale': 'Tailscale', 'cloudflare/cloudflared': 'cloudflared',
    'ollama/ollama': 'Ollama', 'python-kasa/python-kasa': 'python-kasa', 'postgres/postgres': 'PostgreSQL',
}
_STACK_ALIASES = {  # repo -> extra tokens to match containers/units/state predicates
    'home-assistant/core': ['homeassistant', 'home-assistant'], 'nextcloud/server': ['nextcloud'],
    'plausible/analytics': ['plausible'], 'actualbudget/actual': ['actual'],
    'wonderwhy-er/DesktopCommanderMCP': ['desktop-commander', 'dc_version'],
    'ggml-org/llama.cpp': ['llama-server', 'llama-'], 'VictoriaMetrics/VictoriaLogs': ['victorialogs', 'vlogs'],
    'danny-avila/LibreChat': ['librechat'], 'Klipper3d/klipper': ['klipper'],
    'python-kasa/python-kasa': ['kasa', 'tapo'], 'binwiederhier/ntfy': ['ntfy'],
}


def _probe_fleet_stack():
    """wq#1386/#1501: name the PRODUCTS the fleet runs, one line each, with whatever
    installed version/unit the probes can resolve. RUNNING VERSIONS shows
    'langgenius/dify-api:1.14.2' but never the word Dify, so a true claim that the
    fleet runs Dify / Renovate / Home Assistant / ESPHome was NOT_IN_CONTEXT."""
    try:
        repos = json.load(open(FLEET_STACK)).get('repos', {})
    except Exception as e:
        return f'FLEET STACK: PROBE FAILED ({e})'
    containers, units, facts = [], [], []
    try:
        out = subprocess.run(['docker', 'ps', '--format', '{{.Names}}\t{{.Image}}'], capture_output=True, text=True, timeout=20)
        containers = [ln.split('\t', 1) for ln in out.stdout.strip().splitlines() if '\t' in ln]
    except Exception:
        pass
    try:
        out = subprocess.run(['systemctl', 'list-units', '--all', '--no-legend', '--plain', '-t', 'service,timer'],
                             capture_output=True, text=True, timeout=20)
        units = [ln.split()[0] for ln in out.stdout.splitlines() if ln.strip()]
    except Exception:
        pass
    try:
        import sqlite3
        conn = sqlite3.connect('/home/lumen/minerva/minerva.db', timeout=10)
        facts = conn.execute("SELECT subject, predicate, value FROM state_facts WHERE valid_to IS NULL "
                             "AND (predicate LIKE '%version%' OR predicate LIKE '%_ver')").fetchall()
        conn.close()
    except Exception:
        pass
    lines = []
    _UNIT_JUNK = ('lumen-job-', 'ntfy-onfailure@', '-alert', '-smoke', '-probe', '-relay', '-tap-')
    for repo, where in repos.items():
        owner, base = repo.split('/', 1)
        name = _STACK_NAMES.get(repo, base)
        toks = [base.lower()] + [a.lower() for a in _STACK_ALIASES.get(repo, [])]
        if base.lower() in ('server', 'core', 'analytics', 'actual', 'postgres'):
            toks = [a.lower() for a in _STACK_ALIASES.get(repo, [])] or [owner.lower()]
        found = []
        seen = set()
        for cname, image in containers:
            il = image.lower()
            if any(t in il for t in toks):
                short = image.rsplit('/', 1)[-1][:40]
                if short not in seen:
                    found.append(short); seen.add(short)
        vers = {}
        for subj, pred, val in facts:
            pl = pred.lower()
            if any(t in pl for t in toks):
                v = str(val).split(' ')[0].strip('(),')
                vers.setdefault(v, []).append(subj)
        for v, hosts in vers.items():
            found.append(f'{v} on {",".join(sorted(hosts))}')
        us = [u for u in units if any(t in u.lower() for t in toks) and not any(j in u for j in _UNIT_JUNK)]
        if us:
            found.append('unit ' + ','.join(sorted(set(us))[:2]))
        inst = (' | ' + '; '.join(found)) if found else ''
        lines.append(f'  - {name} [{repo}]: {where}{inst}'[:180])
    return ('FLEET STACK (products this fleet RUNS, from /home/lumen/agent/fleet_stack.json + live probes; '
            'a product listed here IS running in the fleet even when no version resolved):\n' + '\n'.join(lines))


if __name__ == '__main__':
    print(write_digest())
