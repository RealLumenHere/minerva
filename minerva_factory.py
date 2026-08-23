#!/usr/bin/env python3
"""minerva_factory — production factory for deploy_ready proposals (2026-08-09, Frank option A).

Pipeline: PLAN -> GATE -> CANARY APPLY -> RECORD
  PLAN   : LLM (via LiteLLM, never Anthropic) materializes a proposal into a
           concrete plan: change_type + validated params + targets + rollback.
  GATE   : deterministic lane classification (GREEN/YELLOW/RED) + safety gates.
  APPLY  : GREEN only, via DETERMINISTIC APPLIERS (code, not LLM diffs):
           backup -> structured edit -> validate -> health probe -> auto-revert on fail.
  RECORD : changelog row, proposal status flip, episode commit.

LANES (conservative launch, Frank-approved 2026-08-09):
  GREEN  auto-apply whitelist: litellm_route_add, new_standalone_script (inert,
         written to /home/lumen/factory/scripts, no exec bit, no wiring).
  YELLOW everything else plannable: staged with LLM draft diff + approval file
         (reuses lumen_exec approval convention: touch <id>.approved) + ntfy.
  RED    never auto: reboot/netplan/MAC/vault-HA/dd/off-limits/no-rollback.

Usage:
  minerva-factory plan [N]          materialize plans for deploy_ready proposals
  minerva-factory list [lane]       list plans
  minerva-factory show <plan_id>    full plan detail
  minerva-factory apply <plan_id>   apply one plan (GREEN; YELLOW needs .approved)
  minerva-factory install <plan_id> --approved
                                    install a STAGED new_service bundle into
                                    /etc/systemd/system + enable/start (human-gated;
                                    undoes itself if the unit does not come up)
  minerva-factory run [N]           plan batch + auto-apply GREEN + stage YELLOW
  minerva-factory replan [N] [--apply]
                                    re-plan proposals parked at planned/staged whose
                                    RED/YELLOW verdict came from the pre-factpack planner
All destructive steps back up first; failed health = auto-revert. Never reboots.
"""
import hashlib, json, os, re, shlex, shutil, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

DB = '/home/lumen/minerva/minerva.db'
LITELLM_URL = 'http://localhost:4000/v1/chat/completions'
MODEL = os.environ.get('FACTORY_MODEL', 'deepseek-v4-pro')
# Independent second opinion for the unclear-rescue path (2026-08-14). Different
# vendor bucket on purpose: a same-family retry mostly reproduces the same shrug.
MODEL_2 = os.environ.get('FACTORY_MODEL_2', 'cerebras-oss-120b')
LITELLM_CONFIG = '/home/lumen/agent/litellm_config.yaml'
SCRIPTS_DIR = Path('/home/lumen/factory/scripts')
APPROVALS_DIR = Path('/home/lumen/instructions/approvals')
NTFY_TOPIC = 'http://127.0.0.1:8080/lumen-alerts'  # self-hosted; reaches aq subscriber + Frank's phone (TS). Public ntfy.sh retired for this publisher 2026-08-09.
# new_service is GREEN because its applier is STAGE-ONLY: it writes an inert
# bundle under /home/lumen/factory/staged/, installs nothing into /etc, enables
# nothing and starts nothing. Installation is a separate approval-gated verb.
GREEN_WHITELIST = {'litellm_route_add', 'new_standalone_script', 'new_service'}
PLANNABLE = GREEN_WHITELIST | {'code_edit', 'config_edit', 'rss_source_append',
                               'timer_schedule_tweak', 'service_change', 'new_service'}

RED_PATTERNS = [
    r'\breboot\b', r'\bshutdown\b', r'\bnetplan\b', r'\benp0s25\b',
    r'64:00:6a:58:fc:d8', r'\bmacaddress\b', r'\bdd\s+if=', r'--remove-orphans',
    r'\bvaultwarden\b.*\b(drop|delete|migrat)', r'\bpgdata\b', r'\bpg_ctl\b',
    r'surface\s*pro', r'\brm\s+-rf\s+/(?!home/lumen/(scratch|factory|tmp))',
    r'\btailscale\s+(down|logout)\b', r'/etc/ssh', r'\bmkfs\b', r'\bfdisk\b', r'\bparted\b',
]
CROWN_SERVICES = {'ma\'at', 'maat', 'wadjet', 'vaultwarden', 'cloudflared', 'tailscaled', 'ssh', 'sshd'}

def now(): return datetime.now(timezone.utc).isoformat()
def log(m): print(f'[{datetime.now(timezone.utc).strftime("%H:%M:%S UTC")}] {m}', flush=True)
def sh(cmd, timeout=90):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()

def db():
    import sqlite3
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    return c

def migrate():
    c = db()
    c.execute('''CREATE TABLE IF NOT EXISTS plans (
        id TEXT PRIMARY KEY, proposal_id TEXT, lane TEXT, change_type TEXT,
        params TEXT, targets TEXT, draft_diff TEXT,
        apply_cmd TEXT, health_cmd TEXT, rollback_cmd TEXT,
        gate_result TEXT, status TEXT DEFAULT 'planned',
        notes TEXT, created_at TEXT, applied_at TEXT, reverted INTEGER DEFAULT 0)''')
    c.commit(); c.close()

def litellm_key():
    for line in open('/home/lumen/agent/.env'):
        if line.startswith('LITELLM_MASTER_KEY='):
            return line.split('=', 1)[1].strip()
    raise RuntimeError('LITELLM_MASTER_KEY not found')

