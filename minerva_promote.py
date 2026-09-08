#!/usr/bin/env python3
"""minerva_promote — turn a factory-parked proposal into a wq ticket (wq#1500).

WHY: minerva_factory consumes every deploy_ready proposal: GREEN auto-applies,
YELLOW -> 'staged' (waits on an .approved file nobody writes), RED -> 'planned'.
No stage created a ticket, so grounded proposals (the wq#1383 CVE set) parked
with no owner. This module is that missing consumer.

Idempotency (three layers, any one is enough):
  1. proposals.wq_ticket (additive column) already set -> skip.
  2. a wq task whose detail carries `minerva_proposal=<id>` -> relink, skip.
  3. wq add's own normalized-title duplicate refusal.
Priority: CVE/GHSA/advisory anywhere in the grounding -> pri 1; else factory
YELLOW -> 2, RED/unknown -> 3. Lane: fleet (env MINERVA_PROMOTE_LANE).
Proposal status is never changed here; the ticket id is the link.
Kill switch: MINERVA_PROMOTE=0.

Reach (wq#1535): a fleet worker is linted against what the ticket body cites, and
/home/lumen/minerva is a deliberately excluded read root (wq#1467). So the body never
cites the factory's own paths: proposal.md + plan.md are PUBLISHED to
/home/lumen/proposals/<pid>/ (an allow.read_dirs root), masked through the harness
secret sweep at write time, and the close path is a factory verb, not a db path.

CLI:  minerva_promote.py run [N]     backfill parked proposals (deploy_ready|planned|staged)
      minerva_promote.py one <pid>   promote a single proposal
      minerva_promote.py status      counts of promoted vs parked
      minerva_promote.py reemit <tid>...   rewrite live promoter tickets' bodies in place (wq#1535)
      minerva_promote.py preview <pid>
"""
import json, os, re, sqlite3, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

DB = '/home/lumen/minerva/minerva.db'
WQ_DB = os.environ.get('WQ_DB', '/home/lumen/agents/work-queue/queue.db')
WQ = '/home/lumen/bin/wq'
PROPOSALS_DIR = Path('/home/lumen/minerva/proposals/pending')
PUBLISH_ROOT = Path(os.environ.get('MINERVA_PUBLISH_ROOT', '/home/lumen/proposals'))   # wq#1535, allow.read_dirs
HARNESS_DIR = '/home/lumen/foreman/harness'
FACTORY_BIN = 'minerva-factory'   # bare: /home/lumen/bin is not a granted read root
LANE = os.environ.get('MINERVA_PROMOTE_LANE', 'fleet')
ELIGIBLE = ('deploy_ready', 'planned', 'staged')
SEC_RE = re.compile(r'CVE-\d{4}-\d+|GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}|\badvisor(y|ies)\b|\bsecurity (patch|fix|update|vulnerab)', re.I)
MARKER = 'minerva_proposal='
FLEET_STACK = '/home/lumen/agent/fleet_stack.json'
UPGRADE_RE = re.compile(r'\b(upgrade|update|patch|bump|pin)\b', re.I)
_GENERIC = {'server', 'core', 'analytics', 'actual'}
_ALIASES = {'home-assistant/core': ['home assistant', 'home-assistant', 'homeassistant'],
            'nextcloud/server': ['nextcloud'], 'plausible/analytics': ['plausible'],
            'actualbudget/actual': ['actual budget', 'actualbudget'],
            'wonderwhy-er/DesktopCommanderMCP': ['desktopcommander', 'desktop commander', 'desktop-commander'],
            'ggml-org/llama.cpp': ['llama.cpp', 'llama-server', 'llama-cpp'],
            'VictoriaMetrics/VictoriaLogs': ['victorialogs', 'victoria logs']}


def product_key(text):
    """Map free text to the fleet_stack repo it is about, or None. Longest alias wins."""
    try:
        repos = json.load(open(FLEET_STACK)).get('repos', {})
    except Exception:
        return None
    low = ' ' + str(text or '').lower() + ' '
    best, best_pos = None, (10**9, 0)
    for repo in repos:
        owner, base = repo.split('/', 1)
        names = [a.lower() for a in _ALIASES.get(repo, [])]
        if base.lower() not in _GENERIC:
            names.append(base.lower())
        for nm in names:
            m = re.search(r'(?<![a-z0-9])' + re.escape(nm) + r'(?![a-z0-9])', low)
            if m and (m.start(), -len(nm)) < best_pos:   # earliest mention wins ("Upgrade uvicorn ... FastAPI"); longer breaks ties
                best, best_pos = repo, (m.start(), -len(nm))
    return best


