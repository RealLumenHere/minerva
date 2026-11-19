#!/usr/bin/env python3
"""ir_triage.py — read-only, proposal-only incident-response triage lane (wq#2836).

Three X-MASIR-derived roles (doi:10.34218/ijaiml_05_02_007) run as sequential
LiteLLM calls over the fleet's existing alert signal set:

  1. evidence    select the fragments that are evidence of an incident
  2. correlate   one incident hypothesis, citing fragment IDs
  3. plan        ranked remediation steps, each naming the artifact it touches

Signals are read, never written: the aq alert queue (Gatus probes, ECC, load,
spof dead-man and every other ntfy alert land there), the lumen-failed-units
state file and the latest weekly Trivy report. Every citation the model returns
is checked against the fragment set, so an explanation can only point at
evidence that exists.

The only output is one row in minerva.db `proposals` plus its markdown twin. The
row is written with status='blocked': the proposals UI shows it by default, and
nothing in the pipeline consumes 'blocked' (minerva_eval reads 'accept', the
factory reads 'deploy_ready', minerva_defer_review re-scores 'defer'), so a
triage plan can reach the fleet only through a human pressing Ship.

Spend is bounded by the seen-set: the model is called only when a fragment ID
appears that the previous completed run did not see.

Usage: ir_triage.py [--collect-only] [--dry] [--force]
"""
import argparse, glob, hashlib, json, os, re, sqlite3, subprocess, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

AQ_DB = Path('/home/lumen/agents/alert-queue/alerts.db')
FAILED_UNITS = Path('/home/lumen/agents/failed-units/state.json')
TRIVY_DIR = Path('/home/lumen/services/trivy-weekly/reports')
MINERVA_DB = Path('/home/lumen/minerva/minerva.db')
PROPOSALS_DIR = Path('/home/lumen/minerva/proposals/pending')
STATE = Path('/home/lumen/minerva/logs/ir_triage.state.json')
LOG = Path('/home/lumen/minerva/logs/ir_triage.log')
MASKING_DIR = '/home/lumen/foreman/harness'

LITELLM_URL = 'http://localhost:4000/v1/chat/completions'
MODEL = os.getenv('IR_TRIAGE_MODEL', 'deepseek-v4-flash')
# wq#3586: one unfunded provider must not fail the unit. 2026-10-07 deepseek
# answered 402 Insufficient Balance on every pass, so the unit flapped failed
# hourly (aq#2314 x12). A billing/auth refusal skips straight to the next model.
FALLBACK_MODELS = [m.strip() for m in os.getenv(
    'IR_TRIAGE_FALLBACK_MODELS', 'cerebras-oss-120b').split(',') if m.strip()]
MODELS = list(dict.fromkeys([MODEL] + FALLBACK_MODELS))
NO_RETRY_HTTP = (401, 402, 403)
# wq#3586: wall-clock cap on all LLM calls in one pass. With deepseek at 402 the
# fallback cerebras timed out at 180s x3 per role, so three roles could run ~28
# min, past the unit's TimeoutStartSec=15min, and systemd killed it to failed.
# Spent budget = model failure: aq row, exit 0, next fire retries.
LLM_BUDGET_S = float(os.getenv('IR_TRIAGE_LLM_BUDGET_S', '600'))
LLM_TIMEOUT_S = 180
STATUS = 'blocked'
DECISION_REASON = ('ir-triage lane (wq#2836): review-only incident plan, never '
                   'auto-applied. Ship to act on it, Reject to dismiss.')

AQ_LIMIT = 40
# wq#2907 guard class: a held minerva.db lock must not hard-fail the unit
# (2026-09-28: the wq#2895 leaked lock made this lane exit 1 for 35 min —
# same restart/alert-spam class as wq#2570/2571). Retry with backoff, then
# ONE deduped aq row and exit 0; the seen-set is not advanced, so the next
# timer fire retries the whole pass.
AQ_BIN = '/home/lumen/bin/aq'
DB_TIMEOUT_S = 30
LOCK_BACKOFF_S = (5, 15, 45)
# The first live pass (proposal deddbcfcba55) returned 17 steps and overran the
# 2000-char proposed_change column mid-step.
MAX_STEPS = 8
# ntfy priorities arrive both as words and as 1-5.
RANK = {'urgent': 5, 'max': 5, '5': 5, 'high': 4, '4': 4}
# Always evidence regardless of priority: the credential tripwire.
SECURITY_SOURCES = ('guard-fired',)