# ---------------- PLAN ----------------
PLANNER_SYS = """You are Lumen's deployment planner. Turn an approved improvement proposal
into ONE concrete, minimal change plan as STRICT JSON (no markdown, no prose).

Schema:
{
 "change_type": "litellm_route_add|new_standalone_script|rss_source_append|timer_schedule_tweak|code_edit|config_edit|service_change|new_service|need_files|unplannable",
 "params": {},
 "targets": ["/absolute/path", ...],
 "rationale": "1-2 sentences",
 "draft_diff": "unified diff or full new-file content, or empty string",
 "apply_cmd": "single shell command to activate the change, or empty",
 "health_cmd": "single shell command that exits 0 iff healthy, or empty",
 "rollback_cmd": "single shell command to revert, or empty",
 "restart_units": ["unit-or-container-names"],
 "frank_required_because": "none|reboot|crown_jewel|irreversible|physical|unclear"
}

frank_required_because is a SAFETY field, not a confidence field. Use "unclear"
ONLY when you cannot tell whether the change is SAFE (could it touch a crown service,
could it be irreversible). NEVER use it to mean "I do not know which file to edit" -
the FACTPACK appended to the user message lists every real path, unit and route. If the
factpack still lacks what you need, answer change_type "unplannable" and name the missing
fact in "rationale"; do not raise it as a safety concern.

If you cannot write a concrete diff because you have not seen the code you would be
editing, do NOT answer "unplannable" - ask for the file. Answer:
  {"change_type": "need_files", "params": {"files": ["/abs/path1", "/abs/path2"]}}
(at most 3 files, each one listed in the FACTPACK). Their contents will be handed back
to you and you will then produce the real plan. This is a free move; use it whenever a
diff would otherwise be guesswork.

params by change_type:
- litellm_route_add: {"model_name": "<route name>", "litellm_params": {"model": "<provider/model>", ...}}
- new_standalone_script: {"filename": "name.py", "content": "<full file content>"}
- new_service: {"name": "lumen-foo", "script_filename": "foo.py",
                "script_content": "<full script>", "unit_content": "<full .service file>",
                "timer_content": "<full .timer file, or empty string for a long-running service>"}
  For new_service the unit MUST set User=lumen and an absolute ExecStart. Do NOT
  write Install/WantedBy lines that assume the unit is already installed - the
  factory only STAGES the bundle to disk; a human installs it.
- others: free-form, but targets+draft_diff must be concrete.

Rules:
- Known facts: LiteLLM gateway config is /home/lumen/agent/litellm_config.yaml (docker container 'litellm').
  Minerva code lives in /home/lumen/minerva/. A FACTPACK of real paths, units and routes
  is appended to the user message - target ONLY paths listed there, never invent one.
  If the needed file genuinely is not in the factpack, use change_type "unplannable".
- Never propose reboots, netplan/MAC changes, vault/postgres data ops, or anything touching the Surface Pro business.
- Rollback_cmd is MANDATORY for any change that modifies an existing file. Omitting it
  is the single most common reason a good plan is thrown out, so always write one, and
  make it a real command, e.g.
    cp /home/lumen/minerva/foo.py.bak.PREAPPLY /home/lumen/minerva/foo.py
  paired with an apply_cmd that creates that .bak.PREAPPLY copy first.
- Prefer the smallest change that delivers the proposal."""

FENCE = chr(96) * 3
INJ_TOKENS = ('$' + '(', chr(96), ';', '&&', '||', chr(10))

def _factpack():
    """Real-path context for the planner. Fail-open: '' degrades to the old
    starved behaviour rather than blocking a plan."""
    try:
        import factory_context
        return factory_context.build()
    except Exception as e:
        log(f'  factpack unavailable (fail-open): {str(e)[:120]}')
        return ''


def llm_plan(prop, context='', model=None):
    # aq#353-class handling (see minerva_proposal.cerebras_gen 2026-08-07):
    # deepseek reasoning can exhaust max_tokens -> content="". Use a high cap,
    # 429 backoff, and one escalation retry on empty content.
    import requests
    user = (f"PROPOSAL {prop['id']}: {prop['title']}\n\nCURRENT STATE:\n{prop['current_state']}\n\n"
            f"PROPOSED CHANGE:\n{prop['proposed_change']}\n\nROLLBACK HINT:\n{prop['rollback']}\n"
            f"effort={prop['effort']} impact={prop['impact']} risk={prop['risk']}\n{context}")
    model = model or MODEL
    # 6000 truncated plans that embed a whole script (new_service /
    # new_standalone_script) mid-string -> "Unterminated string" JSON errors,
    # measured on the 2026-08-14 replan probe. Cap raised and escalated on any
    # parse failure, not just on empty content.
    body = {'model': model, 'max_tokens': 12000, 'temperature': 0.1,
            'messages': [{'role': 'system', 'content': PLANNER_SYS},
                         {'role': 'user', 'content': user}]}
    if 'deepseek' in str(model):
        body['response_format'] = {'type': 'json_object'}
    last_err = 'no_attempts'
    for attempt in range(4):
        try:
            r = requests.post(LITELLM_URL, timeout=180, json=body,
                headers={'Authorization': f'Bearer {litellm_key()}', 'Content-Type': 'application/json'})
            if r.status_code == 429:
                time.sleep(4 * (attempt + 1)); last_err = 'http_429'; continue
            r.raise_for_status()
            txt = (r.json()['choices'][0]['message']['content'] or '').strip()
            if not txt:
                body['max_tokens'] = 10000; last_err = 'empty_content'
                time.sleep(2); continue
            if txt.startswith(FENCE):
                txt = txt.strip(FENCE).lstrip('json').strip()
            return json.loads(txt)
        except json.JSONDecodeError as e:
            # Almost always truncation mid-string: give the next attempt more room.
            body['max_tokens'] = min(int(body['max_tokens'] * 1.6), 24000)
            last_err = f'json_decode:{str(e)[:90]}'
            time.sleep(2)
        except Exception as e:
            last_err = str(e)[:120]; time.sleep(2)
    raise RuntimeError(last_err)

# ---------------- GATE ----------------
def classify_lane(prop, plan, rescued=False):
    """Deterministic. Returns (lane, reasons[]).

    rescued=True means this plan came from the second-opinion planner after the
    first one could not classify it. Those are never auto-applied: a rescued GREEN
    is capped to YELLOW so a human still signs off on the change the second model
    invented. Fail-closed direction only - rescue can never raise a lane."""
    blob = json.dumps(plan) + ' ' + prop['proposed_change'] + ' ' + prop['title']
    low = blob.lower()
    reasons = []
    for pat in RED_PATTERNS:
        if re.search(pat, low):
            reasons.append(f'red_pattern:{pat}')
    if plan.get('frank_required_because', 'none') not in ('none', ''):
        reasons.append('planner_flag:' + plan['frank_required_because'])
    for u in plan.get('restart_units', []) or []:
        if u.lower().strip() in CROWN_SERVICES:
            reasons.append(f'crown_service_restart:{u}')
    ct = plan.get('change_type', 'unplannable')
    modifies_existing = any(Path(t).exists() for t in plan.get('targets', []) or [])
    if modifies_existing and not (plan.get('rollback_cmd') or '').strip() and ct not in GREEN_WHITELIST:
        reasons.append('no_rollback_for_existing_file')
    if reasons:
        return 'red', reasons
    if ct in GREEN_WHITELIST:
        if rescued:
            return 'yellow', ['rescued_capped_to_yellow:' + ct]
        return 'green', ['whitelisted:' + ct]
    if ct in PLANNABLE:
        return 'yellow', ['plannable_not_whitelisted:' + ct]
    return 'red', ['unplannable']