def log(m):
    print(f'[{datetime.now(timezone.utc).strftime("%H:%M:%S UTC")}] promote: {m}', flush=True)


def db():
    c = sqlite3.connect(DB, timeout=30); c.row_factory = sqlite3.Row
    return c


def migrate():
    c = db()
    cols = {r[1] for r in c.execute('PRAGMA table_info(proposals)')}
    if 'wq_ticket' not in cols:
        c.execute('ALTER TABLE proposals ADD COLUMN wq_ticket INTEGER')
        c.commit(); log('migrated: proposals.wq_ticket added')
    c.close()


def _md_path(pid):
    hits = sorted(PROPOSALS_DIR.glob(f'*_{pid}.md'))
    return str(hits[-1]) if hits else None


def _plan(c, pid):
    return c.execute('SELECT * FROM plans WHERE proposal_id=? ORDER BY created_at DESC LIMIT 1', (pid,)).fetchone()


def _intel(c, intel_id):
    if not intel_id:
        return None
    return c.execute('SELECT id, source, title, url, category FROM classified_intel WHERE id=?', (intel_id,)).fetchone()


def _find_existing_ticket(pid):
    """Layer 2: recover the link from the ticket body if the column was lost."""
    try:
        w = sqlite3.connect(WQ_DB, timeout=30)
        row = w.execute("SELECT id FROM tasks WHERE detail LIKE ? ORDER BY id LIMIT 1", (f'%{MARKER}{pid}%',)).fetchone()
        w.close()
        return row[0] if row else None
    except Exception as e:
        log(f'wq db read failed ({e}) — treating as no existing ticket'); return None


def _is_green_applied(c, pid, plan):
    if plan is None:
        return False
    if plan['lane'] == 'green' and plan['status'] in ('applied', 'applied_unverified', 'deployed'):
        return True
    return c.execute('SELECT 1 FROM changelog WHERE proposal_id=? AND reverted=0 LIMIT 1', (pid,)).fetchone() is not None


def _priority(prop, intel, plan):
    blob = ' '.join(str(x or '') for x in (prop['title'], prop['proposed_change'], prop['current_state'],
                                            intel['title'] if intel else '', intel['source'] if intel else ''))
    if SEC_RE.search(blob):
        return 1, 'security'
    lane = plan['lane'] if plan else None
    return (2, 'factory-yellow') if lane == 'yellow' else (3, f'factory-{lane or "unplanned"}')


def _eval_scores(prop, c=None):
    """Verify scores. minerva_eval overwrites proposals.eval_result with its safety
    checks, so the guard's verdict is read back from verification_log (subject =
    'proposal:<intel title[:40]>' is not recoverable from here, but the proposal md
    keeps it and the log row is matched on the proposal's own current_state)."""
    ev = {}
    try:
        ev = json.loads(prop['eval_result'] or '{}')
    except Exception:
        pass
    if 'judge_score' in ev:
        return ev
    try:
        cc = c or db()
        cs = str(prop['current_state'] or '')[:200]
        row = cc.execute('SELECT passed, citation_score, judge_score, final_score, judge_rationale FROM verification_log '
                         'WHERE subject LIKE ? AND substr(input_text,1,200)=? ORDER BY ts DESC LIMIT 1',
                         ('proposal:%', cs)).fetchone()
        if c is None:
            cc.close()
        if row:
            return {'passed': bool(row[0]), 'citation_score': row[1], 'judge_score': row[2],
                    'final_score': row[3], 'judge_rationale': row[4], 'safety_checks': ev.get('checks')}
    except Exception:
        pass
    return ev