# wq#3523: permanent decision exclusions -- these alert keys generate proposals
# for settled decisions. Skip them at fragment collection so they never reach the LLM.
EXCLUSIONS = [
    (re.compile(r'anthropic', re.I),
     'ANTHROPIC_API_KEY permanently defused'),
    (re.compile(r'github.lumen', re.I),
     'github-lumen token is valid'),
    (re.compile(r'credential.*cascade', re.I),
     'credential cascade settled'),
    (re.compile(r'probe-credential-expiry'),
     'sentinel-managed, not a triage target'),
    (re.compile(r'\[selfheal\]\s*rotate.*credential', re.I),
     'handled by selfheal, not triage'),
]


def _excluded(key, title):
    """Return True if the alert matches a permanent-decision exclusion."""
    text = f"{key} {title}"
    return any(p.search(text) for p, _reason in EXCLUSIONS)


def log(m):
    line = f'[{datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}] {m}'
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, 'a') as f:
            f.write(line + '\n')
    except OSError:
        pass


def _load_mask():
    try:
        sys.path.insert(0, MASKING_DIR)
        from masking import mask_text
        return lambda t: mask_text(t or '')[0]
    except Exception as e:
        log(f'masking unavailable ({e}); fragment text dropped')
        return lambda t: '[text withheld: masking unavailable]'


mask = _load_mask()


def frag(fid, source, ts, text):
    return {'id': fid, 'source': source, 'ts': ts, 'text': mask(text)[:400]}


def collect_aq(db=None):
    db = db or AQ_DB
    if not Path(db).exists():
        return []
    # mode=ro: aq.db() would run its CREATE TABLE script, which is a write.
    c = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute("SELECT * FROM alerts WHERE status='open' ORDER BY last_ts DESC").fetchall()
    finally:
        c.close()
    out = []
    for r in rows:
        if RANK.get(str(r['priority']).lower(), 0) < 4 and r['source'] not in SECURITY_SOURCES:
            continue
        if _excluded(r['key'], r['title'] or ''):
            continue
        # mask() first, THEN the display budget (wq#2846/wq#2872): cutting the raw message
        # at 200 can land inside a token, leaving a prefix plus a body run under masking.py's
        # rule floor, which no later mask() call can recognize any more.
        text = (f"{r['title'] or r['key']} (x{r['count']}, first {r['first_ts']}, "
                f"priority {r['priority']}) {mask(r['message'])[:200]}")
        out.append(frag(f"aq#{r['id']}", f"aq:{r['source']}", r['last_ts'], text))
        if len(out) >= AQ_LIMIT:
            break
    return out