def run_gates(prop, plan, lane):
    """Pre-apply checks against the MATERIALIZED plan."""
    checks = {}
    tgts = plan.get('targets', []) or []
    ok = True; missing = []
    for t in tgts:
        p = Path(t)
        if not p.exists() and not p.parent.exists():
            ok = False; missing.append(t)
    checks['targets_resolvable'] = {'passed': ok, 'missing': missing}
    svc_ok = True; svc = {}
    for unit in ('lumen-fleet-cerebras', 'lumen-fleet-mistral', 'lumen-fleet-openrouter'):
        rc, out = sh(f'systemctl is-active {unit}.service')
        svc[unit] = out; svc_ok = svc_ok and out == 'active'
    checks['fleet_services'] = {'passed': svc_ok, 'detail': svc}
    ct = plan.get('change_type')
    if ct == 'litellm_route_add':
        checks['litellm'] = _gate_litellm(plan)
    if ct == 'new_standalone_script':
        checks['script'] = _gate_script(plan)
    if ct == 'new_service':
        checks['service_bundle'] = _gate_new_service(plan)
    all_passed = all(v.get('passed') for v in checks.values())
    return {'all_passed': all_passed, 'checks': checks, 'lane': lane}

# Providers the fleet must never route to. Standing order: no per-token billing to
# Anthropic or OpenAI - Claude access is the claude-code CLI on Frank's Max plan, and
# ANTHROPIC_API_KEY in /home/lumen/agent/.env is DEFUSED. The factory auto-applied an
# 'anthropic-fable5' route on 2026-08-15 because nothing in the gate encoded that; the
# route was live for 52 minutes before Claude caught and reverted it. Encoded now.
FORBIDDEN_PROVIDERS = ('anthropic/', 'claude-', 'openai/', 'gpt-4', 'gpt-5', 'azure/')
FORBIDDEN_KEY_ENVS = ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY')


def _gate_litellm(plan):
    import yaml
    p = plan.get('params', {}) or {}
    name = p.get('model_name', ''); lp = p.get('litellm_params', {})
    blob = json.dumps(lp).lower() + ' ' + str(name).lower()
    for prov in FORBIDDEN_PROVIDERS:
        if prov in blob:
            return {'passed': False, 'reason': f'forbidden_paid_provider:{prov}'}
    for env in FORBIDDEN_KEY_ENVS:
        if env.lower() in blob:
            return {'passed': False, 'reason': f'forbidden_api_key_env:{env}'}
    if not name or not isinstance(lp, dict) or not lp.get('model'):
        return {'passed': False, 'reason': 'params_incomplete'}
    if not re.fullmatch(r'[A-Za-z0-9._/-]{2,64}', name):
        return {'passed': False, 'reason': 'bad_model_name'}
    bad = [k for k, v in _flat(lp) if not isinstance(v, (str, int, float, bool))]
    if bad:
        return {'passed': False, 'reason': f'non_scalar_params:{bad}'}
    joined = json.dumps(lp)
    if any(w in joined for w in INJ_TOKENS):
        return {'passed': False, 'reason': 'suspicious_param_content'}
    cfg = yaml.safe_load(open(LITELLM_CONFIG))
    if any(m.get('model_name') == name for m in cfg.get('model_list', [])):
        return {'passed': False, 'reason': f'route_exists:{name}'}
    return {'passed': True}

def _flat(d, pre=''):
    for k, v in d.items():
        if isinstance(v, dict):
            yield from _flat(v, pre + k + '.')
        else:
            yield pre + k, v

def _gate_script(plan):
    p = plan.get('params', {}) or {}
    fn = p.get('filename', ''); content = p.get('content', '')
    if not re.fullmatch(r'[A-Za-z0-9._-]{3,80}', fn) or not content:
        return {'passed': False, 'reason': 'bad_filename_or_empty'}
    low = content.lower()
    for pat in RED_PATTERNS:
        if re.search(pat, low):
            return {'passed': False, 'reason': f'red_pattern_in_content:{pat}'}
    return {'passed': True}



def _execstart_script(unit_text):
    """The script path an installed unit would run (first non-interpreter argument)."""
    m = re.search(r'^ExecStart\s*=\s*(.+)$', unit_text or '', re.M)
    if not m:
        return ''
    parts = shlex.split(m.group(1).strip().lstrip('-@+!'))
    for tok in parts:
        if tok.startswith('/') and not tok.startswith(('/usr/bin/python', '/usr/bin/env', '/bin/')):
            return tok
    return parts[0] if parts else ''


SERVICE_NAME_RE = re.compile(r'^[a-z][a-z0-9-]{2,40}$')


def _gate_new_service(plan):
    """Static validation of a staged service bundle. Nothing is installed or run,
    so this only has to prove the bundle is inert and well-formed."""
    p = plan.get('params', {}) or {}
    name = (p.get('name') or '').strip()
    script = p.get('script_content') or ''
    unit = p.get('unit_content') or ''
    timer = p.get('timer_content') or ''
    fn = (p.get('script_filename') or '').strip()
    if not SERVICE_NAME_RE.fullmatch(name):
        return {'passed': False, 'reason': f'bad_service_name:{name[:40]}'}
    if not re.fullmatch(r'[A-Za-z0-9._-]{3,80}', fn) or not script.strip():
        return {'passed': False, 'reason': 'bad_script_filename_or_empty'}
    if 'ExecStart' not in unit:
        return {'passed': False, 'reason': 'unit_missing_execstart'}
    # The unit's ExecStart names where the script must LIVE once installed; the
    # bundle only stages it. Validate that destination now so install cannot be
    # handed a path outside Lumen's trees or one that would clobber a real file.
    dest = _execstart_script(unit)
    if not dest:
        return {'passed': False, 'reason': 'execstart_names_no_script'}
    if not dest.startswith(FILE_ALLOW) or '..' in dest:
        return {'passed': False, 'reason': f'execstart_outside_allowed_trees:{dest[:80]}'}
    if Path(dest).exists():
        return {'passed': False, 'reason': f'execstart_would_clobber:{dest[:80]}'}
    rc, out = sh(f'systemctl list-unit-files {shlex.quote(name)}.service --no-legend --no-pager')
    if out.strip():
        return {'passed': False, 'reason': f'unit_exists:{name}.service'}
    blob = (script + chr(10) + unit + chr(10) + timer).lower()
    for pat in RED_PATTERNS:
        if re.search(pat, blob):
            return {'passed': False, 'reason': f'red_pattern_in_bundle:{pat}'}
    if timer.strip() and 'OnCalendar' not in timer and 'OnUnitActiveSec' not in timer:
        return {'passed': False, 'reason': 'timer_has_no_schedule'}
    return {'passed': True}