def _sweep_module():
    """harness/read_dir_sweep.py — the same PATTERNS/BASENAME_GLOBS the lint's daily sweep
    uses. None when unavailable: publish() then publishes NOTHING (fail closed — a granted
    root must never receive unswept text)."""
    try:
        sys.path.insert(0, HARNESS_DIR)
        import read_dir_sweep
        return read_dir_sweep
    except Exception as e:
        log(f'sweep module unavailable ({type(e).__name__}: {str(e)[:80]}) — not publishing')
        return None


def mask_text(text, sweep):
    """(masked_text, n_masked): every sweep PATTERNS match replaced by its masked form
    (first 4 chars + ellipsis). Masked tokens are shorter than any pattern's minimum,
    so the daily sweep can never re-fire on what we wrote."""
    n = 0
    out = text or ''
    for _name, rx in sweep.PATTERNS:
        def _sub(m):
            nonlocal n
            n += 1
            return sweep.mask(m.group(0))
        out = rx.sub(_sub, out)
    return out, n


def _approval_cmds(plan_id):
    try:
        sys.path.insert(0, '/home/lumen/minerva')
        import minerva_factory
        return minerva_factory.approval_cmds(plan_id)
    except Exception:
        return (f'touch /home/lumen/instructions/approvals/F{plan_id}.approved',
                f'{FACTORY_BIN} apply {plan_id} --approved')


def publish(prop, plan):
    """Write /home/lumen/proposals/<pid>/proposal.md (+ plan.md when a plan exists), each
    masked. Returns {'proposal': path|None, 'plan': path|None, 'masked': n}. Idempotent:
    re-running overwrites. Nothing is written when the sweep module is missing."""
    sweep = _sweep_module()
    out = {'proposal': None, 'plan': None, 'masked': 0}
    if sweep is None:
        return out
    pid = prop['id']
    d = PUBLISH_ROOT / pid
    d.mkdir(parents=True, exist_ok=True)
    hdr = f'<!-- published by minerva_promote (wq#1535) {datetime.now(timezone.utc).isoformat(timespec="seconds")} ; source root is not worker-readable -->\n'
    src = _md_path(pid)
    body = Path(src).read_text(errors='replace') if src else (
        f'# {prop["title"]}\n\n(no proposal md on disk)\n\n## Current state\n{prop["current_state"]}\n\n'
        f'## Proposed change\n{prop["proposed_change"]}\n\nrollback: {prop["rollback"]}\n')
    body, n1 = mask_text(body, sweep)
    (d / 'proposal.md').write_text(hdr + (f'<!-- {n1} secret-shaped token(s) masked -->\n' if n1 else '') + body)
    out['proposal'] = str(d / 'proposal.md'); out['masked'] += n1
    if plan is not None:
        keys = plan.keys()
        lines = [f'# factory plan {plan["id"]} for proposal {pid}', '']
        for k in ('lane', 'change_type', 'status', 'created_at', 'applied_at', 'targets', 'gate_result', 'notes'):
            if k in keys:
                lines.append(f'{k}: {plan[k]}')
        for k in ('params', 'apply_cmd', 'health_cmd', 'rollback_cmd', 'draft_diff'):
            if k in keys:
                lines += ['', f'## {k}', '```', str(plan[k] or '-'), '```']
        if plan['lane'] == 'yellow':
            lines += ['', '## to approve + apply', *_approval_cmds(plan['id'])]
        txt, n2 = mask_text('\n'.join(lines) + '\n', sweep)
        (d / 'plan.md').write_text(hdr + (f'<!-- {n2} secret-shaped token(s) masked -->\n' if n2 else '') + txt)
        out['plan'] = str(d / 'plan.md'); out['masked'] += n2
    if out['masked']:
        log(f'  publish {pid}: masked {out["masked"]} secret-shaped token(s)')
    return out


def _clip(s, n=400):
    s = str(s or '').strip()
    return '-' if not s else (s if len(s) <= n else s[:n] + f' ...[{len(s)} chars, see full plan]')