def collect_failed_units(path=None):
    path = Path(path or FAILED_UNITS)
    try:
        st = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    ts = datetime.fromtimestamp(st.get('checked_at', 0), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return [frag(f'unit:{u}', 'lumen-failed-units', ts, f'systemd unit {u} is in failed state')
            for u in st.get('failed', [])]


def collect_trivy(report_dir=None):
    reports = sorted(glob.glob(str(Path(report_dir or TRIVY_DIR) / 'trivy_*.json')))
    if not reports:
        return []
    path = Path(reports[-1])
    stamp = path.stem.removeprefix('trivy_')
    ts = datetime.strptime(stamp, '%Y%m%d_%H%M%S').replace(tzinfo=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    raw = path.read_text(errors='replace')
    try:
        doc = json.loads(raw)
    except ValueError:
        return [frag(f'trivy:{stamp}', 'lumen-trivy-weekly', ts,
                     'weekly host rootfs CVE scan produced no parseable report, so host '
                     f'CVE coverage is blind this week: {mask(raw.strip())[:250]}')]
    vulns = [v for res in doc.get('Results') or [] for v in res.get('Vulnerabilities') or []]
    sev = {s: [v for v in vulns if v.get('Severity') == s] for s in ('CRITICAL', 'HIGH')}
    if not (sev['CRITICAL'] or sev['HIGH']):
        return []
    top = ', '.join(f"{v.get('VulnerabilityID')} ({v.get('PkgName')} {v.get('InstalledVersion')}"
                    f"->{v.get('FixedVersion') or 'no fix'})" for v in (sev['CRITICAL'] + sev['HIGH'])[:5])
    return [frag(f'trivy:{stamp}', 'lumen-trivy-weekly', ts,
                 f"{len(sev['CRITICAL'])} CRITICAL, {len(sev['HIGH'])} HIGH host CVEs; top: {top}")]


def collect():
    return collect_aq() + collect_failed_units() + collect_trivy()


_refused = set()  # models that answered 401/402/403 this pass; not asked again
_answered = {}  # last model that returned content, recorded in eval_result
_deadline = {}  # monotonic end of this pass's LLM budget, set on the first chat()


def _budget_left():
    return _deadline['t'] - time.monotonic()


def _chat_one(model, prompt, key):
    body = {'model': model, 'messages': [{'role': 'user', 'content': prompt}],
            'temperature': 0.2, 'max_tokens': 4000}
    if 'deepseek' in model:
        # Same reasoning-sprawl guard minerva_proposal uses for this model.
        body['response_format'] = {'type': 'json_object'}
    req = urllib.request.Request(LITELLM_URL, data=json.dumps(body).encode(), headers={
        'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
    for attempt in range(3):
        left = _budget_left()
        if left <= 0:
            log(f'  llm budget {LLM_BUDGET_S:.0f}s spent; not asking {model}')
            return ''
        try:
            with urllib.request.urlopen(req, timeout=min(LLM_TIMEOUT_S, left)) as r:
                return json.load(r)['choices'][0]['message']['content'] or ''
        except Exception as e:
            log(f'  llm err {model} (attempt {attempt + 1}/3): {str(e)[:160]}')
            if getattr(e, 'code', None) in NO_RETRY_HTTP:
                _refused.add(model)
                return ''
            time.sleep(3 * (attempt + 1))
    return ''


def chat(prompt):
    key = os.getenv('LITELLM_MASTER_KEY', '')
    if not key:
        log('LITELLM_MASTER_KEY unset')
        return ''
    _deadline.setdefault('t', time.monotonic() + LLM_BUDGET_S)
    for model in MODELS:
        if model in _refused:
            continue
        out = _chat_one(model, prompt, key)
        if out:
            _answered['model'] = model
            return out
    return ''


def parse_json(out):
    m = re.search(r'\{[\s\S]*\}', out or '')
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def render(frags):
    return '\n'.join(f"[{f['id']}] source={f['source']} ts={f['ts']} :: {f['text']}" for f in frags)


EVIDENCE_PROMPT = """You are the EVIDENCE-COLLECTION agent of an incident-response team for a self-hosted homelab fleet (host "lumen" plus tailnet nodes).
Below are raw signal fragments, one per line, each with an ID, source and timestamp.
Select the fragments that are evidence of a security or operational incident a human should act on now. Drop noise, duplicates and routine chatter.
Return JSON only: {"evidence": [{"id": "<fragment id exactly as given>", "why": "<one line>"}]}
Use only IDs that appear below. Return {"evidence": []} if nothing warrants action.

FRAGMENTS:
__FRAGMENTS__"""

CORRELATE_PROMPT = """You are the CORRELATION agent of an incident-response team for a self-hosted homelab fleet.
Map the evidence fragments below to the single most important incident hypothesis. Every claim must be backed by the fragment IDs you cite.
Return JSON only: {"hypothesis": "<2-4 sentences>", "cites": ["<fragment id>", ...], "severity": "low|medium|high|critical", "confidence": "low|medium|high", "alternatives": ["<competing explanation>", ...]}
Cite only IDs that appear below.

EVIDENCE:
__FRAGMENTS__"""

PLAN_PROMPT = """You are the RESPONSE-PLANNING agent of an incident-response team for a self-hosted homelab fleet.
Given the incident hypothesis and its evidence, write a ranked remediation plan of at most __MAX_STEPS__ steps, most urgent step first. Start with read-only diagnosis where the cause is uncertain.
Every step must name the exact fleet artifact it touches (a systemd unit, file path, host, container or service) and cite the fragment IDs that justify it. Never propose a destructive command without a backup step before it.
Return JSON only: {"title": "<under 90 chars>", "steps": [{"action": "<imperative>", "artifact": "<artifact>", "cites": ["<fragment id>", ...]}], "rollback": "<how to undo the plan>"}

HYPOTHESIS: __HYPOTHESIS__

EVIDENCE:
__FRAGMENTS__"""


def triage(frags, llm=chat):
    """Run the three roles. Returns (result, reason): result is None when there is
    no incident, and reason == 'llm' when the model failed and the run must retry."""
    known = {f['id']: f for f in frags}

    ev = parse_json(llm(EVIDENCE_PROMPT.replace('__FRAGMENTS__', render(frags))))
    if ev is None:
        return None, 'llm'
    evidence = [e for e in ev.get('evidence') or [] if isinstance(e, dict) and e.get('id') in known]
    if not evidence:
        return None, 'no evidence selected'
    ev_frags = [known[e['id']] for e in evidence]

    co = parse_json(llm(CORRELATE_PROMPT.replace('__FRAGMENTS__', render(ev_frags))))
    if co is None:
        return None, 'llm'
    cites = [c for c in co.get('cites') or [] if c in known]
    if not cites or not str(co.get('hypothesis', '')).strip():
        return None, 'hypothesis cites no known fragment'

    pl = parse_json(llm(PLAN_PROMPT.replace('__MAX_STEPS__', str(MAX_STEPS))
                        .replace('__HYPOTHESIS__', str(co['hypothesis']))
                        .replace('__FRAGMENTS__', render(ev_frags))))
    if pl is None:
        return None, 'llm'
    steps = []
    for s in pl.get('steps') or []:
        if not isinstance(s, dict):
            continue
        s_cites = [c for c in s.get('cites') or [] if c in known]
        if str(s.get('action', '')).strip() and str(s.get('artifact', '')).strip() and s_cites:
            steps.append({'action': s['action'], 'artifact': s['artifact'], 'cites': s_cites})
    if not steps:
        return None, 'no plan step names an artifact and cites evidence'
    steps = steps[:MAX_STEPS]

    return {'evidence': evidence, 'hypothesis': co['hypothesis'], 'cites': cites,
            'severity': co.get('severity', 'medium'), 'confidence': co.get('confidence', 'low'),
            'alternatives': co.get('alternatives') or [], 'title': pl.get('title') or 'incident',
            'steps': steps, 'rollback': pl.get('rollback') or '', 'fragments': known}, 'ok'


def explanation(result, cap=4000):
    """eval_result JSON: the cited fragments are the explanation. Shrinks fragment
    text until it fits the column cap, so the stored JSON is never truncated mid-token."""
    cited = sorted({c for c in result['cites']} | {c for s in result['steps'] for c in s['cites']})
    for width in (240, 120, 60, 0):
        doc = {'lane': 'ir-triage', 'ticket': 'wq#2836', 'model': _answered.get('model', MODEL),
               'severity': result['severity'], 'confidence': result['confidence'],
               'explanation': {'hypothesis_cites': result['cites'], 'fragments': {
                   c: {k: result['fragments'][c][k] for k in ('source', 'ts')}
                   | ({'text': result['fragments'][c]['text'][:width]} if width else {})
                   for c in cited}},
               'alternatives': [str(a)[:200] for a in result['alternatives']][:3]}
        s = json.dumps(doc)
        if len(s) <= cap:
            return s
    return json.dumps({'lane': 'ir-triage', 'explanation': {'hypothesis_cites': result['cites'][:20]}})


def to_proposal(result):
    sev = str(result['severity']).lower()
    steps = [f"{s['action']} -- artifact: {s['artifact']} [cites: {', '.join(s['cites'])}]"
             for s in result['steps']]
    # Keep whole steps only: the proposed_change column holds 2000 chars.
    while len(steps) > 1 and len('\n'.join(f'{i + 1}. {s}' for i, s in enumerate(steps))) > 2000:
        steps.pop()
    return {
        'title': f"IR triage: {result['title']}"[:200],
        'current_state': (f"{result['hypothesis']} [cites: {', '.join(result['cites'])}; "
                          f"severity {sev}, confidence {result['confidence']}]"),
        'proposed_change': '\n'.join(f'{i + 1}. {s}' for i, s in enumerate(steps)),
        'integration_steps': steps,
        'effort': 'S',
        'impact': {'critical': 'H', 'high': 'H', 'medium': 'M'}.get(sev, 'S'),
        'risk': 'M',
        'rollback': result['rollback'],
    }


def _is_locked(e):
    return isinstance(e, sqlite3.OperationalError) and (
        'locked' in str(e) or 'busy' in str(e))


def _aq_add(key, title, message):
    """One deduped aq row (source=ir-triage) instead of a failed-unit page.
    Best-effort: a missing aq binary only logs."""
    try:
        subprocess.run([AQ_BIN, 'add', 'ir-triage', key, title, message],
                       check=False, timeout=30)
    except OSError as e:
        log(f'aq unavailable ({e}); {key} alert dropped')


def alert_db_locked(message):
    _aq_add('minerva-db-locked', 'ir-triage: minerva.db locked, pass skipped', message)


def alert_llm_down(message):
    _aq_add('llm-unavailable', 'ir-triage: every model failed, pass skipped', message)


def save(pid, source_id, prop, result, db=None, out_dir=None):
    """Insert the proposal row + markdown, retrying a held write lock with
    backoff (wq#2907) so a short contention window never fails the pass.
    Returns False if pid already exists."""
    for delay in LOCK_BACKOFF_S:
        try:
            return _save_once(pid, source_id, prop, result, db, out_dir)
        except sqlite3.OperationalError as e:
            if not _is_locked(e):
                raise
            log(f'minerva.db locked on save; retrying in {delay}s')
            time.sleep(delay)
    return _save_once(pid, source_id, prop, result, db, out_dir)


def _save_once(pid, source_id, prop, result, db=None, out_dir=None):
    now = datetime.now(timezone.utc).isoformat()
    c = sqlite3.connect(db or MINERVA_DB, timeout=DB_TIMEOUT_S)
    try:
        c.execute('PRAGMA busy_timeout=%d' % (DB_TIMEOUT_S * 1000))
        cur = c.execute(
            '''INSERT OR IGNORE INTO proposals
               (id, parent_id, title, source_intel_id, current_state, proposed_change,
                effort, impact, risk, rollback, status, eval_result, created_at, decision_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (pid, None, prop['title'], source_id, prop['current_state'][:1000],
             prop['proposed_change'][:2000], prop['effort'], prop['impact'], prop['risk'],
             prop['rollback'][:500], STATUS, explanation(result), now, DECISION_REASON))
        c.commit()
        if not cur.rowcount:
            return False
    finally:
        c.close()

    cited = sorted({c for c in result['cites']} | {c for s in result['steps'] for c in s['cites']})
    frags = result['fragments']
    md = '\n'.join([
        '---', f'id: {pid}', f"title: {prop['title']}", f'source_intel_id: {source_id}',
        f"effort: {prop['effort']}", f"impact: {prop['impact']}", f"risk: {prop['risk']}",
        f'decision: {STATUS}', f'created_at: {now}', '---', '',
        f"# {prop['title']}", '', f'**Decision:** {STATUS} - {DECISION_REASON}', '',
        '## Current state', prop['current_state'], '',
        '## Proposed change', prop['proposed_change'], '',
        '## Effort / Impact / Risk', f"- Effort: {prop['effort']}", f"- Impact: {prop['impact']}",
        f"- Risk: {prop['risk']}", '',
        '## Rollback', prop['rollback'], '',
        '## Explanation (cited fragments)',
        *[f"- `{c}` {frags[c]['source']} @ {frags[c]['ts']}: {frags[c]['text']}" for c in cited], '',
        '## Alternatives considered', *[f'- {a}' for a in result['alternatives']], '',
    ])
    out = Path(out_dir or PROPOSALS_DIR)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{datetime.now(timezone.utc).strftime('%Y%m%d')}_{pid}.md").write_text(md)
    return True


def load_seen():
    try:
        return set(json.loads(STATE.read_text()).get('seen', []))
    except (OSError, ValueError):
        return set()


def store_seen(ids, note):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps({'seen': sorted(ids), 'at': datetime.now(timezone.utc).isoformat(),
                               'last': note}, indent=1))
    tmp.replace(STATE)


def _proposal_title_exists(title):
    """wq#3523: dedup check -- return True if a proposal with this title exists
    in pending/ or rejected/ (case-insensitive first-line match)."""
    t_lower = title.strip().lower()
    for d in (PROPOSALS_DIR, PROPOSALS_DIR.parent / 'rejected'):
        if not d.is_dir():
            continue
        for p in d.glob('*.md'):
            try:
                with open(p) as f:
                    for line in f:
                        if line.startswith('title:'):
                            if line.split(':', 1)[1].strip().lower() == t_lower:
                                return True
                            break
            except OSError:
                continue
    return False


def run(dry=False, force=False, llm=chat):
    frags = collect()
    ids = {f['id'] for f in frags}
    new = ids - load_seen()
    log(f'fragments={len(frags)} new={len(new)}')
    if not frags:
        store_seen(ids, 'no fragments')
        return 'quiet'
    if not new and not force:
        return 'unchanged'

    result, reason = triage(frags, llm)
    if reason == 'llm':
        log('model gave no usable JSON; seen-set not advanced, next run retries')
        return 'llm_error'
    if result is None:
        log(f'no incident: {reason}')
        if not dry:
            store_seen(ids, reason)
        return 'no_incident'

    fp = hashlib.sha256(','.join(sorted(ids)).encode()).hexdigest()
    pid = hashlib.md5(f'ir-triage:{fp}'.encode()).hexdigest()[:12]
    prop = to_proposal(result)
    if dry:
        print(json.dumps({'id': pid, **prop, 'eval_result': json.loads(explanation(result))}, indent=1))
        return 'dry'
    # wq#3523: dedup -- skip if a proposal with the same title already exists
    if _proposal_title_exists(prop['title']):
        log(f'dedup: proposal with title {prop["title"]!r} already exists, skipping')
        store_seen(ids, f'dedup {prop["title"][:60]}')
        return 'dedup'
    wrote = save(pid, f'ir-triage:{fp[:16]}', prop, result)
    store_seen(ids, f'proposal {pid}')
    log(f"{'wrote' if wrote else 'exists'} proposal {pid}: {prop['title']} "
        f"({len(result['steps'])} steps, cites {len(result['cites'])})")
    return 'proposed' if wrote else 'exists'


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--collect-only', action='store_true', help='print masked fragments, no LLM, no writes')
    ap.add_argument('--dry', action='store_true', help='run the three roles, print the proposal, write nothing')
    ap.add_argument('--force', action='store_true', help='run even if no fragment is new')
    a = ap.parse_args(argv)
    if a.collect_only:
        print(json.dumps(collect(), indent=1))
        return 0
    try:
        outcome = run(dry=a.dry, force=a.force)
    except sqlite3.OperationalError as e:
        if not _is_locked(e):
            raise
        # wq#2907: lock still held after save()'s backoff (or a collect read hit
        # it). Exit 0 — the aq row is the alert, the unit must not go failed,
        # and the unadvanced seen-set makes the next timer fire retry.
        log(f'database locked after retries; skipping this pass: {e}')
        alert_db_locked(f'held lock outlasted retries ({e}); '
                        'seen-set not advanced, next timer fire retries')
        return 0
    if outcome == 'llm_error':
        if a.dry:
            return 1
        # wq#3586: same guard class. A dead provider is an aq row, not an hourly
        # failed-unit page; the unadvanced seen-set retries on the next fire.
        alert_llm_down(f'models {",".join(MODELS)} gave no usable JSON '
                       f'(refused: {",".join(sorted(_refused)) or "none"}); next timer fire retries')
    return 0


if __name__ == '__main__':
    sys.exit(main())