# ---------------- APPLIERS (deterministic; GREEN lane never applies LLM diffs) ----------------
def apply_litellm_route_add(plan, rec):
    import yaml
    p = plan['params']; name = p['model_name']
    ts = time.strftime('%Y%m%d_%H%M%S')
    bak = f'{LITELLM_CONFIG}.bak.{ts}'
    shutil.copy2(LITELLM_CONFIG, bak); rec('backup', bak)
    cfg = yaml.safe_load(open(LITELLM_CONFIG))
    cfg.setdefault('model_list', []).append(
        {'model_name': name, 'litellm_params': p['litellm_params']})
    tmp = LITELLM_CONFIG + '.factory.tmp'
    with open(tmp, 'w') as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    yaml.safe_load(open(tmp))  # re-parse validation
    os.replace(tmp, LITELLM_CONFIG); rec('config_written', name)
    rc, out = sh('docker restart litellm', timeout=120); rec('restart', out[:200])
    if rc != 0:
        return _revert_litellm(bak, rec, f'restart_failed:{out[:200]}')
    key = litellm_key()
    deadline = time.time() + 90
    healthy = False
    while time.time() < deadline:
        rc, out = sh(f'curl -fsS -m 5 -H "Authorization: Bearer {key}" http://localhost:4000/v1/models', timeout=10)
        if rc == 0 and name in out:
            healthy = True; break
        time.sleep(5)
    if not healthy:
        return _revert_litellm(bak, rec, 'health_probe_failed_route_not_visible')
    rec('health', 'route_visible')
    return True, f'route {name} live, backup {bak}'

def _revert_litellm(bak, rec, why):
    shutil.copy2(bak, LITELLM_CONFIG)
    rc, out = sh('docker restart litellm', timeout=120)
    rec('REVERTED', f'{why}; restore_rc={rc}')
    key = litellm_key()
    time.sleep(10)
    rc2, _ = sh(f'curl -fsS -m 5 -H "Authorization: Bearer {key}" http://localhost:4000/v1/models', timeout=10)
    rec('revert_health', 'gateway_up' if rc2 == 0 else 'GATEWAY_DOWN_ALERT')
    if rc2 != 0:
        _ntfy(f'FACTORY ALERT: litellm gateway unhealthy after revert ({why}) - manual attention needed')
    return False, f'reverted: {why}'

def apply_new_standalone_script(plan, rec):
    p = plan['params']
    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    dest = SCRIPTS_DIR / p['filename']
    if dest.exists():
        return False, f'exists:{dest}'
    dest.write_text(p['content'])
    os.chmod(dest, 0o644)  # inert: no exec bit
    rec('script_written', str(dest))
    if str(dest).endswith('.py'):
        rc, out = sh(f'python3 -m py_compile {shlex.quote(str(dest))}')
        if rc != 0:
            dest.unlink(); rec('REVERTED', 'py_compile_failed')
            return False, f'py_compile_failed:{out[:200]}'
    return True, f'inert script staged at {dest} (no exec bit, not wired)'


STAGED_DIR = Path('/home/lumen/factory/staged')


def apply_new_service(plan, rec):
    """STAGE ONLY. Writes an inert bundle (script + unit + timer + INSTALL.md) to
    /home/lumen/factory/staged/<name>/. Installs nothing, enables nothing, starts
    nothing - that is minerva_factory.py install <plan_id>, which needs approval.

    WHY THIS EXISTS: 'new_service' was PLANNABLE but had no applier, so 40 plans in
    14 days went YELLOW -> staged -> and then sat forever waiting for a human to
    hand-write the code. The factory can safely do the writing; only activation
    needs a human."""
    p = plan['params']
    name = p['name']
    dest = STAGED_DIR / name
    if dest.exists():
        return False, f'already_staged:{dest}'
    dest.mkdir(parents=True)
    script = dest / p['script_filename']
    script.write_text(p['script_content'])
    os.chmod(script, 0o644)  # inert: no exec bit
    rec('script_staged', str(script))
    if script.suffix == '.py':
        rc, out = sh(f'python3 -m py_compile {shlex.quote(str(script))}')
        if rc != 0:
            shutil.rmtree(dest, ignore_errors=True)
            rec('REVERTED', 'py_compile_failed')
            return False, f'py_compile_failed:{out[:200]}'
    unit = dest / f'{name}.service'
    unit.write_text(p['unit_content'])
    rec('unit_staged', str(unit))
    timer_txt = (p.get('timer_content') or '').strip()
    timer = None
    if timer_txt:
        timer = dest / f'{name}.timer'
        timer.write_text(timer_txt)
        rec('timer_staged', str(timer))
    # systemd-analyze verify runs against the staged copies; it parses only.
    rc, out = sh(f'systemd-analyze verify {shlex.quote(str(unit))}', timeout=60)
    if rc != 0 and 'Failed' in out:
        shutil.rmtree(dest, ignore_errors=True)
        rec('REVERTED', f'unit_verify_failed:{out[:200]}')
        return False, f'unit_verify_failed:{out[:200]}'
    rec('unit_verify', out[:200] or 'clean')
    (dest / 'INSTALL.md').write_text(
        f'# {name} - STAGED, NOT INSTALLED\n\n'
        f'Nothing here is wired. To install (human-gated):\n\n'
        f'    cp {script} {_execstart_script(p["unit_content"]) or "<ExecStart path>"}\n'
        f'    sudo cp {unit} /etc/systemd/system/\n'
        + (f'    sudo cp {timer} /etc/systemd/system/\n' if timer else '')
        + f'    sudo systemctl daemon-reload\n'
        f'    sudo systemctl enable --now {name}.' + ('timer' if timer else 'service') + '\n\n'
        f'Or, with the approval gate:\n'
        f'    touch {APPROVALS_DIR}/F<plan_id>.approved\n'
        f'    python3 /home/lumen/minerva/minerva_factory.py install <plan_id>\n\n'
        f'To discard: rm -r {dest}\n')
    return True, f'service bundle staged inert at {dest} (nothing installed or started)'


APPLIERS = {'litellm_route_add': apply_litellm_route_add,
            'new_service': apply_new_service,
            'new_standalone_script': apply_new_standalone_script}

def _ntfy(msg):
    try:
        import requests
        requests.post(NTFY_TOPIC, data=msg.encode(), timeout=8,
                      headers={'Title': 'minerva-factory', 'Priority': 'high'})
    except Exception:
        pass

# ---------------- RECORD ----------------
def record_changelog(plan_row, applied, detail):
    c = db()
    c.execute('INSERT INTO changelog (proposal_id, change_type, target_path, diff, applied_by, applied_at, reverted, revert_reason) VALUES (?,?,?,?,?,?,?,?)',
              (plan_row['proposal_id'], plan_row['change_type'],
               json.dumps(json.loads(plan_row['targets'] or '[]')),
               (plan_row['draft_diff'] or '')[:4000] or json.dumps(json.loads(plan_row['params'] or '{}'))[:4000],
               'minerva-factory', now(), 0 if applied else 1, None if applied else detail[:500]))
    c.commit(); c.close()

