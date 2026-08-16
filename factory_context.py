#!/usr/bin/env python3
"""factory_context — real-path factpack for the minerva_factory PLANNER.

ROOT CAUSE THIS FIXES (audited 2026-08-14):
  minerva_factory.PLANNER_SYS gave the planner exactly two real paths
  (litellm_config.yaml, /home/lumen/minerva/) plus "never invent paths - if you
  cannot name a real file, use unplannable". The planner obeyed: 26 of the last
  14 days' plans came back frank_required_because='unclear' / change_type=
  'unplannable' with notes literally saying "cannot name the file". Those were
  forced RED by classify_lane, so an LLM shrug was scored identically to
  "this could brick Lumen". The planner was starved, not unsafe.

Design:
  - FILE INVENTORY is the new signal: every plausible edit target under the
    Minerva/agent/bin trees, with size + first docstring line so the planner can
    pick the right file instead of guessing.
  - Reuses minerva_factpack.corpus() (services/models/artifacts/hw) when
    importable; degrades to lumen_state.txt; degrades to '' — never raises.
  - Disk cache (TTL default 3600s) because factory runs plan in batches.
  - Hard char cap (default 12000) so it cannot eat the planner's budget.
"""
import json, os, re, subprocess, sys, time
from pathlib import Path

CACHE = Path('/tmp/factory_context_cache.json')
TTL = int(os.getenv('FACTORY_CTX_TTL', '3600'))
CAP = int(os.getenv('FACTORY_CTX_CAP', '26000'))
DIGEST = Path('/home/lumen/minerva/lumen_state.txt')
LITELLM_CONFIG = '/home/lumen/agent/litellm_config.yaml'

# Trees the planner is allowed to target. Anything outside is off-limits by
# construction: if it is not in the inventory, the planner must not name it.
# (root, glob, depth, max_rows) — per-tree caps stop one big directory
# (/home/lumen/bin has 112 scripts) from crowding out the others.
TREES = [
    ('/home/lumen/minerva', '*.py', 1, 80),
    ('/home/lumen/agent', '*.py', 1, 55),
    ('/home/lumen/bin', '*', 1, 60),
]
SKIP_RE = re.compile(r'(/\.|venv|site-packages|node_modules|__pycache__|\.bak|\.tmp|/eval/|/eval_sets/|/v4/)')


def _first_doc(p: Path) -> str:
    try:
        with open(p, 'r', errors='ignore') as f:
            head = f.read(600)
    except Exception:
        return ''
    m = re.search(r'"""(.{3,180}?)(?:\n|""")', head, re.S)
    if m:
        return ' '.join(m.group(1).split())[:110]
    for line in head.splitlines():
        line = line.strip()
        if line.startswith('#') and len(line) > 8 and not line.startswith('#!'):
            return line.lstrip('# ')[:110]
    return ''


def _inventory():
    rows = []
    for root, pat, depth, maxrows in TREES:
        r = Path(root)
        if not r.is_dir():
            continue
        n0 = len(rows)
        globber = r.glob(pat) if depth == 1 else r.rglob(pat)
        for p in sorted(globber):
            if len(rows) - n0 >= maxrows:
                break
            sp = str(p)
            if SKIP_RE.search(sp) or not p.is_file():
                continue
            try:
                kb = p.stat().st_size // 1024
            except Exception:
                continue
            if kb > 400:
                continue
            rows.append(f'{sp} ({kb}K) {_first_doc(p)}'.rstrip())
    return rows


def _units():
    """unit -> ExecStart binary+script. The bare name list was not enough: a planner
    asked to touch "the DGM v2 harness" could see lumen-dgm.service but had no way to
    learn which file it runs, so it returned unplannable (probe 2026-08-14)."""
    try:
        out = subprocess.run(['systemctl', 'list-unit-files', 'lumen-*', 'minerva-*',
                              '--no-legend', '--no-pager'],
                             capture_output=True, text=True, timeout=20).stdout
        names = sorted({l.split()[0] for l in out.strip().splitlines()
                        if l.split() and l.split()[0].endswith('.service')})
        rows = []
        for n in names:
            try:
                ex = subprocess.run(['systemctl', 'show', n, '-p', 'ExecStart', '--value'],
                                    capture_output=True, text=True, timeout=10).stdout
                m = re.search(r'argv\[\]=([^;]+);', ex)
                path = ' '.join(m.group(1).split())[:110] if m else ''
            except Exception:
                path = ''
            rows.append(f'{n} -> {path}' if path else n)
        return rows
    except Exception:
        return []


def _litellm_routes():
    try:
        import yaml
        cfg = yaml.safe_load(open(LITELLM_CONFIG))
        names = [m.get('model_name', '') for m in cfg.get('model_list', [])]
        sample = None
        for m in cfg.get('model_list', []):
            if m.get('litellm_params'):
                sample = {'model_name': m['model_name'],
                          'litellm_params': {k: ('<redacted>' if 'key' in k.lower() else v)
                                             for k, v in m['litellm_params'].items()}}
                break
        return sorted(n for n in names if n), sample
    except Exception:
        return [], None


def _base_facts():
    try:
        sys.path.insert(0, '/home/lumen/minerva')
        import minerva_factpack as fp
        lines = fp.corpus()
        # [service] lines are dropped: the REAL SYSTEMD UNITS section above already
        # names every unit, and duplicating them starved the file inventory.
        return [l for l in lines if str(l).startswith(('[artifact]', '[hw]'))][:30]
    except Exception:
        try:
            return [DIGEST.read_text()[:4000]]
        except Exception:
            return []