def _detail(prop, intel, plan, why, key=None):
    """Ticket body. INVARIANT (wq#1535): every /home/lumen path here is under an
    allow.read_dirs root or is a closure artifact the lint knows (approvals/F*.approved).
    Never cite /home/lumen/minerva, minerva.db, or the factory's apply_cmd/targets —
    those are the factory's reach, not the reviewer's; they live in plan.md."""
    ev = _eval_scores(prop)
    gate = {}
    if plan is not None:
        try:
            gate = json.loads(plan['gate_result'] or '{}')
        except Exception:
            gate = {}
    pub = publish(prop, plan)
    lines = [
        f'{MARKER}{prop["id"]}  minerva_product={key or "-"}  status={prop["status"]}  created={str(prop["created_at"])[:16]}  priority_reason={why}',
        f'proposal md: {pub["proposal"] or "(not published: sweep module unavailable — run minerva_promote.py reemit once it is)"}',
        '',
        '## Source grounding',
    ]
    if intel:
        lines += [f'intel: [{intel["source"]}] {intel["title"]}', f'url: {intel["url"] or "n/a"}', f'category: {intel["category"] or "-"}']
    else:
        lines += [f'intel: {prop["source_intel_id"] or "-"} (row not found)']
    lines += [
        '',
        '## Current state (verified by the hallucination guard)',
        str(prop['current_state'] or '').strip(),
        '',
        '## Proposed change',
        str(prop['proposed_change'] or '').strip(),
        '',
        f'effort={prop["effort"]} impact={prop["impact"]} risk={prop["risk"]}',
        f'rollback: {str(prop["rollback"] or "").strip()}',
        '',
        '## Verification',
        f'passed={ev.get("passed")} citation={ev.get("citation_score")} judge={ev.get("judge_score")} final={ev.get("final_score")}',
        f'judge rationale: {str(ev.get("judge_rationale") or "")[:300]}',
    ]
    close_tail = [f'then `{FACTORY_BIN} close {prop["id"]} --applied` (health-checked; deployed only on PASS) '
                  f'or `{FACTORY_BIN} close {prop["id"]} --reject "why"`, and `wq done`.']
    if plan is not None:
        lines += [
            '',
            '## Factory plan',
            f'plan={plan["id"]} lane={plan["lane"]} change_type={plan["change_type"]} plan_status={plan["status"]}',
            f'lane_reasons: {", ".join(gate.get("lane_reasons", []) or [])}',
            f'full plan (targets, draft diff, apply/health/rollback): {pub["plan"] or "(not published)"}',
            f'planner notes: {str(plan["notes"] or "")[:300]}',
            '',
            '## How to close',
        ]
        if plan['lane'] == 'yellow':
            a1, a2 = _approval_cmds(plan['id'])
            lines += [f'YELLOW: review plan.md, then `{a1}` and `{a2}`. If apply reports no_applier_for, the factory has no '
                      f'deterministic applier for this change_type: apply the draft diff from plan.md by hand under '
                      f'lumen-change-safety (verify -> precheck -> backup -> apply -> health);']
        else:
            lines += ['RED: factory will never auto-apply this. Do the change by hand under lumen-change-safety '
                      '(verify -> precheck -> backup -> apply -> health);']
        lines += close_tail
    else:
        lines += ['', '## How to close', 'No factory plan yet (deploy_ready). Either wait for minerva-factory to plan it '
                  'or do the change by hand under lumen-change-safety;', *close_tail]
    return '\n'.join(lines)


def _wq_add(title, detail, pri):
    env = dict(os.environ)
    env.setdefault('WQ_AGENT', 'minerva-promoter')
    r = subprocess.run([WQ, 'add', '--lane', LANE, '--pri', str(pri), '--title', title, '--detail', detail],
                       capture_output=True, text=True, timeout=60, env=env)
    out = (r.stdout or '') + (r.stderr or '')
    m = re.search(r'added task (\d+)', out)
    if m:
        return int(m.group(1)), None
    m = re.search(r'DUPLICATE — task (\d+)', out)
    if m:
        return int(m.group(1)), 'duplicate-title'
    return None, out.strip()[:300]


def _wq_note(tid, text):
    env = dict(os.environ); env.setdefault('WQ_AGENT', 'minerva-promoter')
    subprocess.run([WQ, 'note', str(tid), text], capture_output=True, text=True, timeout=30, env=env)


def _wq_edit_pri(tid, pri):
    env = dict(os.environ); env.setdefault('WQ_AGENT', 'minerva-promoter')
    subprocess.run([WQ, 'edit', str(tid), '--pri', str(pri)], capture_output=True, text=True, timeout=30, env=env)