def episode_commit(summary):
    sh('python3 /home/lumen/minerva/lumen_episode.py commit --summary ' + shlex.quote(summary)
       + ' --kind infra --tags factory,minerva', timeout=60)



# Trees the planner may read from during a need_files round. Read-only, and a path
# outside these prefixes is ignored rather than opened.
FILE_ALLOW = ('/home/lumen/minerva/', '/home/lumen/agent/', '/home/lumen/agents/', '/home/lumen/bin/')
FILE_READ_CAP = int(os.environ.get('FACTORY_FILE_CAP', '15000'))


def _read_requested_files(paths, cap=FILE_READ_CAP):
    """Hand the planner the code it is being asked to edit.

    WHY: the 2026-08-14 probe showed the remaining unplannables were all the same
    complaint - "cannot produce a concrete diff because the factpack does not include
    the current code". Paths alone are not enough to write a diff; the file is."""
    out, budget = [], cap
    for raw in list(paths or [])[:3]:
        pth = str(raw)
        if not pth.startswith(FILE_ALLOW) or '..' in pth:
            continue
        f = Path(pth)
        if not f.is_file():
            continue
        try:
            txt = f.read_text(errors='ignore')
        except Exception:
            continue
        take = max(2000, min(len(txt), budget))
        chunk = txt[:take] + ('\n...[file truncated]' if take < len(txt) else '')
        out.append(f'### FILE {pth} ({len(txt)} chars total)\n{chunk}')
        budget -= take
        if budget <= 0:
            break
    return '\n\n'.join(out)


# ---------------- UNCLEAR RESCUE ----------------
# Audited 2026-08-14: 26 of 14 days' plans died RED on frank_required_because=
# 'unclear' with all_passed=true, no red pattern and no crown service. Every one
# of their rationales said some version of "cannot name the file" - the planner
# was starved of paths, not detecting danger. A planner shrug is now retried once
# against an independent model WITH the factpack; only a shrug that survives both
# models is treated as a stop.
RESCUE_HINT = ("\n\nSECOND-OPINION PASS. A first planner could not produce a concrete plan "
               "(reason: %s). You are an independent planner. Everything you need is in the "
               "FACTPACK above: real file paths, real unit names, real routes. Choose the most "
               "specific change_type those paths can support. Use 'unplannable' ONLY if no listed "
               "file is relevant, and use frank_required_because='unclear' ONLY for genuine SAFETY "
               "ambiguity - never for missing information.")


def _needs_rescue(plan):
    return (plan.get('change_type') in (None, '', 'unplannable')
            or plan.get('frank_required_because') == 'unclear')



def _plan_with_files(prop, ctx, model=None):
    """One planning attempt, including a single need_files round.

    Returns (plan, files_block). Both the primary and the rescue planner get this -
    an earlier version only served need_files on the primary, so a rescue that asked
    for a file was scored unplannable and died RED holding a question nobody
    answered. The fetched block is handed back so the rescue starts with the code
    already in hand instead of spending its one free move re-asking for it."""
    plan = llm_plan(dict(prop), context=ctx, model=model)
    if plan.get('change_type') != 'need_files':
        return plan, ''
    req = (plan.get('params') or {}).get('files') or plan.get('targets') or []
    body = _read_requested_files(req)
    log(f'  need_files{"(rescue)" if model else ""}: {req} -> {len(body)} chars returned')
    if not body:
        plan['change_type'] = 'unplannable'
        return plan, ''
    files_block = '\n\n## FILE CONTENTS YOU REQUESTED\n' + body
    try:
        plan = llm_plan(dict(prop), context=ctx + files_block, model=model)
    except Exception as e:
        log(f'  need_files replan failed: {str(e)[:120]}')
        return plan, files_block
    if plan.get('change_type') == 'need_files':
        # asked twice - stop the loop rather than fetching forever
        plan['change_type'] = 'unplannable'
    return plan, files_block


# ---------------- ORCHESTRATION ----------------
def plan_one(prop):
    fp = _factpack()
    ctx = ('\n\nFACTPACK (authoritative - these paths/units/routes exist; nothing else does):\n'
           + fp) if fp else ''
    try:
        plan, files_block = _plan_with_files(prop, ctx)
    except Exception as e:
        return None, f'planner_error:{e}'

    rescued = False
    if _needs_rescue(plan):
        why = f"change_type={plan.get('change_type')} flag={plan.get('frank_required_because')}"
        log(f'  rescue: first planner gave up ({why}) -> retry on {MODEL_2}')
        try:
            plan2, _ = _plan_with_files(prop, ctx + files_block + (RESCUE_HINT % why),
                                        model=MODEL_2)
            if not _needs_rescue(plan2):
                plan = plan2
                rescued = True
                log(f"  rescue OK: {MODEL_2} planned change_type={plan.get('change_type')}")
        except Exception as e:
            log(f'  rescue failed (fail-closed, stays RED): {str(e)[:120]}')
        if not rescued and plan.get('frank_required_because') == 'unclear':
            # Distinguishes 'two independent planners with full context could not
            # classify this' from the old single-shrug RED.
            plan['frank_required_because'] = 'unclear_after_2'
    lane, reasons = classify_lane(prop, plan, rescued=rescued)
    if rescued:
        reasons = reasons + ['rescued_by:' + MODEL_2]
    gates = run_gates(prop, plan, lane)
    pid = hashlib.md5(f"{prop['id']}:{json.dumps(plan, sort_keys=True)}".encode()).hexdigest()[:12]
    c = db()
    c.execute('INSERT OR REPLACE INTO plans (id, proposal_id, lane, change_type, params, targets, draft_diff, apply_cmd, health_cmd, rollback_cmd, gate_result, status, notes, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
              (pid, prop['id'], lane, plan.get('change_type'),
               json.dumps(plan.get('params', {}))[:8000],
               json.dumps(plan.get('targets', [])),
               (plan.get('draft_diff') or '')[:8000],
               plan.get('apply_cmd', ''), plan.get('health_cmd', ''), plan.get('rollback_cmd', ''),
               json.dumps({'lane_reasons': reasons, **gates})[:4000],
               'planned',
               (('[rescued by ' + MODEL_2 + '] ') if rescued else '') + plan.get('rationale', '')[:500],
               now()))
    c.execute("UPDATE proposals SET status='planned' WHERE id=?", (prop['id'],))
    c.commit(); c.close()
    return pid, lane

# --- wq#675: post-deploy verification gate ---
# 'deployed' is no longer a flag-flip. Every status transition to 'deployed'
# must pass a liveness/health check. The verify step runs AFTER the applier
# succeeds and BEFORE the proposal status is set.

def _run_health_cmd(cmd, timeout=30):
    """Run a health_cmd string; returns (exit_code, stdout[:500])."""
    if not cmd or not cmd.strip():
        return None, 'no_health_cmd'
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()[:500]
    except subprocess.TimeoutExpired:
        return 1, f'health_cmd timed out after {timeout}s'
    except Exception as e:
        return 1, f'health_cmd error: {str(e)[:200]}'