# House conventions the planner cannot infer from a file listing. Added after the
# first staged bundle (lumen-intelliaudit, 2026-08-14) defaulted to public ntfy.sh -
# retired for publishers on 2026-08-09 - and to the legacy gpt-3.5-turbo route.
CONVENTIONS = """## LUMEN CONVENTIONS (authoritative - follow these in any code you write)
- ALL model calls go through the local LiteLLM gateway at http://localhost:4000/v1/chat/completions
  with the key from LITELLM_MASTER_KEY in /home/lumen/agent/.env. NO Anthropic or OpenAI spend, ever.
  Do not default to gpt-3.5-turbo (legacy stub) - prefer mistral-small, deepseek-v4-flash,
  cerebras-oss-120b, or a local ollama route.
- Notifications go to the SELF-HOSTED ntfy at http://127.0.0.1:8080/lumen-alerts. Public ntfy.sh was
  retired for publishers on 2026-08-09 - never write https://ntfy.sh into a new script.
- New Python lives in /home/lumen/minerva/ (Minerva pipeline) or /home/lumen/bin/ (CLI tools).
  A new unit's ExecStart must point at that final path, not at the staging directory.
- Every unit sets User=lumen and an absolute ExecStart. Timer-driven work is Type=oneshot.
- sqlite state belongs in /home/lumen/minerva/minerva.db unless there is a reason not to.
- Fail-open on anything advisory; fail-closed on anything that gates a change.
- minerva.db proposals.status vocabulary is exactly: defer, reject, rejected_aged,
  duplicate, researching, accept, deploy_ready, planned, staged, approved, deployed,
  shipped, blocked. There is NO 'rejected' - a query for it silently matches nothing.
- proposals.eval_result holds JSON that other code json.loads(). Never append text to it;
  write new findings to their own table and join on the proposal id.
- Monitoring already exists and is dense: Gatus, sentinel probes, the alert queue (aq),
  lumen-failed-units, lumen-model-watchdog, lumen-smart-watchdog, lumen-brain-health,
  lumen-sqlite-health. Before proposing another health checker, say what it catches that
  those miss - a redundant hourly LLM summary is a cost with no signal.
- Any script making repeated LLM calls over a table MUST take a batch limit from env.
  A backlog of hundreds of rows in one cron firing is a bug, not throughput.
- NEVER propose or configure a route to a paid Anthropic or OpenAI endpoint. Claude
  access is the claude-code CLI on Frank's Max subscription; ANTHROPIC_API_KEY in
  /home/lumen/agent/.env is DEFUSED and must never be re-enabled. A proposal to add
  claude-*, anthropic/*, openai/* or gpt-* to the gateway is a REJECT, not a plan -
  the claude-opus-5 route that already exists is not a licence to add more."""


def build(force=False) -> str:
    """Priority order matters: routes/units/live-facts are small and always fit;
    the file inventory gets whatever budget is left, so a long tree can never
    starve the planner of the config structure it needs."""
    if not force:
        try:
            if CACHE.exists() and (time.time() - CACHE.stat().st_mtime) < TTL:
                return json.loads(CACHE.read_text())['text']
        except Exception:
            pass
    head = [CONVENTIONS]
    routes, sample = _litellm_routes()
    if routes:
        blk = ('## LITELLM ROUTES ALREADY IN ' + LITELLM_CONFIG
               + ' (proposing one that already exists is a duplicate — say so)\n' + ', '.join(routes))
        if sample:
            blk += '\n## model_list ENTRY STRUCTURE (copy this shape)\n' + json.dumps(sample)
        head.append(blk)
    units = _units()
    if units:
        u = '\n'.join(units)
        if len(u) > 7000:
            u = u[:7000] + '\n...(+more units)'
        head.append('## REAL SYSTEMD UNITS — .service shown; most have a .timer sibling. '
                    'New units install to /etc/systemd/system\n' + u)
    facts = _base_facts()
    if facts:
        head.append('## LIVE SYSTEM FACTS\n' + '\n'.join(str(f) for f in facts))
    head_txt = '\n\n'.join(head)

    inv_budget = max(0, CAP - len(head_txt) - 400)
    inv = _inventory()
    inv_lines, used = [], 0
    for row in inv:
        if used + len(row) + 1 > inv_budget:
            break
        inv_lines.append(row); used += len(row) + 1
    inv_txt = ''
    if inv_lines:
        inv_txt = ('## REAL FILES YOU MAY TARGET (path (size) purpose) — these paths exist; '
                   'do NOT name a path that is not listed here\n' + '\n'.join(inv_lines))
        if len(inv_lines) < len(inv):
            inv_txt += f'\n[+{len(inv) - len(inv_lines)} more files not shown]'

    text = '\n\n'.join(x for x in (head_txt, inv_txt) if x)
    if len(text) > CAP:
        text = text[:CAP] + '\n[factpack truncated at cap]'
    try:
        CACHE.write_text(json.dumps({'text': text, 'built': time.time()}))
    except Exception:
        pass
    return text


if __name__ == '__main__':
    t = build(force='--force' in sys.argv)
    print(t if '--print' in sys.argv else f'factpack chars={len(t)} lines={t.count(chr(10))+1}')