def _find_open_product_ticket(key):
    """An OPEN promoter ticket for the same fleet_stack product -> (ticket_id, canonical pid)."""
    try:
        w = sqlite3.connect(WQ_DB, timeout=30)
        row = w.execute("SELECT id, detail FROM tasks WHERE status='open' AND detail LIKE ? ORDER BY id LIMIT 1",
                        (f'%minerva_product={key} %',)).fetchone()
        w.close()
        if not row:
            return None
        m = re.search(MARKER + r'([0-9a-f]{12})', row[1] or '')
        return (row[0], m.group(1) if m else '?')
    except Exception as e:
        log(f'wq db read failed ({e}) — no product collapse'); return None


def promote(prop, c=None, merge_into=None):
    """Returns (ticket_id|None, action). Never raises.
    merge_into=(ticket_id, canonical_pid): link this proposal to an existing
    same-product upgrade ticket instead of opening a second one."""
    if os.environ.get('MINERVA_PROMOTE', '1') == '0':
        return None, 'disabled'
    own = c is None
    if own:
        c = db()
    try:
        pid = prop['id']
        if prop['wq_ticket']:
            return prop['wq_ticket'], 'already'
        existing = _find_existing_ticket(pid)
        if existing:
            c.execute('UPDATE proposals SET wq_ticket=? WHERE id=?', (existing, pid)); c.commit()
            return existing, 'relinked'
        plan = _plan(c, pid)
        if plan is not None and str(plan['status']).startswith('rejected'):
            return None, 'plan-rejected'
        if _is_green_applied(c, pid, plan):
            return None, 'green-applied'
        intel = _intel(c, prop['source_intel_id'])
        pri, why = _priority(prop, intel, plan)
        key = product_key(prop['title'])
        if merge_into is None and key and UPGRADE_RE.search(str(prop['title'])):
            merge_into = _find_open_product_ticket(key)   # cross-run collapse (factory hook path)
        if merge_into:
            tid, canon = merge_into
            _wq_note(tid, f'also covers {MARKER}{pid} ({str(prop["title"])[:90]}; canonical {canon})'
                          + ('; SECURITY: ' + why if pri == 1 else ''))
            if pri == 1:
                _wq_edit_pri(tid, 1)
            c.execute('UPDATE proposals SET wq_ticket=? WHERE id=?', (tid, pid)); c.commit()
            return tid, 'merged'
        title = (('[SEC] ' if pri == 1 else '') + str(prop['title'] or '').strip())[:160]
        detail = _detail(prop, intel, plan, why, key)
        tid, err = _wq_add(title, detail, pri)
        if tid is None:
            log(f'  wq add FAILED for {pid}: {err}')
            return None, f'wq-add-failed:{err[:80]}'
        c.execute('UPDATE proposals SET wq_ticket=? WHERE id=?', (tid, pid)); c.commit()
        return tid, ('added' if err is None else err)
    except Exception as e:
        log(f'  promote error for {prop["id"] if prop else "?"}: {type(e).__name__}: {str(e)[:120]}')
        return None, f'error:{type(e).__name__}'
    finally:
        if own:
            c.close()


SINCE_DEFAULT = os.environ.get('MINERVA_PROMOTE_SINCE', '2026-09-03')  # wq#1383 grounding fix; older parked rows are pre-grounding


def run(limit=200, since=SINCE_DEFAULT):
    """Backfill parked proposals created on/after `since`. Same-product upgrade
    proposals collapse into one ticket (newest proposal is canonical; the rest
    are linked + noted), so five Langfuse bumps become one Langfuse ticket."""
    migrate()
    c = db()
    rows = c.execute(f"SELECT * FROM proposals WHERE status IN ({','.join('?' * len(ELIGIBLE))}) "
                     "AND created_at >= ? ORDER BY created_at DESC LIMIT ?", ELIGIBLE + (since, limit)).fetchall()
    tally, canon = {}, {}   # canon: product repo -> (ticket_id, canonical pid)
    for prop in rows:
        key = product_key(prop['title'])
        merge = None
        if key and UPGRADE_RE.search(str(prop['title'])) and key in canon and not prop['wq_ticket']:
            merge = canon[key]
        tid, action = promote(prop, c, merge_into=merge)
        tally[action.split(':')[0]] = tally.get(action.split(':')[0], 0) + 1
        if tid and key and UPGRADE_RE.search(str(prop['title'])) and key not in canon:
            canon[key] = (tid, prop['id'])
        if action in ('added', 'relinked', 'duplicate-title', 'merged'):
            log(f'  {action:9s} wq#{tid} <- {prop["id"]} {str(prop["title"])[:60]}')
    c.close()
    log(f'run done over {len(rows)} parked since {since}: {tally}')
    return tally