# Type-specific default verification when no health_cmd is provided.
_VERIFY_DEFAULTS = {
    'litellm_route_add': lambda p: _verify_litellm_route(p),
    'new_standalone_script': lambda p: _verify_script_exists(p),
    'new_service': lambda p: _verify_service_staged(p),
}

def _verify_litellm_route(plan_row):
    """Check the route name actually appears in the gateway's model list."""
    params = json.loads(plan_row['params'] or '{}')
    name = params.get('model_name', '')
    if not name:
        return False, 'no model_name in params'
    key = litellm_key()
    rc, out = sh(f'curl -fsS -m 10 -H "Authorization: Bearer {key}" http://localhost:4000/v1/models', timeout=15)
    if rc != 0:
        return False, f'gateway unreachable (rc={rc})'
    if name in out:
        return True, f'route {name} visible in /v1/models'
    return False, f'route {name} NOT in /v1/models response'

def _verify_script_exists(plan_row):
    """Check the script file exists and is syntactically valid."""
    params = json.loads(plan_row['params'] or '{}')
    fname = params.get('filename', '')
    dest = SCRIPTS_DIR / fname
    if not dest.exists():
        return False, f'{dest} does not exist'
    if str(dest).endswith('.py'):
        rc, out = sh(f'python3 -m py_compile {shlex.quote(str(dest))}')
        if rc != 0:
            return False, f'py_compile failed: {out[:200]}'
    return True, f'{dest} exists and valid'

def _verify_service_staged(plan_row):
    """new_service applier is STAGE-ONLY. Staged != deployed. Always returns
    False because nothing is live — the proposal should be 'applied_unverified'
    until install_staged_service() is used and the unit comes up."""
    params = json.loads(plan_row['params'] or '{}')
    name = params.get('name', '')
    # Check if the service was actually installed (via install_staged_service)
    rc, out = sh(f'systemctl is-active {shlex.quote(name + ".service")} 2>/dev/null || systemctl is-active {shlex.quote(name + ".timer")} 2>/dev/null', timeout=10)
    if rc == 0 and out.strip() in ('active', 'waiting'):
        return True, f'{name} is running ({out.strip()})'
    return False, f'{name} not active (staged only, not installed)'

def verify_deployment(plan_row, rec=None):
    """Post-apply verification gate (wq#675).

    Order:
      1. If plan has a non-empty health_cmd, run it. Exit 0 = verified.
      2. Else fall back to type-specific default check.
      3. No check available = fail-closed (unverified).
    """
    if rec is None:
        def rec(step, detail): log(f'  verify: {step}: {detail}')

    health_cmd = (plan_row['health_cmd'] if 'health_cmd' in plan_row.keys() else '').strip() if plan_row else ''
    change_type = plan_row['change_type'] if plan_row and 'change_type' in plan_row.keys() else ''

    # 1. Explicit health_cmd takes priority
    if health_cmd:
        rc, out = _run_health_cmd(health_cmd)
        if rc == 0:
            return True, f'health_cmd passed: {out[:150]}'
        return False, f'health_cmd failed (rc={rc}): {out[:200]}'

    # 2. Type-specific default
    checker = _VERIFY_DEFAULTS.get(change_type)
    if checker:
        return checker(plan_row)

    # 3. Fail-closed: unknown change_type with no health_cmd = unverified
    return False, f'no verification available for change_type={change_type}'

def verify_proposal(proposal_id):
    """CLI entry: re-verify a deployed (or applied_unverified) proposal.
    Finds the plan, runs verification, updates status accordingly.
    Returns (verified, detail)."""
    c = db()
    plan = c.execute('SELECT * FROM plans WHERE proposal_id=? ORDER BY created_at DESC LIMIT 1', (proposal_id,)).fetchone()
    prop = c.execute('SELECT * FROM proposals WHERE id=?', (proposal_id,)).fetchone()
    c.close()
    if not prop:
        return False, f'proposal {proposal_id} not found'
    if not plan:
        return False, f'no plan for proposal {proposal_id} — cannot verify (was a raw status flip)'
    ok, detail = verify_deployment(plan)
    c = db()
    if ok:
        c.execute('UPDATE proposals SET status=?, deployed_at=? WHERE id=? AND status IN (?, ?)',
                  ('deployed', now(), proposal_id, 'applied_unverified', 'deployed'))
    else:
        c.execute('UPDATE proposals SET status=?, deployed_at=NULL WHERE id=? AND status=?',
                  ('applied_unverified', proposal_id, 'deployed'))
    c.commit(); c.close()
    log(f'verify {proposal_id}: {"PASS" if ok else "FAIL"} — {detail}')
    return ok, detail


def apply_plan(plan_id, approved=False):
    c = db()
    row = c.execute('SELECT * FROM plans WHERE id=?', (plan_id,)).fetchone()
    c.close()
    if not row:
        return False, 'no_such_plan'
    if row['status'] not in ('planned', 'staged'):
        return False, f"bad_status:{row['status']}"
    gates = json.loads(row['gate_result'] or '{}')
    if row['lane'] == 'red':
        return False, 'RED lane - Frank required: ' + ','.join(gates.get('lane_reasons', []))
    if row['lane'] == 'yellow':
        ok_file = APPROVALS_DIR / f'F{plan_id}.approved'
        if not (approved and ok_file.exists()):
            return False, f'YELLOW - needs approval: touch {ok_file}'
    if not gates.get('all_passed'):
        return False, 'gates_failed: ' + json.dumps(gates.get('checks', {}))[:300]
    applier = APPLIERS.get(row['change_type'])
    if not applier:
        return False, f"no_applier_for:{row['change_type']} (yellow items with draft diffs are applied by Claude/Frank, not the factory)"
    trail = []
    def rec(step, detail): trail.append(f'{step}: {detail}'); log(f'  {step}: {detail}')
    plan = {'params': json.loads(row['params'] or '{}'),
            'targets': json.loads(row['targets'] or '[]'),
            'change_type': row['change_type']}
    ok, detail = applier(plan, rec)
    c = db()
    c.execute('UPDATE plans SET status=?, applied_at=?, reverted=?, notes=notes || ? WHERE id=?',
              ('applied' if ok else 'failed_reverted', now(), 0 if ok else 1,
               chr(10) + chr(10).join(trail)[:2000], plan_id))
    # --- wq#675: post-deploy verification gate ---
    # Applier success means "action completed" (file written, config edited).
    # 'deployed' requires a SEPARATE liveness/health check to pass.
    if ok:
        verified, verify_detail = verify_deployment(row, rec)
        if verified:
            prop_status, prop_ts = 'deployed', now()
            rec('VERIFIED', verify_detail)
        else:
            prop_status, prop_ts = 'applied_unverified', None
            rec('VERIFY_FAILED', verify_detail)
    else:
        prop_status, prop_ts = 'deploy_ready', None
    c.execute('UPDATE proposals SET status=?, deployed_at=? WHERE id=?',
              (prop_status, prop_ts, row['proposal_id']))
    c.commit(); c.close()
    record_changelog(row, ok, detail)
    final_label = f"{'APPLIED' if ok else 'FAILED+REVERTED'}"
    if ok:
        final_label += f" [{'VERIFIED' if verified else 'UNVERIFIED'}]"
    episode_commit(f"factory {final_label} plan {plan_id} ({row['change_type']}) for proposal {row['proposal_id']}: {detail[:150]}")
    return ok, detail