def _wq_edit_detail(tid, detail):
    env = dict(os.environ); env.setdefault('WQ_AGENT', 'minerva-promoter')
    r = subprocess.run([WQ, 'edit', str(tid), '--detail', detail], capture_output=True, text=True, timeout=30, env=env)
    return r.returncode == 0, ((r.stdout or '') + (r.stderr or '')).strip()[:200]


def reemit(tids):
    """wq#1535: rewrite the body of live promoter tickets in place. Everything but
    `detail` (pri, lane, notes, decisions) is untouched; the old body is saved under
    PUBLISH_ROOT/_reemit_backup/<tid>.txt for a `wq edit --detail` restore."""
    migrate()
    bak = PUBLISH_ROOT / '_reemit_backup'; bak.mkdir(parents=True, exist_ok=True)
    c = db()
    w = sqlite3.connect(WQ_DB, timeout=30)
    done = []
    for tid in tids:
        row = w.execute('SELECT id, detail, status FROM tasks WHERE id=?', (int(tid),)).fetchone()
        if not row:
            log(f'  reemit {tid}: no such ticket'); continue
        m = re.search(MARKER + r'([0-9a-f]{12})', row[1] or '')
        if not m:
            log(f'  reemit {tid}: not a promoter ticket (no {MARKER} marker)'); continue
        prop = c.execute('SELECT * FROM proposals WHERE id=?', (m.group(1),)).fetchone()
        if not prop:
            log(f'  reemit {tid}: proposal {m.group(1)} not in minerva.db'); continue
        (bak / f'{tid}.txt').write_text(row[1] or '')
        plan = _plan(c, prop['id']); intel = _intel(c, prop['source_intel_id'])
        pri, why = _priority(prop, intel, plan); key = product_key(prop['title'])
        ok, out = _wq_edit_detail(tid, _detail(prop, intel, plan, why, key))
        log(f'  reemit wq#{tid} <- {prop["id"]} {"OK" if ok else "FAILED " + out}')
        if ok:
            done.append(int(tid))
    w.close(); c.close()
    return done


def status():
    migrate()
    c = db()
    for r in c.execute(f"SELECT status, COUNT(*) n, SUM(wq_ticket IS NOT NULL) promoted FROM proposals "
                       f"WHERE status IN ({','.join('?' * len(ELIGIBLE))}) GROUP BY status", ELIGIBLE):
        print(f'{r["status"]:14s} parked={r["n"]:4d} promoted={r["promoted"] or 0}')
    c.close()


if __name__ == '__main__':
    a = sys.argv[1:] or ['status']
    if a[0] == 'run':
        run(int(a[1]) if len(a) > 1 else 200)
    elif a[0] == 'reemit':
        reemit(a[1:])
    elif a[0] == 'one':
        migrate(); c = db(); p = c.execute('SELECT * FROM proposals WHERE id=?', (a[1],)).fetchone()
        print(promote(p, c) if p else 'no such proposal'); c.close()
    elif a[0] == 'preview':
        migrate(); c = db(); p = c.execute('SELECT * FROM proposals WHERE id=?', (a[1],)).fetchone()
        if not p:
            print('no such proposal'); sys.exit(1)
        plan = _plan(c, p['id']); intel = _intel(c, p['source_intel_id'])
        pri, why = _priority(p, intel, plan); key = product_key(p['title'])
        print(f'pri={pri} why={why} lane={LANE} product={key} plan_lane={plan["lane"] if plan else None}')
        print('TITLE:', ('[SEC] ' if pri == 1 else '') + p['title']); print(_detail(p, intel, plan, why, key)); c.close()
    else:
        status()