def install_staged_service(plan_id, approved=False):
    """Approval-gated activation of a staged new_service bundle.

    This is the ONLY step that touches /etc/systemd/system or starts anything, so
    it never runs from the timer - a human runs it after touching the .approved
    file. Fail path un-does everything it did."""
    c = db()
    row = c.execute('SELECT * FROM plans WHERE id=?', (plan_id,)).fetchone()
    c.close()
    if not row:
        return False, 'no_such_plan'
    if row['change_type'] != 'new_service':
        return False, f"not_a_service_plan:{row['change_type']}"
    if row['status'] != 'applied':
        return False, f"bundle_not_staged_yet:status={row['status']}"
    ok_file = APPROVALS_DIR / f'F{plan_id}.approved'
    if not (approved and ok_file.exists()):
        return False, f'needs approval: touch {ok_file} then re-run with --approved'
    params = json.loads(row['params'] or '{}')
    name = params.get('name', '')
    src = STAGED_DIR / name
    if not src.is_dir():
        return False, f'staged_bundle_missing:{src}'
    unit_src = src / f'{name}.service'
    timer_src = src / f'{name}.timer'
    installed = []
    placed = []
    # The bundle stages the script beside its unit; the unit expects it at the
    # ExecStart path. Put it there first, or the unit installs and instantly fails.
    dest = _execstart_script(unit_src.read_text())
    if not dest or not dest.startswith(FILE_ALLOW) or '..' in dest:
        return False, f'refusing_install: ExecStart path not allowed ({dest[:80]})'
    staged_script = src / Path(dest).name
    if not staged_script.is_file():
        return False, f'staged_script_missing:{staged_script}'
    if Path(dest).exists():
        return False, f'refusing_install: {dest} already exists (would clobber)'
    try:
        shutil.copy2(staged_script, dest)
        os.chmod(dest, 0o755)
        placed.append(dest)
    except Exception as e:
        return False, f'script_place_failed:{str(e)[:120]}'
    def undo(why):
        for u in installed:
            sh(f'sudo -n systemctl disable --now {shlex.quote(Path(u).name)}')
            sh(f'sudo -n rm -f {shlex.quote(u)}')
        sh('sudo -n systemctl daemon-reload')
        for f in placed:
            try:
                Path(f).unlink()
            except Exception:
                pass
        _ntfy(f'FACTORY install of {name} failed and was undone: {why}')
        return False, f'install_failed_undone: {why}'
    rc, out = sh(f'sudo -n cp {shlex.quote(str(unit_src))} /etc/systemd/system/')
    if rc != 0:
        return undo(f'cp_unit:{out[:150]}')
    installed.append(f'/etc/systemd/system/{name}.service')
    if timer_src.exists():
        rc, out = sh(f'sudo -n cp {shlex.quote(str(timer_src))} /etc/systemd/system/')
        if rc != 0:
            return undo(f'cp_timer:{out[:150]}')
        installed.append(f'/etc/systemd/system/{name}.timer')
    rc, out = sh('sudo -n systemctl daemon-reload')
    if rc != 0:
        return undo(f'daemon_reload:{out[:150]}')
    target = f'{name}.timer' if timer_src.exists() else f'{name}.service'
    rc, out = sh(f'sudo -n systemctl enable --now {shlex.quote(target)}', timeout=120)
    if rc != 0:
        return undo(f'enable:{out[:150]}')
    time.sleep(3)
    rc, state = sh(f'systemctl is-active {shlex.quote(target)}')
    healthy = state.strip() in ('active', 'activating', 'waiting')
    if not healthy and target.endswith('.service'):
        rc2, res = sh(f'systemctl show {shlex.quote(target)} -p Result --value')
        healthy = res.strip() == 'success'  # oneshot that already finished cleanly
    if not healthy:
        return undo(f'unhealthy_after_start:{state[:80]}')
    c = db()
    c.execute("UPDATE plans SET status='installed', notes=notes || ? WHERE id=?",
              (chr(10) + f'installed {target} at ' + now(), plan_id))
    c.commit(); c.close()
    episode_commit(f'factory INSTALLED staged service {name} ({target}) from plan {plan_id}')
    return True, f'{target} installed ({dest}) and {state.strip()}'


def stage_yellow(plan_id):
    """Write approval request + ntfy (reuses lumen_exec approval convention)."""
    c = db()
    row = c.execute('SELECT p.*, pr.title FROM plans p JOIN proposals pr ON pr.id=p.proposal_id WHERE p.id=?', (plan_id,)).fetchone()
    c.execute("UPDATE plans SET status='staged' WHERE id=?", (plan_id,))
    c.execute("UPDATE proposals SET status='staged' WHERE id=?", (row['proposal_id'],))
    c.commit(); c.close()
    APPROVALS_DIR.mkdir(parents=True, exist_ok=True)
    req = APPROVALS_DIR / f'F{plan_id}.md'
    req.write_text(
        f'# FACTORY YELLOW PLAN F{plan_id}\n'
        f'proposal: {row["proposal_id"]}  {row["title"]}\n'
        f'change_type: {row["change_type"]}\ntargets: {row["targets"]}\n\n'
        f'## draft diff\n{row["draft_diff"] or "(see params)"}\n\n'
        f'## params\n{row["params"]}\n\n'
        f'## apply/health/rollback\n{row["apply_cmd"]}\n{row["health_cmd"]}\n{row["rollback_cmd"]}\n\n'
        f'## to approve\ntouch {APPROVALS_DIR}/F{plan_id}.approved\n'
        f'then: python3 /home/lumen/minerva/minerva_factory.py apply {plan_id} --approved\n')
    return str(req)

def cmd_run(n, do_apply):
    migrate()
    c = db()
    props = c.execute("SELECT * FROM proposals WHERE status='deploy_ready' ORDER BY created_at DESC LIMIT ?", (n,)).fetchall()
    c.close()
    log(f'factory run: {len(props)} deploy_ready proposals, apply={do_apply}')
    tally = {'green': 0, 'yellow': 0, 'red': 0, 'error': 0, 'applied': 0, 'failed': 0}
    staged_msgs = []
    for prop in props:
        pid, lane = plan_one(prop)
        if pid is None:
            log(f'  PLAN-ERR {prop["id"]}: {lane}'); tally['error'] += 1; continue
        log(f'  plan {pid} [{lane}] <- {prop["id"]} {prop["title"][:55]}')
        tally[lane] += 1
        if lane == 'green' and do_apply:
            ok, detail = apply_plan(pid)
            tally['applied' if ok else 'failed'] += 1
            log(f'  {"APPLIED" if ok else "FAILED"}: {detail[:150]}')
        elif lane == 'yellow':
            staged_msgs.append(f'F{pid}: {prop["title"][:60]}')
            stage_yellow(pid)
    if staged_msgs:
        _ntfy('FACTORY staged ' + str(len(staged_msgs)) + ' YELLOW plans for approval:\n' + chr(10).join(staged_msgs[:10]))
    log(f'factory done: {tally}')
    return tally


def cmd_replan(n, do_apply=False, lanes=('red', 'yellow')):
    """Re-plan proposals whose ONLY problem was the starved planner.

    The 2026-08-14 audit left 41 'planned' + 16 'staged' proposals parked: RED for
    planner_flag:unclear / unplannable, or YELLOW new_service with no applier to
    finish them. Those verdicts were produced by a planner that could not see the
    filesystem, so they are re-run - not trusted - against the current planner."""
    migrate()
    c = db()
    rows = c.execute(
        "SELECT pr.* FROM proposals pr JOIN plans p ON p.proposal_id = pr.id "
        "WHERE pr.status IN ('planned','staged') AND p.status IN ('planned','staged') "
        "AND p.lane IN (%s) GROUP BY pr.id ORDER BY p.created_at DESC LIMIT ?"
        % ','.join('?' * len(lanes)), tuple(lanes) + (n,)).fetchall()
    c.close()
    log(f'replan: {len(rows)} parked proposals')
    tally = {'green': 0, 'yellow': 0, 'red': 0, 'error': 0, 'applied': 0, 'failed': 0}
    for prop in rows:
        pid, lane = plan_one(prop)
        if pid is None:
            log(f'  PLAN-ERR {prop["id"]}: {lane}'); tally['error'] += 1; continue
        log(f'  replan {pid} [{lane}] <- {prop["id"]} {prop["title"][:55]}')
        tally[lane] += 1
        if lane == 'green' and do_apply:
            ok, detail = apply_plan(pid)
            tally['applied' if ok else 'failed'] += 1
            log(f'  {"APPLIED" if ok else "FAILED"}: {detail[:150]}')
        elif lane == 'yellow':
            stage_yellow(pid)
    log(f'replan done: {tally}')
    return tally


def main():
    migrate()
    a = sys.argv[1:]
    if not a or a[0] in ('-h', '--help'):
        print(__doc__); return
    if a[0] == 'plan':
        n = int(a[1]) if len(a) > 1 else 5
        c = db()
        props = c.execute("SELECT * FROM proposals WHERE status='deploy_ready' ORDER BY created_at DESC LIMIT ?", (n,)).fetchall()
        c.close()
        for prop in props:
            pid, lane = plan_one(prop)
            print(f'{pid or "ERR"}  [{lane}]  {prop["id"]}  {prop["title"][:60]}')
    elif a[0] == 'list':
        c = db()
        q = 'SELECT p.id, p.lane, p.change_type, p.status, pr.title FROM plans p JOIN proposals pr ON pr.id=p.proposal_id'
        args = ()
        if len(a) > 1:
            q += ' WHERE p.lane=?'; args = (a[1],)
        q += ' ORDER BY p.created_at DESC LIMIT 40'
        for r in c.execute(q, args):
            print(f"{r['id']}  {r['lane']:<7} {r['status']:<16} {r['change_type']:<24} {r['title'][:55]}")
        c.close()
    elif a[0] == 'show':
        c = db()
        r = c.execute('SELECT * FROM plans WHERE id=?', (a[1],)).fetchone(); c.close()
        if not r: print('no such plan'); return
        for k in r.keys():
            v = r[k]
            print(f'--- {k} ---'); print(v if v is not None else '')
    elif a[0] == 'install':
        ok, detail = install_staged_service(a[1], approved='--approved' in a)
        print(('OK: ' if ok else 'NO: ') + detail)
        sys.exit(0 if ok else 1)
    elif a[0] == 'apply':
        ok, detail = apply_plan(a[1], approved='--approved' in a)
        print(('OK: ' if ok else 'NO: ') + detail)
        sys.exit(0 if ok else 1)
    elif a[0] == 'verify':
        # verify <proposal_id>  — re-run liveness check, update status
        if len(a) < 2:
            print('usage: minerva-factory verify <proposal_id>'); sys.exit(1)
        ok, detail = verify_proposal(a[1])
        print(('PASS: ' if ok else 'FAIL: ') + detail)
        sys.exit(0 if ok else 1)
    elif a[0] == 'verify-all':
        # re-verify every currently-deployed proposal
        c = db()
        rows = c.execute("SELECT id FROM proposals WHERE status='deployed' ORDER BY deployed_at").fetchall()
        c.close()
        passed = failed = skipped = 0
        for r in rows:
            ok, detail = verify_proposal(r['id'])
            label = 'PASS' if ok else 'FAIL'
            if 'raw status flip' in detail:
                label = 'SKIP (no plan)'
                skipped += 1
            elif ok:
                passed += 1
            else:
                failed += 1
            print(f'  {label}: {r["id"]} — {detail[:100]}')
        print(f'\nSummary: {passed} verified, {failed} demoted to applied_unverified, {skipped} planless (reclassified)')
        # Reclassify planless deployed proposals (raw DB flips with no plan = no way to verify)
        c = db()
        c.execute("""UPDATE proposals SET status='applied_unverified', deployed_at=NULL
                     WHERE status='deployed'
                       AND id NOT IN (SELECT proposal_id FROM plans WHERE proposal_id IS NOT NULL)""")
        reclassified = c.execute('SELECT changes()').fetchone()[0]
        c.commit(); c.close()
        if reclassified:
            print(f'  {reclassified} planless proposals reclassified to applied_unverified')
    elif a[0] == 'replan':
        n = int(a[1]) if len(a) > 1 and a[1].isdigit() else 5
        cmd_replan(n, do_apply='--apply' in a)
    elif a[0] == 'run':
        n = int(a[1]) if len(a) > 1 and a[1].isdigit() else 10
        cmd_run(n, do_apply='--apply' in a)
    else:
        print(__doc__)

if __name__ == '__main__':
    main()
